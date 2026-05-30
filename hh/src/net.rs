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
    pub user_id: String,
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
        user_id,
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

/// Decode one stored/broadcast message object into a ChatLine, or None to skip
/// (empty text, decrypt failure, or a file-transfer control frame).
fn decode_msg(room: &fernet::Fernet, m: &Value) -> Option<ChatLine> {
    let ct = m["text"].as_str()?;
    if ct.is_empty() {
        return None;
    }
    let (text, system) = match room.decrypt(ct) {
        Ok(pt) => {
            let t = String::from_utf8_lossy(&pt).to_string();
            if t.starts_with("{\"_ft\":") {
                return None; // file-transfer control frame — handled elsewhere (P5)
            }
            (t, false)
        }
        // Wrong room key / corrupt frame — surface, don't crash or hide silently.
        Err(_) => ("[unreadable — wrong room password?]".to_string(), true),
    };
    let stamp = m["timestamp"].as_str().unwrap_or("");
    let ts = if stamp.len() >= 19 { stamp[11..19].to_string() } else { String::new() };
    Some(ChatLine {
        ts,
        username: m["username"].as_str().unwrap_or("?").to_string(),
        text,
        system,
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
                    .filter_map(|m| decode_msg(&room, m))
                    .collect();
                tx.send(Net::Init { lines, users: parse_users(&v["users"]) })
            }
            "message" => match decode_msg(&room, &v["data"]) {
                Some(l) => tx.send(Net::Message(l)),
                None => Ok(()),
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
