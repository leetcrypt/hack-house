//! SRP authentication (blocking, one-shot) + async websocket transport and the
//! reader task that decrypts/parses server frames into `Net` events.

use crate::app::{ChatLine, Net, User};
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
    anyhow::ensure!(server_hamk == ch.h_amk, "server identity check failed (H_AMK) — MITM?");
    let ws_token = verify["ws_token"].as_str().context("no ws_token")?;

    let fernet = crypto::room_fernet(password.as_bytes(), &room_salt)?;
    let ws_scheme = if no_tls { "ws" } else { "wss" };
    let ws_url =
        format!("{ws_scheme}://{ip}:{port}/ws/chat?user_id={user_id}&ws_token={ws_token}");

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
    Ok(ws)
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
fn decode_msg(room: &fernet::Fernet, m: &Value, allow_sbx: bool) -> Decoded {
    let ct = match m["text"].as_str() {
        Some(c) if !c.is_empty() => c,
        _ => return Decoded::Skip,
    };
    let (text, system) = match room.decrypt(ct) {
        Ok(pt) => {
            let t = String::from_utf8_lossy(&pt).to_string();
            if t.starts_with("{\"_ft\":") {
                return Decoded::Skip; // file-transfer control frame (P5)
            }
            // Server-stamped (authenticated) sender of this message.
            let sender = m["username"].as_str().unwrap_or("?");
            if t.starts_with("{\"_perm\":") {
                return parse_perm(&t).map(Decoded::Sbx).unwrap_or(Decoded::Skip);
            }
            if t.starts_with("{\"_sbx\":") {
                // Don't replay terminal history from the stored snapshot.
                return if allow_sbx {
                    parse_sbx(&t, sender).map(Decoded::Sbx).unwrap_or(Decoded::Skip)
                } else {
                    Decoded::Skip
                };
            }
            (t, false)
        }
        Err(_) => ("[unreadable — wrong room password?]".to_string(), true),
    };
    let stamp = m["timestamp"].as_str().unwrap_or("");
    let ts = if stamp.len() >= 19 { stamp[11..19].to_string() } else { String::new() };
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
        _ => None,
    }
}

/// Parse a decrypted `{"_perm":"acl",...}` frame.
fn parse_perm(text: &str) -> Option<Net> {
    let v: Value = serde_json::from_str(text).ok()?;
    if v["_perm"].as_str()? != "acl" {
        return None;
    }
    let drivers = v["drivers"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|d| d.as_str().map(str::to_string))
        .collect();
    Some(Net::Perm {
        owner: v["owner"].as_str().unwrap_or("").to_string(),
        drivers,
    })
}

/// Read websocket frames forever, forwarding decoded `Net` events to the UI.
pub async fn reader(mut read: impl StreamExt<Item = Result<WsMsg, tokio_tungstenite::tungstenite::Error>> + Unpin, room: Arc<fernet::Fernet>, tx: UnboundedSender<Net>) {
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
                tx.send(Net::Init { lines, users: parse_users(&v["users"]) })
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
            "user_joined" => tx.send(Net::Joined(v["username"].as_str().unwrap_or("?").to_string())),
            "user_left" => tx.send(Net::Left(v["user_id"].as_str().unwrap_or("").to_string())),
            _ => Ok(()),
        };
        if sent.is_err() {
            return; // UI gone
        }
    }
    let _ = tx.send(Net::Closed);
}
