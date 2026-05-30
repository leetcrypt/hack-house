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
use tokio::sync::mpsc::{unbounded_channel, UnboundedReceiver};
use tokio_tungstenite::tungstenite::Message as WsMsg;

pub const SBX_ROWS: u16 = 24;
pub const SBX_COLS: u16 = 80;

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
    SbxStatus { backend: String, ready: bool },
    SbxData(Vec<u8>),
    SbxInput(Vec<u8>),
    Closed,
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
        }
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
            Net::SbxStatus { backend, ready } => {
                if ready {
                    self.sandbox = Some(SbxView {
                        parser: vt100::Parser::new(SBX_ROWS, SBX_COLS, 0),
                        backend: backend.clone(),
                    });
                    self.sys(format!("⛧ sandbox summoned ({backend}) — F2 to drive"));
                } else {
                    self.sandbox = None;
                    self.driving = false;
                    self.sys("⛧ sandbox dismissed");
                }
            }
            Net::SbxData(bytes) => {
                if let Some(v) = &mut self.sandbox {
                    v.parser.process(&bytes);
                }
            }
            // Broker writes input to the PTY in the run loop; non-brokers ignore.
            Net::SbxInput(_) => {}
            Net::Closed => {
                self.connected = false;
                self.sys("connection closed");
            }
        }
    }
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

/// Encrypt and send a JSON application frame over the chat channel.
async fn send_frame<S>(write: &mut S, room: &fernet::Fernet, value: serde_json::Value)
where
    S: SinkExt<WsMsg> + Unpin,
{
    let ct = room.encrypt(value.to_string().as_bytes());
    let _ = write.send(WsMsg::Text(ct)).await;
}

pub async fn run(session: Session, theme: Theme) -> Result<()> {
    let ws = net::connect(&session).await?;
    let (mut write, read) = ws.split();
    let (tx, mut rx) = unbounded_channel::<Net>();
    tokio::spawn(net::reader(read, session.room.clone(), tx));

    // PTY output from a broker-owned sandbox (set on /sbx launch).
    let (pty_tx, mut pty_rx): (_, UnboundedReceiver<Vec<u8>>) = unbounded_channel();
    let mut broker: Option<sbx::Sandbox> = None;

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

        tokio::select! {
            maybe = events.next() => {
                match maybe {
                    Some(Ok(Event::Key(k))) if k.kind == KeyEventKind::Press => {
                        // Global quit.
                        if k.modifiers.contains(KeyModifiers::CONTROL)
                            && matches!(k.code, KeyCode::Char('q')) {
                            break Ok(());
                        }
                        // F2 toggles drive (only meaningful with a live sandbox).
                        if k.code == KeyCode::F(2) {
                            if app.sandbox.is_some() {
                                app.driving = !app.driving;
                            }
                        } else if app.driving {
                            // Drive mode: keystrokes go to the sandbox PTY.
                            if k.code == KeyCode::Esc {
                                app.driving = false;
                            } else if let Some(bytes) = key_to_pty(k.code, k.modifiers) {
                                send_frame(&mut write, &session.room,
                                    json!({"_sbx":"input","b64": STANDARD.encode(&bytes)})).await;
                            }
                        } else {
                            // Chat / command mode.
                            match k.code {
                                KeyCode::Esc => break Ok(()),
                                KeyCode::Enter => {
                                    let line = app.input.trim().to_string();
                                    app.input.clear();
                                    if line.starts_with("/sbx") {
                                        handle_sbx_cmd(&line, &mut app, &mut broker,
                                            &pty_tx, &mut write, &session.room).await;
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
                    Some(Net::SbxInput(b)) => {
                        if let Some(sb) = &mut broker { let _ = sb.write_input(&b); }
                    }
                    Some(n) => app.apply(n),
                    None => break Ok(()),
                }
            }
            pty = pty_rx.recv() => {
                if let Some(bytes) = pty {
                    // Broker relays sandbox output to the whole coven (encrypted).
                    send_frame(&mut write, &session.room,
                        json!({"_sbx":"data","b64": STANDARD.encode(&bytes)})).await;
                }
            }
            _ = tick.tick() => {}
        }
    };

    if let Some(mut sb) = broker.take() {
        sb.stop();
    }
    disable_raw_mode()?;
    execute!(term.backend_mut(), LeaveAlternateScreen)?;
    term.show_cursor()?;
    result
}

async fn handle_sbx_cmd<S>(
    line: &str,
    app: &mut App,
    broker: &mut Option<sbx::Sandbox>,
    pty_tx: &tokio::sync::mpsc::UnboundedSender<Vec<u8>>,
    write: &mut S,
    room: &fernet::Fernet,
) where
    S: SinkExt<WsMsg> + Unpin,
{
    let mut parts = line.split_whitespace();
    let _ = parts.next(); // "/sbx"
    match parts.next() {
        Some("launch") => {
            if app.sandbox.is_some() || broker.is_some() {
                app.sys("a sandbox is already running");
                return;
            }
            let backend = parts
                .next()
                .and_then(sbx::Backend::parse)
                .unwrap_or(sbx::Backend::Local);
            let image = parts.next().unwrap_or("ubuntu:24.04");
            let (std_tx, std_rx) = std::sync::mpsc::channel::<Vec<u8>>();
            match sbx::Sandbox::launch(backend, "house", image, SBX_ROWS, SBX_COLS, std_tx) {
                Ok(sb) => {
                    *broker = Some(sb);
                    let ptx = pty_tx.clone();
                    std::thread::spawn(move || {
                        while let Ok(b) = std_rx.recv() {
                            if ptx.send(b).is_err() {
                                break;
                            }
                        }
                    });
                    send_frame(
                        write,
                        room,
                        json!({"_sbx":"status","state":"ready","backend": backend.label()}),
                    )
                    .await;
                    app.sys(format!("summoning {} sandbox…", backend.label()));
                }
                Err(e) => app.sys(format!("sandbox launch failed: {e}")),
            }
        }
        Some("stop") => {
            if let Some(mut sb) = broker.take() {
                sb.stop();
                send_frame(write, room, json!({"_sbx":"status","state":"stopped"})).await;
            } else {
                app.sys("you are not hosting a sandbox");
            }
        }
        _ => app.sys("usage: /sbx launch [local|docker|multipass] [image] | /sbx stop"),
    }
}
