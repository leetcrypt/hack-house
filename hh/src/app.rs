//! TUI application state, network event model, and the async run loop.

use crate::ft;
use crate::net::{self, Session};
use crate::sbx;
use crate::theme::Theme;
use crate::ui;
use anyhow::Result;
use base64::engine::general_purpose::STANDARD;
use base64::Engine;
use crossterm::event::{
    DisableMouseCapture, EnableMouseCapture, Event, EventStream, KeyCode, KeyEventKind,
    KeyModifiers, MouseEventKind,
};
use crossterm::execute;
use crossterm::terminal::{
    disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen,
};
use futures_util::{SinkExt, StreamExt};
use ratatui::backend::CrosstermBackend;
use ratatui::Terminal;
use serde_json::json;
use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::mpsc::{unbounded_channel, UnboundedReceiver, UnboundedSender};
use tokio_tungstenite::tungstenite::Message as WsMsg;

const SBX_NAME: &str = "hack-house";

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

/// An in-progress incoming transfer we accepted.
struct Transfer {
    meta: ft::Offer,
    buf: Vec<u8>,
    accepted: bool,
}

/// An outgoing transfer awaiting / serving an accept.
struct ActiveSend {
    id: String,
    payload: Arc<Vec<u8>>,
    sending: bool,
}

/// Decoded events arriving from the websocket reader task.
pub enum Net {
    Init {
        lines: Vec<ChatLine>,
        users: Vec<User>,
    },
    Message(ChatLine),
    Roster {
        users: Vec<User>,
        capacity: usize,
    },
    Joined(String),
    Left(String),
    SbxStatus {
        backend: String,
        ready: bool,
        rows: u16,
        cols: u16,
    },
    SbxResize {
        rows: u16,
        cols: u16,
    },
    SbxData(Vec<u8>),
    SbxInput {
        from: String,
        bytes: Vec<u8>,
    },
    Perm {
        owner: String,
        drivers: Vec<String>,
        sudoers: Vec<String>,
    },
    Ft(ft::Ft),
    /// An AI agent is generating a reply (`on`) or has finished (`!on`).
    AiTyping {
        name: String,
        on: bool,
    },
    /// A local system notice produced off-thread (e.g. async Ollama probe).
    Sys(String),
    Err(String),
    Closed,
}

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
    pub owner: Option<String>,
    pub drivers: std::collections::HashSet<String>,
    /// Members whose VM unix account has sudo (superuser). Always includes owner.
    pub sudoers: std::collections::HashSet<String>,
    pub pending_offer: Option<ft::Offer>,
    transfers: HashMap<String, Transfer>,
    /// Chat scrollback: lines scrolled up from the live bottom (0 = following).
    pub chat_scroll: usize,
    /// Sandbox terminal scrollback: rows scrolled up from the bottom.
    pub sbx_scroll: usize,
    /// Whether the help overlay is showing.
    pub show_help: bool,
    /// Vertical scroll offset (rows) into the help overlay when it doesn't fit.
    pub help_scroll: u16,
    /// A reconnect handshake is in flight (Ctrl-R after a disconnect).
    pub reconnecting: bool,
    /// Transient error shown as a popup over the clergy (cleared on next keypress).
    pub error: Option<String>,
    /// The room password this client authenticated with (shown by `/pw`).
    pub password: String,
    /// AI agents currently generating a reply — drives the "thinking" spinner.
    pub ai_typing: std::collections::HashSet<String>,
    /// Monotonic tick counter used to animate the AI spinner.
    pub spin: usize,
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
            sudoers: std::collections::HashSet::new(),
            pending_offer: None,
            transfers: HashMap::new(),
            chat_scroll: 0,
            sbx_scroll: 0,
            show_help: false,
            help_scroll: 0,
            reconnecting: false,
            error: None,
            password: String::new(),
            ai_typing: std::collections::HashSet::new(),
            spin: 0,
        }
    }

    /// Append a chat line. Holds the viewport steady if scrolled up, and caps
    /// the in-memory backlog.
    fn push_line(&mut self, l: ChatLine) {
        self.lines.push(l);
        if self.chat_scroll > 0 {
            self.chat_scroll += 1;
        }
        const CAP: usize = 4000;
        if self.lines.len() > CAP {
            let drop = self.lines.len() - CAP;
            self.lines.drain(0..drop);
            self.chat_scroll = self.chat_scroll.min(self.lines.len().saturating_sub(1));
        }
    }

    pub fn is_owner(&self) -> bool {
        self.owner.as_deref() == Some(self.me.as_str())
    }
    pub fn can_drive(&self) -> bool {
        self.drivers.contains(&self.me)
    }

    fn sys(&mut self, text: impl Into<String>) {
        self.push_line(ChatLine {
            ts: String::new(),
            username: String::new(),
            text: text.into(),
            system: true,
        });
    }

    /// Surface an error: kept in chat scrollback for history AND shown as a
    /// popup over the clergy so it can't bleed onto / be overwritten at the
    /// input box. Dismissed by the next keypress.
    fn err(&mut self, text: impl Into<String>) {
        let t = text.into();
        self.sys(format!("✖ {t}"));
        self.error = Some(t);
    }

    fn apply(&mut self, n: Net) {
        match n {
            Net::Init { lines, users } => {
                self.lines = lines;
                self.users = users;
                self.connected = true;
                self.chat_scroll = 0;
                self.sys(format!("joined as {} ⛧", self.me));
                self.sys("/sbx launch · /drive (Esc releases) · /ai start · /ai <question> · /send <file> · /pw show password · PgUp/PgDn scroll chat · ctrl-q quit");
            }
            Net::Message(l) => self.push_line(l),
            Net::Roster { users, capacity } => {
                self.users = users;
                self.capacity = capacity;
            }
            Net::Joined(name) => self.sys(format!("{name} entered the house")),
            Net::Left(uid) => {
                if let Some(p) = self.users.iter().position(|u| u.user_id == uid) {
                    let name = self.users.remove(p).username;
                    self.ai_typing.remove(&name); // a departed agent isn't thinking
                    self.sys(format!("{name} left"));
                }
            }
            Net::AiTyping { name, on } => {
                if on {
                    self.ai_typing.insert(name);
                } else {
                    self.ai_typing.remove(&name);
                }
            }
            Net::SbxStatus {
                backend,
                ready,
                rows,
                cols,
            } => {
                if ready {
                    self.sandbox = Some(SbxView {
                        parser: vt100::Parser::new(rows.max(1), cols.max(1), 2000),
                        backend: backend.clone(),
                    });
                    self.sys(format!("⛧ sandbox summoned ({backend}) — F2 to drive"));
                } else {
                    self.sandbox = None;
                    self.driving = false;
                    self.sbx_scroll = 0;
                    self.owner = None;
                    self.drivers.clear();
                    self.sudoers.clear();
                    self.sys("⛧ sandbox dismissed");
                }
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
            Net::SbxInput { .. } => {} // broker enforces + writes in the run loop
            Net::Perm {
                owner,
                drivers,
                sudoers,
            } => {
                let new: std::collections::HashSet<String> = drivers.into_iter().collect();
                let sudo: std::collections::HashSet<String> = sudoers.into_iter().collect();
                if !owner.is_empty() && self.owner.as_deref() != Some(owner.as_str()) {
                    self.sys(format!("⛧ {owner} is the superuser (sandbox owner)"));
                }
                if new.contains(&self.me)
                    && !self.drivers.contains(&self.me)
                    && self.owner.is_some()
                {
                    self.sys("⛧ you were granted drive (F2 to take the shell)");
                } else if !new.contains(&self.me) && self.drivers.contains(&self.me) {
                    self.driving = false;
                    self.sys("⛧ your drive permission was revoked");
                }
                if sudo.contains(&self.me)
                    && !self.sudoers.contains(&self.me)
                    && self.owner.is_some()
                {
                    self.sys("⛧ you were granted sudo (superuser) in the VM");
                }
                self.owner = Some(owner).filter(|o| !o.is_empty());
                self.drivers = new;
                self.sudoers = sudo;
            }
            Net::Ft(_) => {} // handled in the run loop (needs out channel + disk)
            Net::Sys(t) => self.sys(t),
            Net::Err(t) => self.err(t),
            Net::Closed => {
                self.connected = false;
                self.sys("connection closed — press Ctrl-R to reconnect");
            }
        }
    }
}

fn sbx_dims(term_w: u16, term_h: u16) -> (u16, u16) {
    let body_h = term_h.saturating_sub(4);
    let sbx_h = (body_h as u32 * 55 / 100) as u16;
    (
        sbx_h.saturating_sub(2).max(1),
        term_w.saturating_sub(2).max(1),
    )
}

/// One page of sandbox scrollback = the visible grid height (defaults to 10 if
/// no sandbox is up). The run loop clamps `sbx_scroll` to the grid height anyway.
fn sbx_page(app: &App) -> usize {
    app.sandbox
        .as_ref()
        .map(|v| v.parser.screen().size().0 as usize)
        .unwrap_or(10)
        .max(1)
}

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

/// Queue an encrypted JSON frame for transmission (drained by the run loop).
fn send_frame(out: &UnboundedSender<WsMsg>, room: &fernet::Fernet, value: serde_json::Value) {
    let _ = out.send(WsMsg::Text(room.encrypt(value.to_string().as_bytes())));
}

fn broadcast_acl(out: &UnboundedSender<WsMsg>, room: &fernet::Fernet, app: &App) {
    let drivers: Vec<&String> = app.drivers.iter().collect();
    let sudoers: Vec<&String> = app.sudoers.iter().collect();
    send_frame(
        out,
        room,
        json!({
            "_perm":"acl","owner": app.owner, "drivers": drivers, "sudoers": sudoers
        }),
    );
}

/// Stream a payload to the clergy as `_ft` chunks (background, paced).
fn spawn_send(
    id: String,
    payload: Arc<Vec<u8>>,
    out: UnboundedSender<WsMsg>,
    room: Arc<fernet::Fernet>,
) {
    tokio::spawn(async move {
        for (seq, chunk) in payload.chunks(ft::CHUNK).enumerate() {
            let frame = json!({"_ft":"chunk","id": id,"seq": seq,"data": STANDARD.encode(chunk)});
            if out
                .send(WsMsg::Text(room.encrypt(frame.to_string().as_bytes())))
                .is_err()
            {
                return;
            }
            tokio::time::sleep(Duration::from_millis(2)).await;
        }
        send_frame(&out, &room, json!({"_ft":"done","id": id}));
    });
}

fn handle_ft(
    f: ft::Ft,
    app: &mut App,
    active: &mut Option<ActiveSend>,
    out: &UnboundedSender<WsMsg>,
    room: &Arc<fernet::Fernet>,
    downloads: &std::path::Path,
) {
    match f {
        ft::Ft::Offer(o) => {
            if o.from == app.me {
                return; // our own offer echo
            }
            app.sys(format!(
                "⛧ {} offers {} ({}{}) — /accept or /reject",
                o.from,
                o.name,
                ft::human(o.size as usize),
                if o.dir { ", directory" } else { "" }
            ));
            app.transfers.insert(
                o.id.clone(),
                Transfer {
                    meta: o.clone(),
                    buf: Vec::new(),
                    accepted: false,
                },
            );
            app.pending_offer = Some(o);
        }
        ft::Ft::Accept(id) => {
            if let Some(a) = active.as_mut() {
                if a.id == id && !a.sending {
                    a.sending = true;
                    spawn_send(id, a.payload.clone(), out.clone(), room.clone());
                    app.sys("transfer accepted — sending…");
                }
            }
        }
        ft::Ft::Reject(id) => {
            if active.as_ref().map(|a| a.id == id).unwrap_or(false) {
                app.sys("transfer rejected");
                *active = None;
            }
        }
        ft::Ft::Chunk { id, data } => {
            if let Some(t) = app.transfers.get_mut(&id) {
                if t.accepted {
                    t.buf.extend_from_slice(&data);
                }
            }
        }
        ft::Ft::Done(id) => {
            if let Some(t) = app.transfers.remove(&id) {
                if t.accepted {
                    if ft::sha256_hex(&t.buf) != t.meta.sha256 {
                        app.err(format!("{} — SHA-256 mismatch, discarded", t.meta.name));
                    } else {
                        match ft::save(downloads, &t.meta, &t.buf) {
                            Ok(p) => app.sys(format!(
                                "⛧ saved {} ({}) — verified ✓",
                                p.display(),
                                ft::human(t.buf.len())
                            )),
                            Err(e) => app.err(format!("save failed: {e}")),
                        }
                    }
                }
            }
            if app
                .pending_offer
                .as_ref()
                .map(|o| o.id == id)
                .unwrap_or(false)
            {
                app.pending_offer = None;
            }
        }
    }
}

/// Put the terminal back the way we found it: leave raw mode, leave the
/// alternate screen, stop mouse capture, show the cursor. Best-effort — every
/// step is independent so one failing (e.g. already-restored) can't strand the
/// rest. Safe to call more than once.
fn restore_terminal() {
    let _ = disable_raw_mode();
    let _ = execute!(std::io::stdout(), LeaveAlternateScreen, DisableMouseCapture);
    let _ = execute!(std::io::stdout(), crossterm::cursor::Show);
}

/// RAII: restores the terminal on drop. As long as a guard is alive, *any* exit
/// from `run` — normal return, `?` error, or a panic unwinding through the
/// frame — leaves the user's terminal (or tmux pane) usable instead of stuck in
/// raw/alt-screen mode.
struct TermGuard;
impl Drop for TermGuard {
    fn drop(&mut self) {
        restore_terminal();
    }
}

pub async fn run(params: net::ConnParams, mut session: Session, mut theme: Theme) -> Result<()> {
    let (tx, mut rx) = unbounded_channel::<Net>();
    let app_tx = tx.clone();
    let write = net::open(&session, tx.clone()).await?;
    // Carries the result of a background reconnect handshake back to the loop.
    let (recon_tx, mut recon_rx) = unbounded_channel::<std::result::Result<Session, String>>();

    // All outgoing frames funnel through here so background tasks (file chunks,
    // PTY relay) can transmit without owning the socket.
    let (out_tx, out_rx) = unbounded_channel::<WsMsg>();
    // The websocket writer runs on its own task so a slow / backpressured socket
    // (notably while relaying a sandbox PTY stream to a remote peer) can never
    // stall the UI loop's keyboard + chat handling. On reconnect the loop hands
    // the writer a fresh sink through `sink_tx`.
    let (sink_tx, sink_rx) = unbounded_channel::<net::WsSink>();
    tokio::spawn(writer_task(write, out_rx, sink_rx));
    let (pty_tx, mut pty_rx): (UnboundedSender<Vec<u8>>, UnboundedReceiver<Vec<u8>>) =
        unbounded_channel();
    let (broker_tx, mut broker_rx) = unbounded_channel::<BrokerMsg>();
    let mut broker: Option<sbx::Sandbox> = None;
    let mut broker_meta: Option<(sbx::Backend, String)> = None;
    let mut launching = false;
    let mut announced_dims: Option<(u16, u16)> = None;
    let mut active_send: Option<ActiveSend> = None;
    let mut send_seq: u64 = 0;
    // The local AI agent subprocess this client spawned via `/ai start`, if any.
    let mut agent: Option<std::process::Child> = None;
    let downloads = PathBuf::from("./downloads");

    enable_raw_mode()?;
    let mut stdout = std::io::stdout();
    execute!(stdout, EnterAlternateScreen, EnableMouseCapture)?;
    let mut term = Terminal::new(CrosstermBackend::new(stdout))?;

    // From here on the terminal is in raw/alt-screen mode. The guard restores it
    // on every exit path (return, `?`, panic); the panic hook additionally prints
    // the panic *after* restoring, so a crash leaves a readable message in the
    // pane instead of garbled raw-mode output.
    let _term_guard = TermGuard;
    let default_panic = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        restore_terminal();
        default_panic(info);
    }));

    // Graceful shutdown on signals: a `kill`, a closed tmux pane (SIGHUP), or a
    // Ctrl-C delivered while NOT in raw mode all break the loop cleanly so the
    // guard above can restore the terminal — no more being "booted" with a
    // broken pane. (In raw mode Ctrl-C arrives as a key event, handled below.)
    let mut sigterm = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
    let mut sighup = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::hangup())?;

    let mut app = App::new(session.username.clone());
    app.password = params.password.clone();
    let mut events = EventStream::new();
    let mut tick = tokio::time::interval(Duration::from_millis(50));

    let result = loop {
        // Apply the sandbox scrollback offset (0 = follow live).
        //
        // vt100 0.15.2 panics ("subtract with overflow" in grid::visible_rows)
        // if the scrollback offset ever exceeds the visible grid height: it does
        // `rows_len - offset` on usize without clamping. Cap our offset to the
        // grid height so a fast scroll can never cross that line and crash us.
        if let Some(v) = &mut app.sandbox {
            let rows = v.parser.screen().size().0 as usize;
            app.sbx_scroll = app.sbx_scroll.min(rows);
            v.parser.set_scrollback(app.sbx_scroll);
        }
        if let Err(e) = term.draw(|f| ui::draw(f, &app, &theme)) {
            break Err(e.into());
        }

        if broker.is_some() {
            if let Ok(sz) = term.size() {
                let dims = sbx_dims(sz.width, sz.height);
                if announced_dims != Some(dims) {
                    announced_dims = Some(dims);
                    if let Some(sb) = &broker {
                        let _ = sb.resize(dims.0, dims.1);
                    }
                    send_frame(
                        &out_tx,
                        &session.room,
                        json!({"_sbx":"resize","rows":dims.0,"cols":dims.1}),
                    );
                }
            }
        }

        tokio::select! {
            biased; // keyboard first, so Ctrl-C / Esc are never starved by output floods
            maybe = events.next() => {
                match maybe {
                    Some(Ok(Event::Key(k))) if k.kind == KeyEventKind::Press => {
                        app.error = None; // any keypress dismisses the error popup
                        // Ctrl-Q always quits. Ctrl-C quits too — *unless* we're
                        // driving the sandbox, where it must reach the PTY as an
                        // interrupt (handled in the `app.driving` branch below).
                        if k.modifiers.contains(KeyModifiers::CONTROL)
                            && (matches!(k.code, KeyCode::Char('q'))
                                || (matches!(k.code, KeyCode::Char('c')) && !app.driving))
                        {
                            break Ok(());
                        }
                        if k.modifiers.contains(KeyModifiers::CONTROL)
                            && matches!(k.code, KeyCode::Char('r'))
                            && !app.connected
                        {
                            // Reconnect: re-run the SRP handshake off-thread so the UI
                            // stays responsive, then re-attach the websocket on success.
                            if !app.reconnecting {
                                app.reconnecting = true;
                                app.sys("⛧ reconnecting…");
                                let p = params.clone();
                                let rtx = recon_tx.clone();
                                tokio::task::spawn_blocking(move || {
                                    let r = net::authenticate(
                                        &p.ip, p.port, &p.user, &p.password, p.no_tls, p.insecure,
                                    )
                                    .map_err(|e| e.to_string());
                                    let _ = rtx.send(r);
                                });
                            }
                        } else if app.show_help {
                            // While the help overlay is up, arrows / paging scroll it
                            // (when it's taller than the screen); only Esc closes, so
                            // stray keystrokes can't dismiss a menu you're still reading.
                            let max = term
                                .size()
                                .map(|s| ui::help_max_scroll(s.width, s.height, &theme))
                                .unwrap_or(0);
                            match k.code {
                                KeyCode::Up => app.help_scroll = app.help_scroll.saturating_sub(1),
                                KeyCode::Down => app.help_scroll = (app.help_scroll + 1).min(max),
                                KeyCode::PageUp => app.help_scroll = app.help_scroll.saturating_sub(10),
                                KeyCode::PageDown => app.help_scroll = (app.help_scroll + 10).min(max),
                                KeyCode::Home => app.help_scroll = 0,
                                KeyCode::End => app.help_scroll = max,
                                KeyCode::Esc => {
                                    app.show_help = false; // Esc dismisses the overlay
                                    app.help_scroll = 0;
                                }
                                _ => {} // ignore other keys so the menu stays put
                            }
                        } else if k.code == KeyCode::F(1) {
                            app.show_help = true;  // F1 from any mode
                            app.help_scroll = 0;
                        } else if k.code == KeyCode::F(2) {
                            if app.sandbox.is_none() {
                            } else if app.can_drive() {
                                app.driving = !app.driving;
                            } else {
                                app.sys("you don't have drive permission — the owner can /grant you");
                            }
                        } else if app.driving {
                            if k.code == KeyCode::Esc {
                                app.driving = false;
                            } else if k.code == KeyCode::PageUp {
                                // Scroll the shared shell's scrollback without releasing the
                                // drive: PgUp/PgDn aren't forwarded to the PTY anyway.
                                app.sbx_scroll = (app.sbx_scroll + sbx_page(&app)).min(2000);
                            } else if k.code == KeyCode::PageDown {
                                app.sbx_scroll = app.sbx_scroll.saturating_sub(sbx_page(&app));
                            } else if let Some(bytes) = key_to_pty(k.code, k.modifiers) {
                                if let Some(sb) = &mut broker {
                                    // I own the sandbox: write straight to the PTY — instant,
                                    // and Ctrl-C can't be queued behind outgoing output.
                                    let _ = sb.write_input(&bytes);
                                } else {
                                    send_frame(&out_tx, &session.room, json!({"_sbx":"input","b64": STANDARD.encode(&bytes)}));
                                }
                            }
                        } else {
                            match k.code {
                                KeyCode::Enter => {
                                    let line = app.input.trim().to_string();
                                    app.input.clear();
                                    app.chat_scroll = 0; // jump back to live on send
                                    handle_command(&line, &mut app, &mut theme, &mut active_send, &mut send_seq,
                                        &mut broker, &mut broker_meta, &mut launching, &mut announced_dims,
                                        &out_tx, &pty_tx, &broker_tx, &app_tx, &session, &term,
                                        &mut agent, &params);
                                }
                                KeyCode::Backspace => { app.input.pop(); }
                                // Scroll: ↑/↓ scroll the sandbox terminal if one is up,
                                // otherwise the chat. PgUp/PgDn always scroll chat.
                                KeyCode::Up => {
                                    if app.sandbox.is_some() {
                                        app.sbx_scroll = (app.sbx_scroll + 1).min(2000);
                                    } else {
                                        app.chat_scroll = (app.chat_scroll + 1).min(app.lines.len().saturating_sub(1));
                                    }
                                }
                                KeyCode::Down => {
                                    if app.sandbox.is_some() {
                                        app.sbx_scroll = app.sbx_scroll.saturating_sub(1);
                                    } else {
                                        app.chat_scroll = app.chat_scroll.saturating_sub(1);
                                    }
                                }
                                KeyCode::PageUp => {
                                    app.chat_scroll = (app.chat_scroll + 10).min(app.lines.len().saturating_sub(1));
                                }
                                KeyCode::PageDown => {
                                    app.chat_scroll = app.chat_scroll.saturating_sub(10);
                                }
                                KeyCode::Home => {
                                    app.chat_scroll = app.lines.len().saturating_sub(1);
                                }
                                KeyCode::End => {
                                    app.chat_scroll = 0;
                                    app.sbx_scroll = 0;
                                }
                                KeyCode::Char(c) => app.input.push(c),
                                _ => {}
                            }
                        }
                    }
                    Some(Ok(Event::Mouse(m))) => {
                        // Mouse wheel scrolls the sandbox terminal if one is up
                        // (incl. while driving), otherwise the chat — mirrors ↑/↓.
                        match m.kind {
                            MouseEventKind::ScrollUp => {
                                if app.sandbox.is_some() {
                                    app.sbx_scroll = (app.sbx_scroll + 3).min(2000);
                                } else {
                                    app.chat_scroll = (app.chat_scroll + 3).min(app.lines.len().saturating_sub(1));
                                }
                            }
                            MouseEventKind::ScrollDown => {
                                if app.sandbox.is_some() {
                                    app.sbx_scroll = app.sbx_scroll.saturating_sub(3);
                                } else {
                                    app.chat_scroll = app.chat_scroll.saturating_sub(3);
                                }
                            }
                            _ => {}
                        }
                    }
                    Some(Err(e)) => break Err(e.into()),
                    _ => {}
                }
            }
            net = rx.recv() => {
                match net {
                    Some(Net::SbxInput { from, bytes }) => {
                        if let Some(sb) = &mut broker {
                            if app.drivers.contains(&from) {
                                let _ = sb.write_input(&bytes);
                            }
                        }
                    }
                    Some(Net::Ft(f)) => handle_ft(f, &mut app, &mut active_send, &out_tx, &session.room, &downloads),
                    // The broker renders its sandbox locally from the PTY, so it
                    // ignores its own echoed status/data; everyone else uses them.
                    Some(Net::SbxData(b)) => {
                        if broker.is_none() {
                            if let Some(v) = &mut app.sandbox { v.parser.process(&b); }
                        }
                    }
                    Some(Net::SbxStatus { .. }) if broker.is_some() => {}
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
                        // Local sandbox view — broker renders straight from the PTY.
                        app.sandbox = Some(SbxView {
                            parser: vt100::Parser::new(rows.max(1), cols.max(1), 2000),
                            backend: backend.label().to_string(),
                        });
                        app.sys(format!("⛧ sandbox summoned ({}) — /drive to take the shell", backend.label()));
                        app.owner = Some(app.me.clone());
                        app.drivers.clear();
                        app.drivers.insert(app.me.clone());
                        app.sudoers.clear();
                        app.sudoers.insert(app.me.clone()); // owner = superuser
                        send_frame(&out_tx, &session.room, json!({
                            "_sbx":"status","state":"ready","backend": backend.label(), "rows": rows, "cols": cols
                        }));
                        broadcast_acl(&out_tx, &session.room, &app);
                    }
                    Some(BrokerMsg::Failed) => { launching = false; }
                    None => {}
                }
            }
            recon = recon_rx.recv() => {
                if let Some(result) = recon {
                    app.reconnecting = false;
                    match result {
                        Ok(s) => {
                            session = s;
                            match net::open(&session, tx.clone()).await {
                                Ok(w) => {
                                    let _ = sink_tx.send(w);
                                    app.sys("⛧ websocket re-attached — syncing…");
                                    // If we host the sandbox, re-announce it so the
                                    // rest of the house re-syncs the shared shell.
                                    if let Some((be, _)) = &broker_meta {
                                        if let Some(v) = &app.sandbox {
                                            let (rows, cols) = v.parser.screen().size();
                                            send_frame(&out_tx, &session.room, json!({
                                                "_sbx":"status","state":"ready",
                                                "backend": be.label(),"rows": rows,"cols": cols
                                            }));
                                            broadcast_acl(&out_tx, &session.room, &app);
                                        }
                                    }
                                }
                                Err(e) => app.err(format!("reconnect failed: {e}")),
                            }
                        }
                        Err(e) => app.sys(format!("reconnect failed: {e}")),
                    }
                }
            }
            pty = pty_rx.recv() => {
                if let Some(mut bytes) = pty {
                    // Coalesce a burst (e.g. `tree`) into one frame: fewer round-trips,
                    // no flood. Render locally now so the owner sees output instantly.
                    while let Ok(more) = pty_rx.try_recv() {
                        bytes.extend_from_slice(&more);
                        if bytes.len() > 256 * 1024 { break; }
                    }
                    if let Some(v) = &mut app.sandbox { v.parser.process(&bytes); }
                    send_frame(&out_tx, &session.room, json!({"_sbx":"data","b64": STANDARD.encode(&bytes)}));
                }
            }
            _ = sigterm.recv() => { break Ok(()); }
            _ = sighup.recv() => { break Ok(()); }
            _ = tick.tick() => { app.spin = app.spin.wrapping_add(1); }
        }
    };

    if let Some(mut sb) = broker.take() {
        sb.stop();
        if let Some((be, name)) = broker_meta.take() {
            sbx::teardown(be, &name);
        }
    }
    if let Some(mut child) = agent.take() {
        let _ = child.kill();
        let _ = child.wait();
    }
    disable_raw_mode()?;
    execute!(
        term.backend_mut(),
        LeaveAlternateScreen,
        DisableMouseCapture
    )?;
    term.show_cursor()?;
    result
}

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

/// Owns the websocket write half and drains all outgoing frames off the UI
/// loop. Because `sink.send().await` can block on a backpressured socket (e.g.
/// while the server relays a sandbox PTY stream to a slow remote peer), keeping
/// it here means that stall never starves keyboard input or chat rendering. A
/// reconnect delivers a fresh sink via `sink_rx`; the reader task independently
/// surfaces `Net::Closed`, so a dead sink here just drops frames until then.
async fn writer_task(
    mut sink: net::WsSink,
    mut out_rx: UnboundedReceiver<WsMsg>,
    mut sink_rx: UnboundedReceiver<net::WsSink>,
) {
    loop {
        tokio::select! {
            biased;
            // Swap in a reconnect's fresh sink before draining more frames.
            new_sink = sink_rx.recv() => {
                match new_sink {
                    Some(s) => sink = s,
                    None => {}
                }
            }
            msg = out_rx.recv() => {
                let Some(first) = msg else { return }; // app exiting
                // Coalesce a burst (file chunks / PTY relay) into one batch.
                let mut batch = vec![first];
                while let Ok(m) = out_rx.try_recv() {
                    batch.push(m);
                    if batch.len() >= 64 { break; }
                }
                for m in batch {
                    if sink.send(m).await.is_err() {
                        break; // sink dead — wait for a reconnect to replace it
                    }
                }
            }
        }
    }
}

#[allow(clippy::too_many_arguments)]
fn handle_command(
    line: &str,
    app: &mut App,
    theme: &mut Theme,
    active_send: &mut Option<ActiveSend>,
    send_seq: &mut u64,
    broker: &mut Option<sbx::Sandbox>,
    broker_meta: &mut Option<(sbx::Backend, String)>,
    launching: &mut bool,
    announced_dims: &mut Option<(u16, u16)>,
    out_tx: &UnboundedSender<WsMsg>,
    pty_tx: &UnboundedSender<Vec<u8>>,
    broker_tx: &UnboundedSender<BrokerMsg>,
    app_tx: &UnboundedSender<Net>,
    session: &Session,
    term: &Terminal<CrosstermBackend<std::io::Stdout>>,
    agent: &mut Option<std::process::Child>,
    params: &net::ConnParams,
) {
    let room = &session.room;
    if line == "/help" || line == "/?" {
        app.show_help = true;
        app.help_scroll = 0;
    } else if line == "/pw" || line == "/password" {
        // Show the room password locally (never broadcast). Handy when the
        // server's password was autogenerated and you need to read it off / share
        // it out-of-band to invite someone into the room.
        if app.password.is_empty() {
            app.sys("⛧ no room password (joined without one)");
        } else {
            app.sys(format!("⛧ room password: {}", app.password));
        }
    } else if let Some(rest) = line.strip_prefix("/theme") {
        // Live vestment switch: `/theme <name>`, or bare `/theme` to list options.
        let name = rest.trim();
        if name.is_empty() {
            app.sys(format!(
                "vestments: {} — /theme <name>",
                Theme::available().join(" · ")
            ));
        } else {
            match Theme::by_name(name) {
                Ok(t) => {
                    *theme = t;
                    app.sys(format!("donned the {name} vestments"));
                }
                Err(_) => app.err(format!(
                    "no theme '{name}' — try: {}",
                    Theme::available().join(" · ")
                )),
            }
        }
    } else if line == "/drive" {
        // Mobile-friendly alternative to F2 (no function key needed).
        if app.sandbox.is_none() {
            app.sys("no sandbox running — /sbx launch first");
        } else if app.can_drive() {
            app.driving = true;
            app.sys("⛧ drive mode ON — type into the shell · press Esc to release");
        } else {
            app.sys("you don't have drive permission — the owner can /grant you");
        }
    } else if let Some(path) = line
        .strip_prefix("/sendd ")
        .or_else(|| line.strip_prefix("/send "))
    {
        let path = path.trim();
        match ft::read_payload(path) {
            Ok((name, bytes, dir)) => {
                *send_seq += 1;
                let id = format!("{}-{}", app.me, send_seq);
                let (size, sha) = (bytes.len(), ft::sha256_hex(&bytes));
                *active_send = Some(ActiveSend {
                    id: id.clone(),
                    payload: Arc::new(bytes),
                    sending: false,
                });
                send_frame(
                    out_tx,
                    room,
                    json!({
                        "_ft":"offer","id": id,"name": name,"size": size,"sha256": sha,"dir": dir
                    }),
                );
                app.sys(format!(
                    "offered {} ({}) — waiting for an /accept",
                    name,
                    ft::human(size)
                ));
            }
            Err(e) => app.err(format!("send failed: {e}")),
        }
    } else if line == "/accept" {
        if let Some(o) = app.pending_offer.take() {
            send_frame(out_tx, room, json!({"_ft":"accept","id": o.id}));
            if let Some(t) = app.transfers.get_mut(&o.id) {
                t.accepted = true;
            }
            app.sys(format!("accepting {}…", o.name));
        } else {
            app.sys("no pending offer");
        }
    } else if line == "/reject" {
        if let Some(o) = app.pending_offer.take() {
            send_frame(out_tx, room, json!({"_ft":"reject","id": o.id}));
            app.transfers.remove(&o.id);
            app.sys("rejected the offer");
        } else {
            app.sys("no pending offer");
        }
    } else if let Some(rest) = line.strip_prefix("/sbx") {
        let mut p = rest.split_whitespace();
        match p.next() {
            Some("launch") => {
                if app.sandbox.is_some() || broker.is_some() || *launching {
                    app.sys("a sandbox is already running");
                } else {
                    // `--start` (alias `--start-daemon` / `-y`) opts in to booting
                    // a stopped Docker daemon; everything else is positional.
                    let args: Vec<&str> = p.collect();
                    let start_daemon = args
                        .iter()
                        .any(|a| matches!(*a, "--start" | "--start-daemon" | "-y"));
                    let mut pos = args.iter().copied().filter(|a| !a.starts_with('-'));
                    let backend = pos
                        .next()
                        .and_then(sbx::Backend::parse)
                        .unwrap_or(sbx::Backend::Local);
                    let image = pos
                        .next()
                        .map(str::to_string)
                        .unwrap_or_else(|| backend.default_image().to_string());

                    if backend == sbx::Backend::Docker && !start_daemon && !sbx::docker_daemon_up()
                    {
                        app.err("docker daemon is not running — retry with `/sbx launch docker --start` to boot it (sudo), or run ./ensure-docker.sh in a terminal first");
                    } else {
                        let sz = term.size().map(|s| (s.width, s.height)).unwrap_or((80, 24));
                        let (rows, cols) = sbx_dims(sz.0, sz.1);
                        *launching = true;
                        let members: Vec<String> =
                            app.users.iter().map(|u| u.username.clone()).collect();
                        app.sys(format!(
                            "summoning {} sandbox… (provisioning unix users; multipass boot ~30s)",
                            backend.label()
                        ));
                        spawn_launch(
                            backend,
                            image,
                            app.me.clone(),
                            members,
                            rows,
                            cols,
                            start_daemon,
                            pty_tx.clone(),
                            broker_tx.clone(),
                            app_tx.clone(),
                        );
                    }
                }
            }
            Some("stop") => {
                if let Some(mut sb) = broker.take() {
                    sb.stop();
                    if let Some((be, name)) = broker_meta.take() {
                        tokio::task::spawn_blocking(move || sbx::teardown(be, &name));
                    }
                    *announced_dims = None;
                    send_frame(out_tx, room, json!({"_sbx":"status","state":"stopped"}));
                } else {
                    app.sys("you are not hosting a sandbox");
                }
            }
            _ => app.sys("usage: /sbx launch [local|docker|multipass] [image] | /sbx stop"),
        }
    } else if let Some(rest) = line.strip_prefix("/unsudo") {
        let target = rest.trim();
        if !app.is_owner() {
            app.sys("only the owner can /unsudo");
        } else if target.is_empty() {
            app.sys("usage: /unsudo <user>");
        } else if let Some((be, name)) = broker_meta.clone() {
            app.sudoers.remove(target);
            let (t, n) = (target.to_string(), name);
            tokio::task::spawn_blocking(move || sbx::set_sudo(be, &n, &t, false));
            broadcast_acl(out_tx, room, app);
            app.sys(format!("revoked sudo from {target} in the VM"));
        } else {
            app.sys("no sandbox running");
        }
    } else if let Some(rest) = line.strip_prefix("/sudo") {
        let target = rest.trim();
        if !app.is_owner() {
            app.sys("only the owner can delegate sudo");
        } else if target.is_empty() {
            app.sys("usage: /sudo <user>  (delegate VM superuser) | /unsudo <user>");
        } else if let Some((be, name)) = broker_meta.clone() {
            app.sudoers.insert(target.to_string());
            let (t, n) = (target.to_string(), name);
            tokio::task::spawn_blocking(move || sbx::set_sudo(be, &n, &t, true));
            broadcast_acl(out_tx, room, app);
            app.sys(format!("delegated VM superuser (sudo) to {target}"));
        } else {
            app.sys("no sandbox running");
        }
    } else if let Some(rest) = line.strip_prefix("/grant") {
        let target = rest.trim();
        if !app.is_owner() {
            app.sys("only the sandbox owner can /grant");
        } else if target.is_empty() {
            app.sys("usage: /grant <user>");
        } else {
            app.drivers.insert(target.to_string());
            broadcast_acl(out_tx, room, app);
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
            broadcast_acl(out_tx, room, app);
            app.sys(format!("revoked drive from {target}"));
        }
    } else if line == "/ai stop" {
        // Reap a child that already exited (e.g. failed auth) so the message is honest.
        if agent
            .as_mut()
            .is_some_and(|c| matches!(c.try_wait(), Ok(Some(_))))
        {
            *agent = None;
        }
        if let Some(mut child) = agent.take() {
            let _ = child.kill();
            let _ = child.wait();
            app.sys("⛧ dismissed the AI agent");
        } else {
            app.sys("no AI agent was started from this client");
        }
    } else if let Some(rest) = line
        .strip_prefix("/ai start")
        .filter(|r| r.is_empty() || r.starts_with(' '))
    {
        // Drop a handle to an agent that has already exited so we can restart.
        if agent
            .as_mut()
            .is_some_and(|c| matches!(c.try_wait(), Ok(Some(_))))
        {
            *agent = None;
        }
        if agent.is_some() {
            app.sys("an AI agent is already running from this client — /ai stop first");
        } else {
            let arg = rest.trim();
            // A bare name (no ':' tag, no '/' path) is a models.toml profile;
            // anything else is treated as a literal Ollama model tag.
            let (profile, model): (Option<&str>, &str) = if arg.is_empty() {
                (None, "qwen2.5:3b")
            } else if arg.contains(':') || arg.contains('/') {
                (None, arg)
            } else {
                (Some(arg), arg)
            };
            let name = "oracle";
            match spawn_agent(params, &app.password, name, profile, model) {
                Ok(child) => {
                    *agent = Some(child);
                    let desc = match profile {
                        Some(p) => format!("profile {p}"),
                        None => format!("ollama/{model}"),
                    };
                    app.sys(format!(
                        "⛧ summoning {name} ({desc})… it will announce when online"
                    ));
                }
                Err(e) => app.err(format!("/ai start failed: {e}")),
            }
        }
    } else if line == "/ai list" || line == "/ai models" {
        // Reap an agent that already exited so we don't forward into a dead pipe.
        if agent
            .as_mut()
            .is_some_and(|c| matches!(c.try_wait(), Ok(Some(_))))
        {
            *agent = None;
        }
        if agent.is_some() {
            // A live agent answers these itself (canned, zero model-call).
            let _ = out_tx.send(WsMsg::Text(room.encrypt(line.as_bytes())));
        } else if line == "/ai list" {
            app.sys("no AI agent running from this client — /ai start to summon one");
        } else {
            // No agent: still useful to show what could be started locally.
            app.sys("querying local ollama…");
            let tx = app_tx.clone();
            tokio::task::spawn_blocking(move || {
                let msg = match local_ollama_models() {
                    Ok(ms) if !ms.is_empty() => format!(
                        "local ollama models (start one with `/ai start <name>`): {}",
                        ms.join(", ")
                    ),
                    Ok(_) => "ollama is reachable but has no models pulled — \
                              `ollama pull qwen2.5:3b` or run ./bootstrap-ai.sh"
                        .to_string(),
                    Err(_) => "ollama not reachable at localhost:11434 — run \
                               ./bootstrap-ai.sh, or `/ai start <profile>` for a cloud model"
                        .to_string(),
                };
                let _ = tx.send(Net::Sys(msg));
            });
        }
    } else if !line.is_empty() && app.connected {
        let _ = out_tx.send(WsMsg::Text(room.encrypt(line.as_bytes())));
    }
}

/// Probe the local Ollama daemon for installed model tags. Used to answer
/// `/ai models` before any agent is summoned (the agentless path); a running
/// agent answers in-room instead. Honors `$OLLAMA_HOST`.
fn local_ollama_models() -> Result<Vec<String>, String> {
    let host = std::env::var("OLLAMA_HOST")
        .ok()
        .filter(|h| !h.is_empty())
        .unwrap_or_else(|| "http://localhost:11434".to_string());
    let host = host.trim_end_matches('/');
    let url = format!("{host}/api/tags");
    let client = reqwest::blocking::Client::builder()
        .timeout(std::time::Duration::from_millis(1500))
        .build()
        .map_err(|e| e.to_string())?;
    let body: serde_json::Value = client
        .get(&url)
        .send()
        .map_err(|e| e.to_string())?
        .json()
        .map_err(|e| e.to_string())?;
    let models = body
        .get("models")
        .and_then(|m| m.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|m| m.get("name").and_then(|n| n.as_str()))
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default();
    Ok(models)
}

#[allow(clippy::too_many_arguments)]
fn spawn_launch(
    backend: sbx::Backend,
    image: String,
    owner: String,
    members: Vec<String>,
    rows: u16,
    cols: u16,
    start_daemon: bool,
    pty_tx: UnboundedSender<Vec<u8>>,
    broker_tx: UnboundedSender<BrokerMsg>,
    app_tx: UnboundedSender<Net>,
) {
    tokio::spawn(async move {
        let name = SBX_NAME.to_string();
        let prep = {
            let (n, img) = (name.clone(), image.clone());
            tokio::task::spawn_blocking(move || sbx::prepare(backend, &n, &img, start_daemon)).await
        };
        if let Err(e) = prep.unwrap_or_else(|e| Err(anyhow::anyhow!("join: {e}"))) {
            let _ = app_tx.send(Net::Err(format!("sandbox prepare failed: {e}")));
            let _ = broker_tx.send(BrokerMsg::Failed);
            return;
        }
        // Provision real unix accounts (owner = sudoer) → the shell's run-user.
        let run_user = {
            let (n, o, ms) = (name.clone(), owner.clone(), members.clone());
            tokio::task::spawn_blocking(move || sbx::provision(backend, &n, &o, &ms))
                .await
                .unwrap_or_default()
        };
        let (std_tx, std_rx) = std::sync::mpsc::channel::<Vec<u8>>();
        match sbx::Sandbox::launch(backend, &name, &run_user, rows, cols, std_tx) {
            Ok(sb) => {
                std::thread::spawn(move || {
                    while let Ok(b) = std_rx.recv() {
                        if pty_tx.send(b).is_err() {
                            break;
                        }
                    }
                });
                let _ = broker_tx.send(BrokerMsg::Ready {
                    sb,
                    backend,
                    name,
                    rows,
                    cols,
                });
            }
            Err(e) => {
                let _ = app_tx.send(Net::Err(format!("sandbox launch failed: {e}")));
                let _ = broker_tx.send(BrokerMsg::Failed);
            }
        }
    });
}

/// Locate the repo root (the dir containing `cmd_chat/agent/`) by walking up from
/// the executable's path and the current directory — so `/ai start` finds the
/// Python agent whether the client runs from the checkout or its `target/` dir.
fn find_repo_root() -> Option<std::path::PathBuf> {
    let mut starts: Vec<std::path::PathBuf> = Vec::new();
    if let Ok(exe) = std::env::current_exe() {
        starts.push(exe);
    }
    if let Ok(cwd) = std::env::current_dir() {
        starts.push(cwd);
    }
    for start in starts {
        let mut dir: Option<&std::path::Path> = Some(start.as_path());
        while let Some(d) = dir {
            if d.join("cmd_chat").join("agent").is_dir() {
                return Some(d.to_path_buf());
            }
            dir = d.parent();
        }
    }
    None
}

/// Spawn the Python AI agent as a child process that joins this room as a normal
/// encrypted client (same SRP + room password). Returns the child handle so
/// `/ai stop` (and client quit) can kill it. The agent's stdout/stderr go to a
/// log file in the temp dir so its prints never corrupt the TUI.
fn spawn_agent(
    params: &net::ConnParams,
    password: &str,
    name: &str,
    profile: Option<&str>,
    model: &str,
) -> std::result::Result<std::process::Child, String> {
    use std::process::{Command, Stdio};
    let root = find_repo_root().ok_or_else(|| {
        "can't locate the repo (cmd_chat/) — run the client from the checkout".to_string()
    })?;
    // Prefer the project venv's interpreter; fall back to HH_AI_PYTHON or python3.
    let venv_py = root.join(".venv/bin/python");
    let program = if venv_py.is_file() {
        venv_py
    } else {
        std::path::PathBuf::from(std::env::var("HH_AI_PYTHON").unwrap_or_else(|_| "python3".into()))
    };
    let log_path = std::env::temp_dir().join(format!("hh-agent-{name}.log"));
    let log = std::fs::File::create(&log_path)
        .map_err(|e| format!("agent log {}: {e}", log_path.display()))?;
    let log_err = log.try_clone().map_err(|e| e.to_string())?;
    let mut cmd = Command::new(&program);
    cmd.current_dir(&root)
        .arg("-m")
        .arg("cmd_chat.agent")
        .arg(&params.ip)
        .arg(params.port.to_string())
        .arg("--name")
        .arg(name);
    // A profile carries its own provider/model/endpoint from models.toml;
    // otherwise summon a local Ollama model by tag.
    match profile {
        Some(p) => {
            cmd.arg("--profile").arg(p);
        }
        None => {
            cmd.arg("--provider").arg("ollama").arg("--model").arg(model);
        }
    }
    cmd.stdin(Stdio::null())
        .stdout(Stdio::from(log))
        .stderr(Stdio::from(log_err));
    if !password.is_empty() {
        cmd.arg("--password").arg(password);
    }
    if params.no_tls {
        cmd.arg("--no-tls");
    }
    if params.insecure {
        cmd.arg("--insecure");
    }
    cmd.spawn()
        .map_err(|e| format!("could not start agent ({}): {e}", program.display()))
}
