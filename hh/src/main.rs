//! hack-house — encrypted collaborative sessions with a summoned sandbox.
//!
//! This binary exposes the crypto-parity spike used to prove the Rust client
//! speaks the same SRP / Fernet dialect as the Python Sanic server, plus the
//! ratatui UI built on top of that proven foundation.

mod app;
mod crypto;
mod ft;
mod layout;
mod net;
mod sbx;
mod theme;
mod ui;

use anyhow::{Context, Result};
use base64::Engine;
use clap::{Parser, Subcommand};
use serde_json::json;

const STD: base64::engine::general_purpose::GeneralPurpose =
    base64::engine::general_purpose::STANDARD;

#[derive(Parser)]
#[command(name = "hack-house", about = "encrypted collaborative sessions")]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// Join a house: SRP auth then launch the ratatui UI.
    Connect {
        ip: String,
        port: u16,
        /// Display name (handle) to join as. Omit to be prompted on join.
        user: Option<String>,
        #[arg(long)]
        password: String,
        #[arg(long, default_value_t = false)]
        no_tls: bool,
        #[arg(long, default_value_t = false)]
        insecure: bool,
        /// Path to a theme (vestments) TOML file.
        #[arg(long)]
        theme: Option<String>,
    },
    /// Debug: print the derived room Fernet key for a password + room_salt(hex).
    Roomkey {
        password: String,
        room_salt_hex: String,
    },
    /// Run the offline SRP golden-vector self-test.
    Selftest,
    /// Debug: compute A and M from explicit a/salt/B hex (parity check vs python).
    Srpm {
        a_hex: String,
        salt_hex: String,
        b_hex: String,
        #[arg(long, default_value = "labtest")]
        password: String,
        #[arg(long, default_value = "chat")]
        user: String,
    },
    /// Perform a live SRP handshake + send one encrypted message (interop proof).
    Handshake {
        ip: String,
        port: u16,
        user: String,
        #[arg(long)]
        password: String,
        #[arg(long, default_value_t = false)]
        no_tls: bool,
        #[arg(long, default_value_t = false)]
        insecure: bool,
    },
}

fn main() -> Result<()> {
    match Cli::parse().cmd {
        Cmd::Connect {
            ip,
            port,
            user,
            password,
            no_tls,
            insecure,
            theme,
        } => {
            // Pick a handle and authenticate. If no name was given on the CLI,
            // prompt for one as the first thing on join — and re-prompt if the
            // server rejects it (e.g. the name is already taken in the room).
            let interactive = user.is_none();
            let mut name = match user {
                Some(u) => u,
                None => prompt_handle()?,
            };
            let session = loop {
                match net::authenticate(&ip, port, &name, &password, no_tls, insecure) {
                    Ok(s) => break s,
                    Err(e) if interactive => {
                        eprintln!(
                            "✖ {e:#}\n  that handle didn't work (taken or full?) — pick another."
                        );
                        name = prompt_handle()?;
                    }
                    Err(e) => return Err(e),
                }
            };
            let params = net::ConnParams {
                ip,
                port,
                user: name,
                password,
                no_tls,
                insecure,
            };
            let theme = match theme {
                Some(p) => theme::Theme::load(&p)?,
                None => theme::Theme::default(),
            };
            tokio::runtime::Runtime::new()?.block_on(app::run(params, session, theme))
        }
        Cmd::Roomkey {
            password,
            room_salt_hex,
        } => {
            let salt = hex::decode(room_salt_hex)?;
            let f = crypto::room_fernet(password.as_bytes(), &salt)?;
            // fernet crate doesn't expose the key; re-derive + print the b64 key.
            use base64::Engine;
            let hk = hkdf::Hkdf::<sha2::Sha256>::new(Some(&salt), password.as_bytes());
            let mut okm = [0u8; 32];
            hk.expand(b"cmd-chat-room-key", &mut okm).unwrap();
            println!("{}", base64::engine::general_purpose::URL_SAFE.encode(okm));
            let _ = f;
            Ok(())
        }
        Cmd::Selftest => selftest(),
        Cmd::Srpm {
            a_hex,
            salt_hex,
            b_hex,
            password,
            user,
        } => {
            let a = hex::decode(a_hex)?;
            let salt = hex::decode(salt_hex)?;
            let b = hex::decode(b_hex)?;
            let c = crypto::SrpClient::with_a(user.as_bytes(), password.as_bytes(), &a);
            println!("A {}", hex::encode(c.a_bytes()));
            let ch = c.process_challenge(&salt, &b)?;
            println!("M {}", hex::encode(&ch.m));
            println!("K {}", hex::encode(&ch.session_key));
            Ok(())
        }
        Cmd::Handshake {
            ip,
            port,
            user,
            password,
            no_tls,
            insecure,
        } => handshake(&ip, port, &user, &password, no_tls, insecure),
    }
}

/// Prompt for a display name on stdin before the TUI starts. Loops until a
/// non-empty handle is entered; errors only if stdin closes (EOF).
fn prompt_handle() -> Result<String> {
    use std::io::Write;
    loop {
        print!("✝ choose your handle: ");
        std::io::stdout().flush()?;
        let mut s = String::new();
        if std::io::stdin().read_line(&mut s)? == 0 {
            anyhow::bail!("no handle entered (stdin closed)");
        }
        let name = s.trim();
        if !name.is_empty() {
            return Ok(name.to_string());
        }
        eprintln!("a handle can't be empty.");
    }
}

fn selftest() -> Result<()> {
    // Re-derive the golden vectors at runtime as a smoke check.
    let c = crypto::SrpClient::with_a(b"chat", b"labtest", &{
        let mut v = vec![0x80u8];
        v.extend(std::iter::repeat_n(0x22u8, 31));
        v
    });
    let a = hex::encode(c.a_bytes());
    println!("A  = {}…", &a[..32]);
    let salt = hex::decode("0a1b2c3d")?;
    let b = hex::decode("047426a55963c70bc385c6a51f6e9dc0bfe5e16b0d1fee4f566fb54b60fa77144f15ed1ee6ade007bd92f2b90846e1ee083ab4290239420606f48a1d861f759543d7856cbce21fd7fec98c9961a66610b412fea2efc5be78f35b18fd48176ac80c3a1cbefacac81e25e7da8079fac4012d01c47d85b783c2ea7340819bfe73d29cd0953d47c8fade77caa5459fb77d88fb918c073a77c495fa884859142a270cb0b1668de06131b150df4dbc931953a381710b7fdb98a953d6f77a4bba847c4c62c15cca8e514dc13f531427966a553c461aa4ab0caec9665612861fef03d48676e5f6551fc8ca4317f3118e0294c949bd2f5821e5900e7f695225dafa0ba2d2")?;
    let ch = c.process_challenge(&salt, &b)?;
    let want_m = "6e733ba88eb86c52e3be89207d2815a65b4dea8116f668af5de1b66ce1f047dd";
    assert_eq!(hex::encode(&ch.m), want_m, "M mismatch vs pysrp");
    println!("M matches pysrp golden vector ✓");
    println!("selftest passed — Rust SRP ≡ Python srp");
    Ok(())
}

fn handshake(
    ip: &str,
    port: u16,
    user: &str,
    password: &str,
    no_tls: bool,
    insecure: bool,
) -> Result<()> {
    let scheme = if no_tls { "http" } else { "https" };
    let base = format!("{scheme}://{ip}:{port}");

    let http = reqwest::blocking::Client::builder()
        .danger_accept_invalid_certs(insecure && !no_tls)
        .timeout(std::time::Duration::from_secs(30))
        .build()?;

    // SRP identity is the fixed room identity b"chat" (see server srp_auth.py);
    // the display `user` name is carried only in the JSON, never in the SRP proof.
    let client = crypto::SrpClient::new(crypto::SRP_IDENTITY, password.as_bytes());

    // /srp/init
    let init: serde_json::Value = http
        .post(format!("{base}/srp/init"))
        .json(&json!({ "username": user, "A": STD.encode(client.a_bytes()) }))
        .send()
        .context("srp/init request")?
        .error_for_status()
        .context("srp/init status")?
        .json()?;

    let user_id = init["user_id"].as_str().context("no user_id")?.to_string();
    let b = STD.decode(init["B"].as_str().context("no B")?)?;
    let salt = STD.decode(init["salt"].as_str().context("no salt")?)?;
    let room_salt = STD.decode(init["room_salt"].as_str().context("no room_salt")?)?;
    println!("/srp/init ok — user_id={}…", &user_id[..8]);

    let ch = client.process_challenge(&salt, &b)?;

    // /srp/verify
    let verify: serde_json::Value = http
        .post(format!("{base}/srp/verify"))
        .json(&json!({
            "user_id": user_id,
            "username": user,
            "M": STD.encode(&ch.m),
        }))
        .send()
        .context("srp/verify request")?
        .error_for_status()
        .context("srp/verify status — auth rejected?")?
        .json()?;

    let server_hamk = STD.decode(verify["H_AMK"].as_str().context("no H_AMK")?)?;
    anyhow::ensure!(server_hamk == ch.h_amk, "server H_AMK mismatch — MITM?");
    let ws_token = verify["ws_token"]
        .as_str()
        .context("no ws_token")?
        .to_string();
    println!("/srp/verify ok — server identity proven (H_AMK ✓)");

    // Room key + encrypt a message the Python clients can read.
    let fernet = crypto::room_fernet(password.as_bytes(), &room_salt)?;
    let ct = fernet.encrypt(b"the house is open");

    // Connect WS and send the ciphertext.
    let ws_scheme = if no_tls { "ws" } else { "wss" };
    let ws_url = format!("{ws_scheme}://{ip}:{port}/ws/chat?user_id={user_id}&ws_token={ws_token}");
    let (mut sock, _resp) =
        tungstenite::connect(&ws_url).context("ws connect (insecure wss not yet wired)")?;
    println!("websocket attached to the house");

    // First frame is the `init` state snapshot.
    if let Ok(msg) = sock.read() {
        if let Ok(txt) = msg.into_text() {
            let v: serde_json::Value = serde_json::from_str(&txt).unwrap_or_default();
            println!("recv: type={}", v["type"].as_str().unwrap_or("?"));
        }
    }
    sock.send(tungstenite::Message::Text(ct))?;
    sock.flush()?;
    println!("sent encrypted offering");

    // Read frames until we see our broadcast echo, then decrypt it to prove the
    // full round-trip (Rust encrypt → server relay → Rust decrypt).
    for _ in 0..5 {
        match sock.read() {
            Ok(tungstenite::Message::Text(txt)) => {
                let v: serde_json::Value = serde_json::from_str(&txt).unwrap_or_default();
                if v["type"] == "message" {
                    if let Some(ct) = v["data"]["text"].as_str() {
                        match fernet.decrypt(ct) {
                            Ok(pt) => {
                                println!(
                                    "round-trip ✓ decrypted: {:?}",
                                    String::from_utf8_lossy(&pt)
                                );
                                break;
                            }
                            Err(_) => println!("[decrypt failed]"),
                        }
                    }
                }
            }
            Ok(_) => {}
            Err(e) => {
                println!("ws read ended: {e}");
                break;
            }
        }
    }
    Ok(())
}
