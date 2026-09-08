//! SRP authentication (blocking, one-shot) + async websocket transport and the
//! reader task that decrypts/parses server frames into `Net` events.

use crate::app::{CatalogItem, ChatLine, Net, User};
use crate::crypto;
use anyhow::{Context, Result};
use base64::engine::general_purpose::STANDARD;
use base64::Engine;
use futures_util::StreamExt;
use serde_json::{json, Value};
use std::sync::Arc;
use tokio::net::TcpStream;
use tokio::sync::mpsc::UnboundedSender;
use tokio_tungstenite::tungstenite::Message as WsMsg;
use tokio_tungstenite::{MaybeTlsStream, WebSocketStream};

type Ws = WebSocketStream<MaybeTlsStream<TcpStream>>;

pub struct Session {
    pub username: String,
    pub room: Arc<fernet::Fernet>,
    pub ws_url: String,
    pub no_tls: bool,
    pub insecure: bool,
}

/// The credentials needed to (re)authenticate a Session — kept so the UI can
/// re-run the SRP handshake and rejoin after a disconnect (AFK / server blip).
#[derive(Clone)]
pub struct ConnParams {
    pub ip: String,
    pub port: u16,
    pub user: String,
    pub password: String,
    pub no_tls: bool,
    pub insecure: bool,
}

/// The write half of a split websocket; outgoing frames are sent here.
pub type WsSink = futures_util::stream::SplitSink<Ws, WsMsg>;

/// Full SRP handshake against the Sanic server. Returns a ready Session
/// (room key derived, ws url built) but does not open the websocket.
pub fn authenticate(
    ip: &str,
    port: u16,
    user: &str,
    password: &str,
    no_tls: bool,
    insecure: bool,
) -> Result<Session> {
    let scheme = if no_tls { "http" } else { "https" };
    let base = format!("{scheme}://{ip}:{port}");
    let http = reqwest::blocking::Client::builder()
        .danger_accept_invalid_certs(insecure && !no_tls)
        .timeout(std::time::Duration::from_secs(30))
        .build()?;

    let client = crypto::SrpClient::new(crypto::SRP_IDENTITY, password.as_bytes());

    let init: Value = http
        .post(format!("{base}/srp/init"))
        .json(&json!({ "username": user, "A": STANDARD.encode(client.a_bytes()) }))
        .send()
        .context("srp/init request")?
        .error_for_status()
        .context("srp/init rejected (name taken or house full?)")?
        .json()?;

    let user_id = init["user_id"].as_str().context("no user_id")?.to_string();
    let b = STANDARD.decode(init["B"].as_str().context("no B")?)?;
    let salt = STANDARD.decode(init["salt"].as_str().context("no salt")?)?;
    let room_salt = STANDARD.decode(init["room_salt"].as_str().context("no room_salt")?)?;

    let ch = client.process_challenge(&salt, &b)?;

    let verify: Value = http
        .post(format!("{base}/srp/verify"))
        .json(&json!({ "user_id": user_id, "username": user, "M": STANDARD.encode(&ch.m) }))
        .send()
        .context("srp/verify request")?
        .error_for_status()
        .context("srp/verify rejected — wrong room password?")?
        .json()?;

    let server_hamk = STANDARD.decode(verify["H_AMK"].as_str().context("no H_AMK")?)?;
    anyhow::ensure!(
        server_hamk == ch.h_amk,
        "server identity check failed (H_AMK) — MITM?"
    );
    let ws_token = verify["ws_token"].as_str().context("no ws_token")?;

    let fernet = crypto::room_fernet(password.as_bytes(), &room_salt)?;
    let ws_scheme = if no_tls { "ws" } else { "wss" };
    let ws_url = format!("{ws_scheme}://{ip}:{port}/ws/chat?user_id={user_id}&ws_token={ws_token}");

    Ok(Session {
        username: user.to_string(),
        room: Arc::new(fernet),
        ws_url,
        no_tls,
        insecure,
    })
}

pub async fn connect(session: &Session) -> Result<Ws> {
    if !session.no_tls && session.insecure {
        anyhow::bail!(
            "self-signed (insecure) wss is not yet wired in the TUI — \
             use --no-tls or a trusted certificate"
        );
    }
    let (ws, _) = tokio_tungstenite::connect_async(&session.ws_url)
        .await
        .context("websocket connect")?;
    // Disable Nagle's algorithm. Sandbox PTY echo, keystrokes, and chat are all
    // small frames; Nagle holds a small segment waiting for more data until the
    // prior one is ACKed, and paired with delayed-ACK (~40 ms) — amplified by
    // Tailscale's RTT — that is a classic cause of "type, pause, then a burst"
    // lag for the non-host viewer. The default (and Tailscale) path is plaintext
    // ws, a `Plain` TcpStream, so set TCP_NODELAY there.
    if let MaybeTlsStream::Plain(s) = ws.get_ref() {
        let _ = s.set_nodelay(true);
    }
    Ok(ws)
}

/// Open the websocket for a session, spawn the reader task feeding `tx`, and
/// hand back the write half. Used for the initial connect and every reconnect.
pub async fn open(session: &Session, tx: UnboundedSender<Net>) -> Result<WsSink> {
    let ws = connect(session).await?;
    let (write, read) = ws.split();
    tokio::spawn(reader(read, session.room.clone(), tx));
    Ok(write)
}

fn parse_users(v: &Value) -> Vec<User> {
    v.as_array()
        .into_iter()
        .flatten()
        .filter_map(|u| {
            Some(User {
                user_id: u["user_id"].as_str()?.to_string(),
                username: u["username"].as_str().unwrap_or("?").to_string(),
            })
        })
        .collect()
}

/// Classification of a decrypted message payload.
enum Decoded {
    Chat(ChatLine),
    Sbx(Net),
    Skip,
}

/// Decrypt + classify one stored/broadcast message object.
fn decode_msg(room: &fernet::Fernet, m: &Value, live: bool) -> Decoded {
    let ct = match m["text"].as_str() {
        Some(c) if !c.is_empty() => c,
        _ => return Decoded::Skip,
    };
    let (text, system) = match room.decrypt(ct) {
        Ok(pt) => {
            let t = String::from_utf8_lossy(&pt).to_string();
            // Server-stamped (authenticated) sender of this message.
            let sender = m["username"].as_str().unwrap_or("?");
            if t.starts_with("{\"_perm\":") {
                return parse_perm(&t).map(Decoded::Sbx).unwrap_or(Decoded::Skip);
            }
            // Control frames are live-only — never replayed from the stored snapshot.
            if t.starts_with("{\"_sbx\":") {
                return if live {
                    parse_sbx(&t, sender)
                        .map(Decoded::Sbx)
                        .unwrap_or(Decoded::Skip)
                } else {
                    Decoded::Skip
                };
            }
            if t.starts_with("{\"_ft\":") {
                return if live {
                    crate::ft::parse(&t, sender)
                        .map(|f| Decoded::Sbx(Net::Ft(f)))
                        .unwrap_or(Decoded::Skip)
                } else {
                    Decoded::Skip
                };
            }
            if t.starts_with("{\"_ai\":") {
                return if live {
                    parse_ai(&t).map(Decoded::Sbx).unwrap_or(Decoded::Skip)
                } else {
                    Decoded::Skip
                };
            }
            (t, false)
        }
        Err(_) => ("[unreadable — wrong room password?]".to_string(), true),
    };
    // Server-stamped ISO time "YYYY-MM-DDTHH:MM:SS…"; show just "HH:MM:SS".
    // Slice on char boundaries (chars, not bytes): the server is untrusted in our
    // zero-knowledge model and could send a non-ASCII timestamp, which byte
    // slicing `stamp[11..19]` would panic on at a non-boundary.
    let stamp = m["timestamp"].as_str().unwrap_or("");
    let ts: String = stamp.chars().skip(11).take(8).collect();
    Decoded::Chat(ChatLine {
        ts,
        username: m["username"].as_str().unwrap_or("?").to_string(),
        text,
        system,
    })
}

/// Parse a decrypted `{"_sbx":...}` frame into a Net event. `sender` is the
/// server-authenticated username of whoever sent it (used to gate drive input).
fn parse_sbx(text: &str, sender: &str) -> Option<Net> {
    let v: Value = serde_json::from_str(text).ok()?;
    match v["_sbx"].as_str()? {
        "status" => Some(Net::SbxStatus {
            backend: v["backend"].as_str().unwrap_or("?").to_string(),
            ready: v["state"].as_str() == Some("ready"),
            rows: v["rows"].as_u64().unwrap_or(24) as u16,
            cols: v["cols"].as_u64().unwrap_or(80) as u16,
        }),
        "resize" => Some(Net::SbxResize {
            rows: v["rows"].as_u64().unwrap_or(24) as u16,
            cols: v["cols"].as_u64().unwrap_or(80) as u16,
        }),
        "data" => Some(Net::SbxData(STANDARD.decode(v["b64"].as_str()?).ok()?)),
        "input" => Some(Net::SbxInput {
            from: sender.to_string(),
            bytes: STANDARD.decode(v["b64"].as_str()?).ok()?,
        }),
        // A member opened a shared VirtualBox VM on their own machine. `by` is
        // the server-stamped sender (trusted), not the frame's own claim.
        "vm" => Some(Net::VmOpened {
            by: sender.to_string(),
            vm: v["vm"].as_str().unwrap_or("a VM").to_string(),
        }),
        // VM-trading (Phase B). `by` is the server-authenticated sender; `to`
        // names the intended recipient (the handler acts only if it's itself).
        "catreq" => Some(Net::SbxCatReq {
            by: sender.to_string(),
            to: v["to"].as_str().unwrap_or("").to_string(),
        }),
        "catalog" => Some(Net::SbxCatalog {
            by: sender.to_string(),
            to: v["to"].as_str().unwrap_or("").to_string(),
            items: v["items"]
                .as_array()
                .map(|a| a.iter().map(parse_catalog_item).collect())
                .unwrap_or_default(),
        }),
        "pullreq" => Some(Net::SbxPullReq {
            by: sender.to_string(),
            to: v["to"].as_str().unwrap_or("").to_string(),
            label: v["label"].as_str()?.to_string(),
        }),
        _ => None,
    }
}

/// Parse one `_sbx:catalog` item object into a `CatalogItem`. Tolerant: missing
/// fields default to empty so a malformed row degrades rather than dropping the
/// whole catalog.
fn parse_catalog_item(v: &Value) -> CatalogItem {
    CatalogItem {
        label: v["label"].as_str().unwrap_or("").to_string(),
        purpose: v["purpose"].as_str().unwrap_or("").to_string(),
        status: v["status"].as_str().unwrap_or("").to_string(),
        tags: v["tags"]
            .as_array()
            .map(|a| a.iter().filter_map(|t| t.as_str().map(str::to_string)).collect())
            .unwrap_or_default(),
        size_bytes: v["size"].as_u64(),
    }
}

/// Parse a decrypted `{"_ai":...}` frame from an AI agent. `"typing"` toggles the
/// thinking spinner; `"stream"` carries the cumulative reply text for a live
/// preview bubble (`done` clears it once the final message is posted).
fn parse_ai(text: &str) -> Option<Net> {
    let v: Value = serde_json::from_str(text).ok()?;
    let name = || v["name"].as_str().unwrap_or("ai").to_string();
    match v["_ai"].as_str()? {
        "typing" => Some(Net::AiTyping {
            name: name(),
            on: v["on"].as_bool().unwrap_or(false),
        }),
        "stream" => Some(Net::AiStream {
            name: name(),
            text: v["text"].as_str().unwrap_or("").to_string(),
            done: v["done"].as_bool().unwrap_or(false),
        }),
        _ => None,
    }
}

/// Parse a decrypted `{"_perm":"acl",...}` frame.
fn parse_perm(text: &str) -> Option<Net> {
    let v: Value = serde_json::from_str(text).ok()?;
    if v["_perm"].as_str()? != "acl" {
        return None;
    }
    let list = |key: &str| {
        v[key]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(|d| d.as_str().map(str::to_string))
            .collect::<Vec<_>>()
    };
    Some(Net::Perm {
        owner: v["owner"].as_str().unwrap_or("").to_string(),
        drivers: list("drivers"),
        sudoers: list("sudoers"),
    })
}

/// Read websocket frames forever, forwarding decoded `Net` events to the UI.
pub async fn reader(
    mut read: impl StreamExt<Item = Result<WsMsg, tokio_tungstenite::tungstenite::Error>> + Unpin,
    room: Arc<fernet::Fernet>,
    tx: UnboundedSender<Net>,
) {
    while let Some(frame) = read.next().await {
        let txt = match frame {
            Ok(WsMsg::Text(t)) => t,
            Ok(WsMsg::Ping(_)) | Ok(WsMsg::Pong(_)) => continue,
            _ => break,
        };
        let v: Value = match serde_json::from_str(&txt) {
            Ok(v) => v,
            Err(_) => continue,
        };
        let sent = match v["type"].as_str().unwrap_or("") {
            "init" => {
                let lines = v["messages"]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .filter_map(|m| match decode_msg(&room, m, false) {
                        Decoded::Chat(l) => Some(l),
                        _ => None,
                    })
                    .collect();
                tx.send(Net::Init {
                    lines,
                    users: parse_users(&v["users"]),
                    onion: v["onion"].as_str().unwrap_or("").to_string(),
                    reach: v["reach"]
                        .as_array()
                        .map(|a| {
                            a.iter()
                                .filter_map(|r| {
                                    Some((
                                        r["label"].as_str()?.to_string(),
                                        r["addr"].as_str()?.to_string(),
                                    ))
                                })
                                .collect()
                        })
                        .unwrap_or_default(),
                })
            }
            "message" => match decode_msg(&room, &v["data"], true) {
                Decoded::Chat(l) => tx.send(Net::Message(l)),
                Decoded::Sbx(ev) => tx.send(ev),
                Decoded::Skip => Ok(()),
            },
            "roster" => tx.send(Net::Roster {
                users: parse_users(&v["users"]),
                capacity: v["capacity"].as_u64().unwrap_or(0) as usize,
            }),
            "user_joined" => tx.send(Net::Joined(
                v["username"].as_str().unwrap_or("?").to_string(),
            )),
            "user_left" => tx.send(Net::Left(v["user_id"].as_str().unwrap_or("").to_string())),
            _ => Ok(()),
        };
        if sent.is_err() {
            return; // UI gone
        }
    }
    let _ = tx.send(Net::Closed);
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;

    fn test_room() -> fernet::Fernet {
        fernet::Fernet::new(&fernet::Fernet::generate_key()).expect("valid key")
    }

    // A decode_msg call must never panic on a malformed timestamp. This is the
    // concrete regression for the byte-slice `stamp[11..19]` that would panic on
    // a non-ASCII (multibyte) timestamp from an untrusted/zero-knowledge server.
    #[test]
    fn decode_msg_multibyte_timestamp_does_not_panic() {
        let room = test_room();
        // A '✝' (3 bytes) straddling the old 11..19 byte window.
        let m = json!({ "text": "not-real-ciphertext", "timestamp": "2026-06-04✝✝✝✝✝✝", "username": "alice" });
        let _ = decode_msg(&room, &m, true); // must not panic
    }

    proptest! {
        // The frame parsers consume attacker-controlled JSON (decrypted from a
        // zero-knowledge relay) — they must classify or reject, never panic.
        #[test]
        fn parse_sbx_never_panics(s in ".*", sender in ".*") {
            let _ = parse_sbx(&s, &sender);
        }

        #[test]
        fn parse_ai_never_panics(s in ".*") {
            let _ = parse_ai(&s);
        }

        #[test]
        fn parse_perm_never_panics(s in ".*") {
            let _ = parse_perm(&s);
        }

        // Well-formed JSON envelopes with arbitrary field values (the realistic
        // shape a hostile peer would send): still no panic.
        #[test]
        fn parse_sbx_structured_never_panics(
            kind in "[a-z]{0,10}", b64 in ".*", rows in any::<i64>(), cols in any::<i64>(), sender in ".*"
        ) {
            let frame = json!({"_sbx": kind, "b64": b64, "rows": rows, "cols": cols, "vm": b64}).to_string();
            let _ = parse_sbx(&frame, &sender);
        }

        #[test]
        fn parse_ai_structured_never_panics(kind in "[a-z]{0,10}", name in ".*", text in ".*", on in any::<bool>()) {
            let frame = json!({"_ai": kind, "name": name, "text": text, "on": on, "done": on}).to_string();
            let _ = parse_ai(&frame);
        }

        // decode_msg sees server-stamped + peer-encrypted objects; a malicious
        // server controls timestamp/username and a peer controls the ciphertext.
        // None of those combinations may panic the client.
        #[test]
        fn decode_msg_never_panics(text in ".*", ts in ".*", user in ".*", live in any::<bool>()) {
            let room = test_room();
            let m = json!({ "text": text, "timestamp": ts, "username": user });
            let _ = decode_msg(&room, &m, live);
        }

        // parse_users tolerates arbitrary array shapes.
        #[test]
        fn parse_users_never_panics(id in ".*", name in ".*") {
            let v = json!([{ "user_id": id, "username": name }, "garbage", 42, null]);
            let _ = parse_users(&v);
        }
    }

    // ── Phase B VM-trading frames ──────────────────────────────────────────
    #[test]
    fn parse_sbx_catreq_uses_authenticated_sender() {
        // The frame can claim any `to`, but `by` is the server-stamped sender.
        let frame = json!({"_sbx":"catreq","to":"bob"}).to_string();
        let Some(Net::SbxCatReq { by, to }) = parse_sbx(&frame, "alice") else {
            panic!("expected SbxCatReq");
        };
        assert_eq!(by, "alice");
        assert_eq!(to, "bob");
    }

    #[test]
    fn parse_sbx_catalog_parses_items() {
        let frame = json!({
            "_sbx":"catalog","to":"alice",
            "items":[
                {"label":"kali-recon","purpose":"recon box","status":"in_progress",
                 "tags":["recon","kali"],"size": 1024},
                {"label":"bare"} // missing fields must default, not drop the row
            ]
        }).to_string();
        let Some(Net::SbxCatalog { by, to, items }) = parse_sbx(&frame, "bob") else {
            panic!("expected SbxCatalog");
        };
        assert_eq!(by, "bob");
        assert_eq!(to, "alice");
        assert_eq!(items.len(), 2);
        assert_eq!(items[0].label, "kali-recon");
        assert_eq!(items[0].tags, vec!["recon".to_string(), "kali".to_string()]);
        assert_eq!(items[0].size_bytes, Some(1024));
        assert_eq!(items[1].label, "bare");
        assert!(items[1].purpose.is_empty());
        assert_eq!(items[1].size_bytes, None);
    }

    #[test]
    fn parse_sbx_pullreq_requires_label() {
        let ok = json!({"_sbx":"pullreq","to":"bob","label":"kali-recon"}).to_string();
        assert!(matches!(parse_sbx(&ok, "alice"), Some(Net::SbxPullReq { .. })));
        // No label → reject the frame rather than pull a nameless VM.
        let bad = json!({"_sbx":"pullreq","to":"bob"}).to_string();
        assert!(parse_sbx(&bad, "alice").is_none());
    }
}
