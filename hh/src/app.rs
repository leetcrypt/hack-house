//! TUI application state, network event model, and the async run loop.

use crate::net::{self, Session};
use crate::sbx;
use crate::theme::Theme;
use crate::ui;
use anyhow::Result;
use base64::engine::general_purpose::STANDARD;
use base64::Engine;
use crossterm::event::{Event, EventStream, KeyCode, KeyEventKind, KeyModifiers};
use crossterm::execute;
use crossterm::terminal::{
    disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen,
};
use futures_util::{SinkExt, StreamExt};
use ratatui::backend::CrosstermBackend;
use ratatui::Terminal;
use serde_json::json;
use std::time::Duration;
use tokio::sync::mpsc::{unbounded_channel, UnboundedReceiver, UnboundedSender};
use tokio_tungstenite::tungstenite::Message as WsMsg;

const SBX_NAME: &str = "hack-house";

/// One rendered chat row.
#[derive(Clone)]
pub struct ChatLine {
    pub ts: String,
    pub username: String,
    pub text: String,
    pub system: bool,
}

#[derive(Clone)]
pub struct User {
    pub user_id: String,
    pub username: String,
}

/// Decoded events arriving from the websocket reader task.
pub enum Net {
    Init { lines: Vec<ChatLine>, users: Vec<User> },
    Message(ChatLine),
    Roster { users: Vec<User>, capacity: usize },
    Joined(String),
    Left(String),
    SbxStatus { backend: String, ready: bool, rows: u16, cols: u16 },
    SbxResize { rows: u16, cols: u16 },
    SbxData(Vec<u8>),
    SbxInput { from: String, bytes: Vec<u8> },
    Perm { owner: String, drivers: Vec<String> },
    Sys(String),
    Closed,
}

/// Sandbox handoff from the async launch task to the run loop.
enum BrokerMsg {
    Ready {
        sb: sbx::Sandbox,
        backend: sbx::Backend,
        name: String,
        rows: u16,
        cols: u16,
    },
    Failed,
}

/// Local view of the shared sandbox terminal (everyone renders from `data`).
pub struct SbxView {
    pub parser: vt100::Parser,
    pub backend: String,
}

pub struct App {
    pub me: String,
    pub lines: Vec<ChatLine>,
    pub users: Vec<User>,
    pub capacity: usize,
    pub input: String,
    pub connected: bool,
    pub sandbox: Option<SbxView>,
    pub driving: bool,
    /// Sandbox owner (the initiator / superuser). Empty until a sandbox launches.
    pub owner: Option<String>,
    /// Members allowed to drive the shared shell (always includes the owner).
    pub drivers: std::collections::HashSet<String>,
}

impl App {
    fn new(me: String) -> Self {
        Self {
            me,
            lines: Vec::new(),
            users: Vec::new(),
            capacity: 0,
            input: String::new(),
            connected: false,
            sandbox: None,
            driving: false,
            owner: None,
            drivers: std::collections::HashSet::new(),
        }
    }

    pub fn is_owner(&self) -> bool {
        self.owner.as_deref() == Some(self.me.as_str())
    }

    pub fn can_drive(&self) -> bool {
        self.drivers.contains(&self.me)
    }

    fn sys(&mut self, text: impl Into<String>) {
        self.lines.push(ChatLine {
            ts: String::new(),
            username: String::new(),
            text: text.into(),
            system: true,
        });
    }

    fn apply(&mut self, n: Net) {
        match n {
            Net::Init { lines, users } => {
                self.lines = lines;
                self.users = users;
                self.connected = true;
                self.sys(format!("joined as {} ⛧", self.me));
                self.sys("/sbx launch [local|docker|multipass] · /sbx stop · F2 to drive");
            }
            Net::Message(l) => self.lines.push(l),
            Net::Roster { users, capacity } => {
                self.users = users;
                self.capacity = capacity;
            }
            Net::Joined(name) => self.sys(format!("{name} entered the house")),
            Net::Left(uid) => {
                if let Some(p) = self.users.iter().position(|u| u.user_id == uid) {
                    let name = self.users.remove(p).username;
                    self.sys(format!("{name} left"));
                }
            }
            Net::SbxStatus { backend, ready, rows, cols } => {
                if ready {
                    self.sandbox = Some(SbxView {
                        parser: vt100::Parser::new(rows.max(1), cols.max(1), 0),
                        backend: backend.clone(),
                    });
                    self.sys(format!("⛧ sandbox summoned ({backend}) — F2 to drive"));
                } else {
                    self.sandbox = None;
                    self.driving = false;
                    self.owner = None;
                    self.drivers.clear();
                    self.sys("⛧ sandbox dismissed");
                }
            }
            Net::Perm { owner, drivers } => {
                let new: std::collections::HashSet<String> = drivers.into_iter().collect();
                // Surface changes that affect me.
                if !owner.is_empty() && self.owner.as_deref() != Some(owner.as_str()) {
                    self.sys(format!("⛧ {owner} is the superuser (sandbox owner)"));
                }
                if new.contains(&self.me) && !self.drivers.contains(&self.me) && self.owner.is_some() {
                    self.sys("⛧ you were granted drive (you can drive — F2)");
                } else if !new.contains(&self.me) && self.drivers.contains(&self.me) {
                    self.driving = false;
                    self.sys("⛧ your drive permission was revoked");
                }
                self.owner = Some(owner).filter(|o| !o.is_empty());
                self.drivers = new;
            }
            Net::SbxResize { rows, cols } => {
                if let Some(v) = &mut self.sandbox {
                    v.parser.set_size(rows.max(1), cols.max(1));
                }
            }
            Net::SbxData(bytes) => {
                if let Some(v) = &mut self.sandbox {
                    v.parser.process(&bytes);
                }
            }
            Net::SbxInput { .. } => {} // broker enforces + writes to PTY in the run loop
            Net::Sys(t) => self.sys(t),
            Net::Closed => {
                self.connected = false;
                self.sys("connection closed");
            }
        }
    }
}

/// Approximate inner dimensions of the sandbox pane for a terminal of this size
/// (mirrors ui.rs layout: top bar 1, input 3, sandbox = 55% of the body).
fn sbx_dims(term_w: u16, term_h: u16) -> (u16, u16) {
    let body_h = term_h.saturating_sub(4);
    let sbx_h = (body_h as u32 * 55 / 100) as u16;
    (sbx_h.saturating_sub(2).max(1), term_w.saturating_sub(2).max(1))
}

/// Translate a key event into the bytes a PTY expects (drive mode).
fn key_to_pty(code: KeyCode, mods: KeyModifiers) -> Option<Vec<u8>> {
    match code {
        KeyCode::Char(c) => {
            if mods.contains(KeyModifiers::CONTROL) {
                let u = (c.to_ascii_uppercase() as u8).wrapping_sub(64);
                Some(vec![u & 0x1f])
            } else {
                Some(c.to_string().into_bytes())
            }
        }
        KeyCode::Enter => Some(vec![b'\r']),
        KeyCode::Backspace => Some(vec![0x7f]),
        KeyCode::Tab => Some(vec![b'\t']),
        KeyCode::Up => Some(b"\x1b[A".to_vec()),
        KeyCode::Down => Some(b"\x1b[B".to_vec()),
        KeyCode::Right => Some(b"\x1b[C".to_vec()),
        KeyCode::Left => Some(b"\x1b[D".to_vec()),
        _ => None,
    }
}

async fn send_frame<S>(write: &mut S, room: &fernet::Fernet, value: serde_json::Value)
where
    S: SinkExt<WsMsg> + Unpin,
{
    let ct = room.encrypt(value.to_string().as_bytes());
    let _ = write.send(WsMsg::Text(ct)).await;
}

/// Broadcast the current access-control list (owner + permitted drivers).
async fn broadcast_acl<S>(write: &mut S, room: &fernet::Fernet, app: &App)
where
    S: SinkExt<WsMsg> + Unpin,
{
    let drivers: Vec<&String> = app.drivers.iter().collect();
    send_frame(
        write,
        room,
        json!({"_perm":"acl","owner": app.owner, "drivers": drivers}),
    )
    .await;
}

pub async fn run(session: Session, theme: Theme) -> Result<()> {
    let ws = net::connect(&session).await?;
    let (mut write, read) = ws.split();
    let (tx, mut rx) = unbounded_channel::<Net>();
    let app_tx = tx.clone();
    tokio::spawn(net::reader(read, session.room.clone(), tx));

    // Broker-owned sandbox PTY output, and async-launch handoff.
    let (pty_tx, mut pty_rx): (UnboundedSender<Vec<u8>>, UnboundedReceiver<Vec<u8>>) =
        unbounded_channel();
    let (broker_tx, mut broker_rx) = unbounded_channel::<BrokerMsg>();
    let mut broker: Option<sbx::Sandbox> = None;
    let mut broker_meta: Option<(sbx::Backend, String)> = None;
    let mut launching = false;
    let mut announced_dims: Option<(u16, u16)> = None;

    enable_raw_mode()?;
    let mut stdout = std::io::stdout();
    execute!(stdout, EnterAlternateScreen)?;
    let mut term = Terminal::new(CrosstermBackend::new(stdout))?;

    let mut app = App::new(session.username.clone());
    let mut events = EventStream::new();
    let mut tick = tokio::time::interval(Duration::from_millis(120));

    let result = loop {
        if let Err(e) = term.draw(|f| ui::draw(f, &app, &theme)) {
            break Err(e.into());
        }

        // Broker: keep the PTY sized to our pane; broadcast size changes.
        if broker.is_some() {
            if let Ok(sz) = term.size() {
                let dims = sbx_dims(sz.width, sz.height);
                if announced_dims != Some(dims) {
                    announced_dims = Some(dims);
                    if let Some(sb) = &broker {
                        let _ = sb.resize(dims.0, dims.1);
                    }
                    send_frame(&mut write, &session.room,
                        json!({"_sbx":"resize","rows":dims.0,"cols":dims.1})).await;
                }
            }
        }

        tokio::select! {
            maybe = events.next() => {
                match maybe {
                    Some(Ok(Event::Key(k))) if k.kind == KeyEventKind::Press => {
                        if k.modifiers.contains(KeyModifiers::CONTROL)
                            && matches!(k.code, KeyCode::Char('q')) {
                            break Ok(());
                        }
                        if k.code == KeyCode::F(2) {
                            if app.sandbox.is_none() {
                                // nothing to drive
                            } else if app.can_drive() {
                                app.driving = !app.driving;
                            } else {
                                app.sys("you don't have drive permission — the owner can /grant you");
                            }
                        } else if app.driving {
                            if k.code == KeyCode::Esc {
                                app.driving = false;
                            } else if let Some(bytes) = key_to_pty(k.code, k.modifiers) {
                                send_frame(&mut write, &session.room,
                                    json!({"_sbx":"input","b64": STANDARD.encode(&bytes)})).await;
                            }
                        } else {
                            match k.code {
                                KeyCode::Esc => break Ok(()),
                                KeyCode::Enter => {
                                    let line = app.input.trim().to_string();
                                    app.input.clear();
                                    if let Some(rest) = line.strip_prefix("/sbx") {
                                        let mut p = rest.split_whitespace();
                                        match p.next() {
                                            Some("launch") => {
                                                if app.sandbox.is_some() || broker.is_some() || launching {
                                                    app.sys("a sandbox is already running");
                                                } else {
                                                    let backend = p.next()
                                                        .and_then(sbx::Backend::parse)
                                                        .unwrap_or(sbx::Backend::Local);
                                                    let image = p.next()
                                                        .map(str::to_string)
                                                        .unwrap_or_else(|| backend.default_image().to_string());
                                                    let sz = term.size().map(|s| (s.width, s.height)).unwrap_or((80, 24));
                                                    let (rows, cols) = sbx_dims(sz.0, sz.1);
                                                    launching = true;
                                                    app.sys(format!(
                                                        "summoning {} sandbox… (multipass boot can take ~30s)",
                                                        backend.label()));
                                                    spawn_launch(backend, image, rows, cols,
                                                        pty_tx.clone(), broker_tx.clone(), app_tx.clone());
                                                }
                                            }
                                            Some("stop") => {
                                                if let Some(mut sb) = broker.take() {
                                                    sb.stop();
                                                    if let Some((be, name)) = broker_meta.take() {
                                                        tokio::task::spawn_blocking(move || sbx::teardown(be, &name));
                                                    }
                                                    announced_dims = None;
                                                    send_frame(&mut write, &session.room,
                                                        json!({"_sbx":"status","state":"stopped"})).await;
                                                } else {
                                                    app.sys("you are not hosting a sandbox");
                                                }
                                            }
                                            _ => app.sys("usage: /sbx launch [local|docker|multipass] [image] | /sbx stop"),
                                        }
                                    } else if let Some(rest) = line.strip_prefix("/grant") {
                                        let target = rest.trim();
                                        if !app.is_owner() {
                                            app.sys("only the sandbox owner can /grant");
                                        } else if target.is_empty() {
                                            app.sys("usage: /grant <user>");
                                        } else {
                                            app.drivers.insert(target.to_string());
                                            broadcast_acl(&mut write, &session.room, &app).await;
                                            app.sys(format!("granted drive to {target}"));
                                        }
                                    } else if let Some(rest) = line.strip_prefix("/revoke") {
                                        let target = rest.trim();
                                        if !app.is_owner() {
                                            app.sys("only the sandbox owner can /revoke");
                                        } else if target == app.me {
                                            app.sys("the owner cannot revoke themselves");
                                        } else if target.is_empty() {
                                            app.sys("usage: /revoke <user>");
                                        } else {
                                            app.drivers.remove(target);
                                            broadcast_acl(&mut write, &session.room, &app).await;
                                            app.sys(format!("revoked drive from {target}"));
                                        }
                                    } else if !line.is_empty() && app.connected {
                                        let ct = session.room.encrypt(line.as_bytes());
                                        if write.send(WsMsg::Text(ct)).await.is_err() {
                                            app.connected = false;
                                        }
                                    }
                                }
                                KeyCode::Backspace => { app.input.pop(); }
                                KeyCode::Char(c) => app.input.push(c),
                                _ => {}
                            }
                        }
                    }
                    Some(Err(e)) => break Err(e.into()),
                    _ => {}
                }
            }
            net = rx.recv() => {
                match net {
                    Some(Net::SbxInput { from, bytes }) => {
                        // Broker authority: only honor input from a permitted driver
                        // (sender is server-authenticated via the message username).
                        if let Some(sb) = &mut broker {
                            if app.drivers.contains(&from) {
                                let _ = sb.write_input(&bytes);
                            }
                        }
                    }
                    Some(n) => app.apply(n),
                    None => break Ok(()),
                }
            }
            msg = broker_rx.recv() => {
                match msg {
                    Some(BrokerMsg::Ready { sb, backend, name, rows, cols }) => {
                        broker = Some(sb);
                        broker_meta = Some((backend, name));
                        announced_dims = Some((rows, cols));
                        launching = false;
                        // The launcher is the owner / superuser and the first driver.
                        app.owner = Some(app.me.clone());
                        app.drivers.clear();
                        app.drivers.insert(app.me.clone());
                        send_frame(&mut write, &session.room, json!({
                            "_sbx":"status","state":"ready",
                            "backend": backend.label(), "rows": rows, "cols": cols
                        })).await;
                        broadcast_acl(&mut write, &session.room, &app).await;
                    }
                    Some(BrokerMsg::Failed) => { launching = false; }
                    None => {}
                }
            }
            pty = pty_rx.recv() => {
                if let Some(bytes) = pty {
                    send_frame(&mut write, &session.room,
                        json!({"_sbx":"data","b64": STANDARD.encode(&bytes)})).await;
                }
            }
            _ = tick.tick() => {}
        }
    };

    if let Some(mut sb) = broker.take() {
        sb.stop();
        if let Some((be, name)) = broker_meta.take() {
            sbx::teardown(be, &name);
        }
    }
    disable_raw_mode()?;
    execute!(term.backend_mut(), LeaveAlternateScreen)?;
    term.show_cursor()?;
    result
}

/// Boot a sandbox off the UI thread (prepare → spawn PTY → hand back the handle).
fn spawn_launch(
    backend: sbx::Backend,
    image: String,
    rows: u16,
    cols: u16,
    pty_tx: UnboundedSender<Vec<u8>>,
    broker_tx: UnboundedSender<BrokerMsg>,
    app_tx: UnboundedSender<Net>,
) {
    tokio::spawn(async move {
        let name = SBX_NAME.to_string();
        let prep = {
            let (n, img) = (name.clone(), image.clone());
            tokio::task::spawn_blocking(move || sbx::prepare(backend, &n, &img)).await
        };
        if let Err(e) = prep.unwrap_or_else(|e| Err(anyhow::anyhow!("join: {e}"))) {
            let _ = app_tx.send(Net::Sys(format!("sandbox prepare failed: {e}")));
            let _ = broker_tx.send(BrokerMsg::Failed);
            return;
        }
        let (std_tx, std_rx) = std::sync::mpsc::channel::<Vec<u8>>();
        match sbx::Sandbox::launch(backend, &name, &image, rows, cols, std_tx) {
            Ok(sb) => {
                std::thread::spawn(move || {
                    while let Ok(b) = std_rx.recv() {
                        if pty_tx.send(b).is_err() {
                            break;
                        }
                    }
                });
                let _ = broker_tx.send(BrokerMsg::Ready { sb, backend, name, rows, cols });
            }
            Err(e) => {
                let _ = app_tx.send(Net::Sys(format!("sandbox launch failed: {e}")));
                let _ = broker_tx.send(BrokerMsg::Failed);
            }
        }
    });
}
