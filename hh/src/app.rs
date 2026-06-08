//! TUI application state, network event model, and the async run loop.

use crate::ft;
use crate::layout::{Dir, Layout, Resize};
use crate::net::{self, Session};
use crate::sbx;
use crate::theme::Theme;
use crate::ui;
use anyhow::Result;
use base64::engine::general_purpose::STANDARD;
use base64::Engine;
use crossterm::event::{
    DisableMouseCapture, EnableMouseCapture, Event, EventStream, KeyCode, KeyEventKind,
    KeyModifiers, MouseButton, MouseEventKind,
};
use crossterm::execute;
use crossterm::terminal::{
    disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen,
};
use futures_util::{SinkExt, StreamExt};
use ratatui::backend::CrosstermBackend;
use ratatui::Terminal;
use serde::{Deserialize, Serialize};
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

/// A power a user currently holds in the room. Badges stack: a user can be the
/// `Host` *and* a `Sudoer` *and* a `Driver` at once. `Member` is the floor —
/// returned only when none of the others apply. The order of the variants is
/// the order badges are painted (host first).
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Role {
    Host,
    Sudoer,
    Driver,
    Member,
}

#[derive(Clone)]
pub struct User {
    pub user_id: String,
    pub username: String,
}

/// An in-progress incoming transfer we accepted. Chunks stream straight to a
/// disk-backed `Sink` (created lazily on the first chunk) so a multi-GB payload
/// never sits in RAM.
struct Transfer {
    meta: ft::Offer,
    sink: Option<ft::Sink>,
    accepted: bool,
}

/// An outgoing transfer awaiting / serving an accept. The payload is streamed
/// from `src` on disk, not held in memory.
struct ActiveSend {
    id: String,
    src: std::path::PathBuf,
    /// `src` is a temp tar we built for a directory — delete it after sending.
    temp: bool,
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
    /// Incremental reply text from a streaming AI agent. `text` is the reply so
    /// far (cumulative); `done` clears the live preview (the final, persisted
    /// chat message arrives separately as a normal `Message`).
    AiStream {
        name: String,
        text: String,
        done: bool,
    },
    /// A local system notice produced off-thread (e.g. async Ollama probe).
    Sys(String),
    Err(String),
    /// An outgoing `/send` finished hashing off-thread and is staged for
    /// streaming — the run loop records it in `active_send` so a peer's `/accept`
    /// can start the chunk stream. The offer frame is emitted by the same task.
    SendReady {
        id: String,
        src: std::path::PathBuf,
        temp: bool,
        name: String,
        size: u64,
        to: Option<String>,
    },
    /// Relayed to the room when a member opens a shared VirtualBox VM locally, so
    /// the *other* party knows the appliance is live and can open their own copy.
    /// `by` is the server-authenticated launcher; the receiver skips its own echo.
    VmOpened {
        by: String,
        vm: String,
    },
    Closed,
}

pub struct SbxView {
    pub parser: vt100::Parser,
    pub backend: String,
}

/// The arrow-navigable VirtualBox VM picker, opened by a bare `/sbx vbox`
/// (or `/sbx gui`). Holds the locally-registered VM names and the highlighted
/// row; Enter/Tab fills `/sbx vbox gui <vm>` into the input, Esc dismisses.
#[derive(Clone)]
pub struct VboxPicker {
    pub vms: Vec<String>,
    pub selected: usize,
}

/// A resizable region of the window, for interactive layout editing. Selected by
/// clicking it (or cycling with F5); once selected, arrow keys resize it.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum Pane {
    Chat,
    Roster,
    Terminal,
    /// The message/compose box at the bottom. Unlike the other three it lives
    /// outside the BSP body tree (it's the frame's fixed bottom row), so its only
    /// adjustable axis is height — grow it to read a long/wrapped message.
    Input,
}

/// Launch parameters captured when `/sbx launch` needs root but sudo isn't
/// cached. Held aside while the masked password modal collects the secret; the
/// launch fires (with the password) once it's submitted.
pub struct PendingSudoLaunch {
    pub backend: sbx::Backend,
    pub image: String,
    pub members: Vec<String>,
    pub rows: u16,
    pub cols: u16,
    pub start_daemon: bool,
    pub install_first: bool,
    pub gui: bool,
}

/// Launch parameters captured when installing VirtualBox needs root but sudo
/// isn't cached. Held aside while the masked password modal collects the secret;
/// the install + VM boot fires (with the password) once it's submitted. Mirrors
/// `PendingSudoLaunch` for the VirtualBox GUI path.
pub struct PendingVboxInstall {
    pub vm: String,
    pub conflicts: Vec<sbx::VtxHolder>,
    pub needs_install: bool,
    pub needs_import: bool,
    pub room: Arc<fernet::Fernet>,
}

/// What a submitted sudo password authorizes. The masked modal is shared; this
/// records which privileged action to fire once the password is entered.
pub enum PendingPrivileged {
    /// Launch a container sandbox that needs root (binary install / daemon start).
    Launch(PendingSudoLaunch),
    /// Install VirtualBox (apt needs root), then boot the requested VM's GUI.
    VboxInstall(PendingVboxInstall),
}

/// A local, masked sudo-password prompt. The typed password lives ONLY here —
/// it never enters `App::input` (chat), a `ChatLine`, the PTY stream, or any
/// outbound frame — until it's handed to `sudo -S` for the pending action.
/// Deliberately no `Debug` derive so the secret can't be logged by accident.
pub struct SudoPrompt {
    pub password: String,
    pub pending: PendingPrivileged,
}

/// Launch parameters captured when a GUI sandbox's noVNC port is already bound
/// by another process. Held aside while the port-consent modal asks whether to
/// kill the holder; the launch fires once the owner confirms. Carries the
/// (already-collected) sudo password, if any, so the gate works on both the
/// no-sudo and sudo-submit launch paths.
pub struct PendingGuiLaunch {
    pub backend: sbx::Backend,
    pub image: String,
    pub members: Vec<String>,
    pub rows: u16,
    pub cols: u16,
    pub start_daemon: bool,
    pub install_first: bool,
    pub gui: bool,
    pub password: Option<String>,
}

/// Active "port in use — kill it?" consent prompt for a GUI sandbox launch.
/// While `Some`, y/n keystrokes resolve this instead of feeding chat.
pub struct PortPrompt {
    pub pid: i32,
    pub holder: String,
    pub pending: PendingGuiLaunch,
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
    /// Open VM picker (arrow-navigable list of local VirtualBox VMs), or None.
    pub vbox_picker: Option<VboxPicker>,
    transfers: HashMap<String, Transfer>,
    /// Chat scrollback: lines scrolled up from the live bottom (0 = following).
    pub chat_scroll: usize,
    /// Sandbox terminal scrollback: rows scrolled up from the bottom.
    pub sbx_scroll: usize,
    /// Whether the help overlay is showing.
    pub show_help: bool,
    /// Vertical scroll offset (rows) into the help overlay when it doesn't fit.
    pub help_scroll: u16,
    /// Index of the currently highlighted help cluster (up/down moves it).
    pub help_selected: usize,
    /// Per-cluster expand/collapse state (left/right/Enter toggles); sized to the
    /// cluster count when the overlay opens.
    pub help_expanded: Vec<bool>,
    /// A reconnect handshake is in flight (Ctrl-R after a disconnect).
    pub reconnecting: bool,
    /// Transient error shown as a popup over the clergy (cleared on next keypress).
    pub error: Option<String>,
    /// The room password this client authenticated with (shown by `/pw`).
    pub password: String,
    /// AI agents currently generating a reply — drives the "thinking" spinner.
    pub ai_typing: std::collections::HashSet<String>,
    /// Live, in-progress reply text per streaming agent, shown as a transient
    /// preview bubble until the final message lands. Keyed by agent name.
    pub ai_stream: std::collections::HashMap<String, String>,
    /// Monotonic tick counter used to animate the AI spinner.
    pub spin: usize,
    /// When set, agents we summon are auto-granted sandbox drive on each launch
    /// (`/ai start <name> allow`). Re-applied in the broker Ready handler, since
    /// launching a sandbox resets the ACL back to just the owner.
    pub agent_sbx_allow: bool,
    /// Display name the summoned agent joined under — derived from its model or
    /// profile at `/ai start` (e.g. "qwen2.5:3b", model name + parameter size).
    /// Tracked so the broker Ready handler can re-grant its drive and `/ai stop`
    /// can revoke the right ACL entry.
    pub agent_name: Option<String>,
    /// Window layout: chat/terminal split, roster width, fullscreen zoom. Read
    /// by `ui::draw` and `sbx_dims`; mutated by F4/F5 + interactive editing.
    pub layout: Layout,
    /// Interactive layout editing: the pane currently selected for resizing
    /// (click it or cycle with F5), or None when not editing. When set, arrow
    /// keys resize this pane instead of scrolling.
    pub focused_pane: Option<Pane>,
    /// Active local sudo-password prompt (None unless `/sbx launch` needs root
    /// and creds aren't cached). While `Some`, keystrokes feed this masked
    /// buffer instead of chat — the secret never leaves the client.
    pub sudo_prompt: Option<SudoPrompt>,
    /// Active "noVNC port already in use — kill the holder?" consent prompt for a
    /// GUI sandbox launch (None unless the port is busy). While `Some`, y/n keys
    /// resolve it instead of feeding chat.
    pub port_prompt: Option<PortPrompt>,
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
            vbox_picker: None,
            transfers: HashMap::new(),
            chat_scroll: 0,
            sbx_scroll: 0,
            show_help: false,
            help_scroll: 0,
            help_selected: 0,
            help_expanded: Vec::new(),
            reconnecting: false,
            error: None,
            password: String::new(),
            ai_typing: std::collections::HashSet::new(),
            ai_stream: std::collections::HashMap::new(),
            spin: 0,
            agent_sbx_allow: false,
            agent_name: None,
            layout: Layout::default(),
            focused_pane: None,
            sudo_prompt: None,
            port_prompt: None,
        }
    }

    /// Length of the in-progress sudo password (for the masked modal to render
    /// `•`s), or `None` when no prompt is open. Exposes only the length — never
    /// the secret itself — so `ui` can draw it without touching the bytes.
    pub fn sudo_prompt_len(&self) -> Option<usize> {
        self.sudo_prompt.as_ref().map(|p| p.password.chars().count())
    }

    /// Cycle the interactive-edit selection: Chat → Terminal → Roster → Input →
    /// off. Bound to F5; clicking a pane jumps straight to it instead. The
    /// Terminal step is skipped when no sandbox is up (nothing to resize there);
    /// the Input bar is always a stop (height-only).
    fn cycle_focus(&mut self) {
        // Cycle over the panes the layout is actually painting (chat → terminal →
        // roster → input), then off. Skips the terminal with no sandbox and the
        // roster when it's hidden, so every stop is something you can resize.
        let panes = self.layout.present_panes(self.sandbox.is_some());
        self.focused_pane = match self.focused_pane {
            None => panes.first().copied(),
            Some(cur) => match panes.iter().position(|p| *p == cur) {
                Some(i) => panes.get(i + 1).copied(),
                None => panes.first().copied(),
            },
        };
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

    /// The room host — whoever opened the house. The relay returns the roster in
    /// join order (its session store is an insertion-ordered map), and every
    /// client is served the *same* ordered roster, so "first occupant" is a
    /// stable, consistent host across all peers without any extra protocol.
    pub fn host(&self) -> Option<&str> {
        self.users.first().map(|u| u.username.as_str())
    }

    /// Every role `name` currently holds, additive and in paint order. Reads the
    /// exact same `sudoers`/`drivers` sets the broker uses to gate shell input,
    /// so a rendered badge can never claim a power the room won't actually
    /// enforce. Always returns at least `Member`.
    pub fn roles_of(&self, name: &str) -> Vec<Role> {
        let mut roles = Vec::new();
        if self.host() == Some(name) {
            roles.push(Role::Host);
        }
        if self.sudoers.contains(name) {
            roles.push(Role::Sudoer);
        }
        if self.drivers.contains(name) {
            roles.push(Role::Driver);
        }
        if roles.is_empty() {
            roles.push(Role::Member);
        }
        roles
    }

    fn sys(&mut self, text: impl Into<String>) {
        self.push_line(ChatLine {
            ts: String::new(),
            username: String::new(),
            text: text.into(),
            system: true,
        });
    }

    /// Open the help overlay fresh: first cluster highlighted, scrolled to top,
    /// all clusters collapsed (the empty expand vec renders as all-collapsed and
    /// is resized to the cluster count on the first navigation key).
    fn open_help(&mut self) {
        self.show_help = true;
        self.help_scroll = 0;
        self.help_selected = 0;
        self.help_expanded.clear();
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
                self.sys("/sbx <docker|podman|multipass|vbox|local> [gui] · /drive (F2 releases) · /ai start · /ai <question> · /send <user> <file> · /sendroom <file> · /pw show password · /help full command list · PgUp/PgDn scroll chat · ctrl-q quit");
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
                    self.ai_stream.remove(&name); // …nor streaming a reply
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
            Net::AiStream { name, text, done } => {
                if done {
                    self.ai_stream.remove(&name);
                } else {
                    // Streaming has started → drop the bare "thinking" spinner;
                    // the live preview now signals the agent is working.
                    self.ai_typing.remove(&name);
                    self.ai_stream.insert(name, text);
                }
            }
            Net::SbxStatus {
                backend,
                ready,
                rows,
                cols,
            } => {
                if ready {
                    // Converge on the shared shell. If we already track this
                    // sandbox, just match its size — recreating the parser would
                    // wipe scrollback and re-announce on every re-broadcast. The
                    // owner re-sends `status:ready` whenever a member (re)joins, so
                    // this MUST be idempotent for everyone already in the room; only
                    // a genuinely new sandbox (none yet) is created and announced.
                    match &mut self.sandbox {
                        Some(v) => {
                            v.parser.set_size(rows.max(1), cols.max(1));
                            v.backend = backend.clone();
                        }
                        None => {
                            self.sandbox = Some(SbxView {
                                parser: vt100::Parser::new(rows.max(1), cols.max(1), 2000),
                                backend: backend.clone(),
                            });
                            self.sys(format!("⛧ sandbox summoned ({backend}) — F2 to drive"));
                        }
                    }
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
            Net::SendReady { .. } => {} // handled in the run loop (stages active_send)
            Net::Sys(t) => self.sys(t),
            Net::Err(t) => self.err(t),
            Net::VmOpened { by, vm } => {
                // Skip our own echo — we already saw the local "launched" line.
                if by != self.me {
                    self.sys(format!(
                        "⛧ {by} opened ‘{vm}’ locally — `/sbx gui {vm}` to open your own copy"
                    ));
                }
            }
            Net::Closed => {
                self.connected = false;
                self.sys("connection closed — press Ctrl-R to reconnect");
            }
        }
    }
}

/// Human label for a resizable pane (used in layout-editing hints).
fn pane_label(p: Pane) -> &'static str {
    match p {
        Pane::Chat => "chat",
        Pane::Roster => "roster",
        Pane::Terminal => "terminal",
        Pane::Input => "input",
    }
}

/// Join saved-preset names for display, or "none" when there are none.
fn once_or_none(names: Vec<String>) -> String {
    if names.is_empty() {
        "none".to_string()
    } else {
        names.join(" · ")
    }
}

/// PTY grid (rows, cols) for the sandbox terminal, derived from the Terminal
/// leaf's actual rectangle in the layout tree — so it tracks **both** axes (the
/// old height-only `pty_pct` couldn't express width changes). Matches the split
/// `ui::draw` paints, keeping the local PTY and the room in lock-step. Subtracts
/// the pane border (2 each way) and floors at 1. Falls back to the full body
/// when no Terminal leaf is laid out (e.g. transiently before the tree rebuilds).
fn sbx_grid(term_w: u16, term_h: u16, layout: &Layout) -> (u16, u16) {
    use ratatui::layout::Rect;
    // Mirror ui::draw's frame split: 1-line top bar, body, then the input bar
    // (now a resizable height, not a fixed 3 lines).
    let body = Rect {
        x: 0,
        y: 1,
        width: term_w,
        height: term_h.saturating_sub(1 + layout.input_height()),
    };
    let r = layout
        .rect_of(body, Pane::Terminal, true)
        .unwrap_or(body);
    (
        r.height.saturating_sub(2).max(1),
        r.width.saturating_sub(2).max(1),
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
        // Esc must reach the shell (vim/less/etc. depend on it). Drive is released
        // with F2, not Esc, so this never collides with leaving the shell.
        KeyCode::Esc => Some(vec![0x1b]),
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

/// Read `path` and broadcast a file/dir offer. `to = Some(user)` targets one
/// member (only they're prompted); `to = None` offers to the whole room. The
/// payload is staged in `active_send` and streamed once an /accept arrives.
fn offer_payload(
    app: &mut App,
    send_seq: &mut u64,
    out_tx: &UnboundedSender<WsMsg>,
    room: &Arc<fernet::Fernet>,
    app_tx: &UnboundedSender<Net>,
    path: &str,
    to: Option<&str>,
) {
    *send_seq += 1;
    let id = format!("{}-{}", app.me, send_seq);
    let path = path.to_string();
    let to = to.map(str::to_string);
    let out = out_tx.clone();
    let room = room.clone();
    let atx = app_tx.clone();
    // Hashing (and tarring a directory) walks every byte — do it off the UI
    // thread so a multi-GB payload doesn't freeze the TUI. The task stages the
    // send via `Net::SendReady` *then* emits the offer, so a peer's `/accept`
    // can never race ahead of `active_send`.
    app.sys(format!("hashing {path}…"));
    tokio::task::spawn_blocking(move || match ft::prepare_send(&path) {
        Ok(s) => {
            let _ = atx.send(Net::SendReady {
                id: id.clone(),
                src: s.src,
                temp: s.temp,
                name: s.name.clone(),
                size: s.size,
                to: to.clone(),
            });
            let mut frame = json!({
                "_ft":"offer","id": id,"name": s.name,"size": s.size,"sha256": s.sha256,"dir": s.dir
            });
            if let Some(t) = &to {
                frame["to"] = json!(t);
            }
            let _ = out.send(WsMsg::Text(room.encrypt(frame.to_string().as_bytes())));
        }
        Err(e) => {
            let _ = atx.send(Net::Err(format!("send failed: {e}")));
        }
    });
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

/// Stream a payload from disk to the clergy as `_ft` chunks (background, paced).
/// Reads `src` in `CHUNK` blocks so only one chunk is ever resident — a multi-GB
/// appliance streams with flat memory. Deletes `src` afterward if it's a temp tar.
fn spawn_send(
    id: String,
    src: std::path::PathBuf,
    temp: bool,
    out: UnboundedSender<WsMsg>,
    room: Arc<fernet::Fernet>,
) {
    tokio::task::spawn_blocking(move || {
        use std::io::Read;
        let mut f = match std::fs::File::open(&src) {
            Ok(f) => f,
            Err(_) => return,
        };
        let mut buf = vec![0u8; ft::CHUNK];
        let mut seq = 0usize;
        loop {
            let n = match f.read(&mut buf) {
                Ok(0) => break,
                Ok(n) => n,
                Err(_) => break,
            };
            let frame =
                json!({"_ft":"chunk","id": id,"seq": seq,"data": STANDARD.encode(&buf[..n])});
            if out
                .send(WsMsg::Text(room.encrypt(frame.to_string().as_bytes())))
                .is_err()
            {
                break;
            }
            seq += 1;
            std::thread::sleep(Duration::from_millis(2));
        }
        let _ = out.send(WsMsg::Text(
            room.encrypt(json!({"_ft":"done","id": id}).to_string().as_bytes()),
        ));
        if temp {
            let _ = std::fs::remove_file(&src);
        }
    });
}

fn handle_ft(
    f: ft::Ft,
    app: &mut App,
    active: &mut Option<ActiveSend>,
    out: &UnboundedSender<WsMsg>,
    room: &Arc<fernet::Fernet>,
    downloads: &std::path::Path,
) -> Option<std::path::PathBuf> {
    // Set to the saved path when a transfer completes & verifies, so the caller
    // can auto-bridge it into a running sandbox.
    let mut saved = None;
    match f {
        ft::Ft::Offer(o) => {
            if o.from == app.me {
                return None; // our own offer echo
            }
            // Direct send addressed to a specific member: everyone receives the
            // broadcast, but only the named recipient is prompted.
            if let Some(to) = &o.to {
                if to != &app.me {
                    return None;
                }
            }
            app.sys(format!(
                "⛧ {} offers {} ({}{}){} — /accept or /reject",
                o.from,
                o.name,
                ft::human(o.size as usize),
                if o.dir { ", directory" } else { "" },
                if o.to.is_some() { " directly to you" } else { "" },
            ));
            app.transfers.insert(
                o.id.clone(),
                Transfer {
                    meta: o.clone(),
                    sink: None,
                    accepted: false,
                },
            );
            app.pending_offer = Some(o);
        }
        ft::Ft::Accept(id) => {
            if let Some(a) = active.as_mut() {
                if a.id == id && !a.sending {
                    a.sending = true;
                    spawn_send(id, a.src.clone(), a.temp, out.clone(), room.clone());
                    app.sys("transfer accepted — sending…");
                }
            }
        }
        ft::Ft::Reject(id) => {
            if active.as_ref().map(|a| a.id == id).unwrap_or(false) {
                app.sys("transfer rejected");
                if let Some(a) = active.take() {
                    if a.temp {
                        let _ = std::fs::remove_file(&a.src); // discard the temp tar
                    }
                }
            }
        }
        ft::Ft::Chunk { id, data } => {
            // Stream straight to the disk-backed sink (created lazily on the
            // first chunk). Enforce the declared size (clamped to STREAM_MAX) on
            // receipt — a sender can lie about `size` or just keep streaming, so
            // never let an accepted transfer grow past the cap.
            let mut overflow = false;
            let mut io_err: Option<String> = None;
            if let Some(t) = app.transfers.get_mut(&id) {
                if t.accepted {
                    if t.sink.is_none() {
                        match ft::Sink::create(downloads, &id) {
                            Ok(s) => t.sink = Some(s),
                            Err(e) => io_err = Some(e.to_string()),
                        }
                    }
                    if let Some(s) = t.sink.as_mut() {
                        let cap = (t.meta.size).min(ft::STREAM_MAX);
                        if s.written() + data.len() as u64 > cap {
                            overflow = true;
                        } else if let Err(e) = s.write(&data) {
                            io_err = Some(e.to_string());
                        }
                    }
                }
            }
            if overflow {
                if let Some(t) = app.transfers.remove(&id) {
                    if let Some(s) = t.sink {
                        s.abort();
                    }
                    app.err(format!(
                        "{} — transfer exceeds declared size (max {}), aborted",
                        t.meta.name,
                        ft::human((t.meta.size).min(ft::STREAM_MAX) as usize)
                    ));
                }
            } else if let Some(e) = io_err {
                if let Some(t) = app.transfers.remove(&id) {
                    if let Some(s) = t.sink {
                        s.abort();
                    }
                    app.err(format!("{} — write failed: {e}", t.meta.name));
                }
            }
        }
        ft::Ft::Done(id) => {
            if let Some(t) = app.transfers.remove(&id) {
                if t.accepted {
                    match t.sink {
                        Some(s) => match s.finish() {
                            Ok((tmp, sha)) => {
                                if sha != t.meta.sha256 {
                                    let _ = std::fs::remove_file(&tmp);
                                    app.err(format!(
                                        "{} — SHA-256 mismatch, discarded",
                                        t.meta.name
                                    ));
                                } else {
                                    match ft::commit(downloads, &t.meta, &tmp) {
                                        Ok(p) => {
                                            app.sys(format!(
                                                "⛧ saved {} ({}) — verified ✓",
                                                p.display(),
                                                ft::human(t.meta.size as usize)
                                            ));
                                            saved = Some(p);
                                        }
                                        Err(e) => app.err(format!("save failed: {e}")),
                                    }
                                }
                            }
                            Err(e) => {
                                app.err(format!("{} — finalize failed: {e}", t.meta.name))
                            }
                        },
                        None => app.err(format!("{} — no data received", t.meta.name)),
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
    saved
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
    // Seed the roster width from the active vestment so a `--theme` with a wider
    // roster is honoured; from here on the live layout owns it (so /theme swaps
    // don't stomp a width the user has set with /layout).
    app.layout.set_roster_width(theme.roster_width);
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
                let dims = sbx_grid(sz.width, sz.height, &app.layout);
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
                        // Masked sudo-password prompt (Option C). When open it
                        // swallows EVERY keystroke so the password can never leak
                        // into chat, the PTY, or an outbound frame — it lives only
                        // in `app.sudo_prompt.password` until it's moved into the
                        // launch task and fed to `sudo -S` over stdin.
                        if app.sudo_prompt.is_some() {
                            match k.code {
                                KeyCode::Enter => {
                                    // SAFETY of secrecy: `take()` moves the buffer
                                    // out; it's never cloned into any logged/visible
                                    // surface. The password is handed to spawn_launch
                                    // and dropped there after stdin is fed.
                                    let SudoPrompt { password, pending } =
                                        app.sudo_prompt.take().unwrap();
                                    if password.is_empty() {
                                        app.sys("⛧ cancelled — empty password");
                                    } else {
                                        match pending {
                                        PendingPrivileged::Launch(pending) => {
                                        if let Some(h) =
                                            if pending.gui { sbx::port_holder(sbx::GUI_PORT) } else { None }
                                        {
                                            // Root's now authenticated, but the noVNC port
                                            // is taken — carry the password into the
                                            // port-consent gate so the launch can still fire
                                            // (with sudo) once the holder is cleared.
                                            app.sys(format!(
                                                "⚠ port {} (noVNC) in use by pid {} ({}) — kill it and proceed? [y/N] · Esc cancels",
                                                sbx::GUI_PORT, h.pid, h.name
                                            ));
                                            app.port_prompt = Some(PortPrompt {
                                                pid: h.pid,
                                                holder: h.name,
                                                pending: PendingGuiLaunch {
                                                    backend: pending.backend,
                                                    image: pending.image,
                                                    members: pending.members,
                                                    rows: pending.rows,
                                                    cols: pending.cols,
                                                    start_daemon: pending.start_daemon,
                                                    install_first: pending.install_first,
                                                    gui: pending.gui,
                                                    password: Some(password),
                                                },
                                            });
                                        } else {
                                            launching = true;
                                            app.sys("🔒 authenticating + launching…");
                                            spawn_launch(
                                                pending.backend,
                                                pending.image,
                                                app.me.clone(),
                                                pending.members,
                                                pending.rows,
                                                pending.cols,
                                                pending.start_daemon,
                                                pending.install_first,
                                                pending.gui,
                                                Some(password),
                                                pty_tx.clone(),
                                                broker_tx.clone(),
                                                app_tx.clone(),
                                            );
                                        }
                                        }
                                        PendingPrivileged::VboxInstall(p) => {
                                            // Root authenticated → install VirtualBox
                                            // (sudo -S reads this password) then boot
                                            // the VM. The password is dropped inside
                                            // spawn_vm_execute after stdin is fed.
                                            app.sys("🔒 authenticating + installing VirtualBox…");
                                            let room = p.room.clone();
                                            spawn_vm_execute(
                                                p.vm,
                                                p.conflicts,
                                                p.needs_install,
                                                p.needs_import,
                                                &mut app,
                                                &app_tx,
                                                &out_tx,
                                                &room,
                                                Some(password),
                                            );
                                        }
                                        }
                                    }
                                }
                                KeyCode::Esc => {
                                    app.sudo_prompt = None;
                                    app.sys("⛧ sudo cancelled — launch aborted");
                                }
                                KeyCode::Backspace => {
                                    if let Some(p) = app.sudo_prompt.as_mut() {
                                        p.password.pop();
                                    }
                                }
                                KeyCode::Char(c) => {
                                    if let Some(p) = app.sudo_prompt.as_mut() {
                                        p.password.push(c);
                                    }
                                }
                                _ => {}
                            }
                        } else if app.port_prompt.is_some() {
                            // GUI-launch port-consent modal: the noVNC port is held
                            // by another process. y/Y kills the named pid then fires
                            // the held launch; n/N/Esc aborts. Swallows keys so the
                            // y/n never lands in chat.
                            match k.code {
                                KeyCode::Char('y') | KeyCode::Char('Y') => {
                                    let PortPrompt { pid, holder, pending } =
                                        app.port_prompt.take().unwrap();
                                    app.sys(format!(
                                        "⛧ killing {holder} (pid {pid}) to free port {}…",
                                        sbx::GUI_PORT
                                    ));
                                    if sbx::kill_port_holder(sbx::GUI_PORT, pid) {
                                        launching = true;
                                        app.sys("🖥 port freed — launching…");
                                        spawn_launch(
                                            pending.backend,
                                            pending.image,
                                            app.me.clone(),
                                            pending.members,
                                            pending.rows,
                                            pending.cols,
                                            pending.start_daemon,
                                            pending.install_first,
                                            pending.gui,
                                            pending.password,
                                            pty_tx.clone(),
                                            broker_tx.clone(),
                                            app_tx.clone(),
                                        );
                                    } else {
                                        app.sys(format!(
                                            "⚠ couldn't free port {} — launch aborted",
                                            sbx::GUI_PORT
                                        ));
                                    }
                                }
                                KeyCode::Char('n') | KeyCode::Char('N') | KeyCode::Esc => {
                                    app.port_prompt = None;
                                    app.sys("⛧ launch aborted — port left untouched");
                                }
                                _ => {}
                            }
                        } else if k.modifiers.contains(KeyModifiers::CONTROL)
                            && matches!(k.code, KeyCode::Char('x'))
                            && !app.driving
                        {
                            // Panic kill switch (sandbox owner): revoke every
                            // non-owner driver, interrupt whatever is running in
                            // the PTY, and re-broadcast the locked-down ACL. Cuts a
                            // runaway agent (or human) off mid-command. Gated on
                            // `!app.driving` so that while YOU hold the shell, Ctrl-X
                            // reaches the PTY instead (nano's quit; key_to_pty sends
                            // 0x18) — release with F2 first to arm the kill switch.
                            if let Some(sb) = &mut broker {
                                let owner = app.me.clone();
                                app.drivers.retain(|u| *u == owner);
                                app.sudoers.retain(|u| *u == owner);
                                app.agent_sbx_allow = false;
                                let _ = sb.write_input(&[0x03]); // Ctrl-C into the shell
                                broadcast_acl(&out_tx, &session.room, &app);
                                app.sys("⛧ kill switch — revoked all drive + interrupted the shell");
                            } else {
                                app.sys("kill switch is for the sandbox owner (you don't hold the PTY)");
                            }
                        } else if k.modifiers.contains(KeyModifiers::CONTROL)
                            && k.modifiers.contains(KeyModifiers::ALT)
                            && matches!(k.code, KeyCode::Char('p'))
                        {
                            // Conjure a brand-new procedural vestment (not a bundled
                            // preset): fresh palette + sigil, rolled from the clock.
                            theme = Theme::random();
                            app.sys(format!(
                                "{} conjured vestment '{}'",
                                theme.sigil, theme.name
                            ));
                        } else if k.modifiers.contains(KeyModifiers::CONTROL)
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
                        } else if app.vbox_picker.is_some() {
                            // Modal VM picker (from a bare `/sbx vbox`): arrow
                            // keys move the highlight, Enter/Tab boots the selected VM
                            // on this machine (host-frictionless path; a non-host who
                            // needs install/import is steered to re-issue with `yes`),
                            // Esc dismisses. Intercepts keys so the list stays put.
                            match k.code {
                                KeyCode::Up => {
                                    if let Some(p) = &mut app.vbox_picker {
                                        p.selected = p.selected.saturating_sub(1);
                                    }
                                }
                                KeyCode::Down => {
                                    if let Some(p) = &mut app.vbox_picker {
                                        p.selected =
                                            (p.selected + 1).min(p.vms.len().saturating_sub(1));
                                    }
                                }
                                KeyCode::Enter | KeyCode::Tab => {
                                    let vm = app
                                        .vbox_picker
                                        .take()
                                        .and_then(|p| p.vms.into_iter().nth(p.selected));
                                    if let Some(vm) = vm {
                                        launch_vbox_gui(
                                            &mut app, vm, false, &app_tx, &out_tx, &session.room,
                                        );
                                    }
                                }
                                KeyCode::Esc => {
                                    app.vbox_picker = None;
                                    app.sys("⛧ picker dismissed");
                                }
                                _ => {} // ignore other keys so the picker stays put
                            }
                        } else if app.show_help {
                            // tmux-style nav: up/down highlight a cluster, left/right
                            // (or Enter) collapse/expand it, PgUp/PgDn scroll the
                            // overflow, only Esc closes so stray keys can't dismiss a
                            // menu you're still reading.
                            let count = ui::help_cluster_count(&theme);
                            let max = term
                                .size()
                                .map(|s| ui::help_max_scroll(s.width, s.height, &app, &theme))
                                .unwrap_or(0);
                            // keep the expand state sized to the clusters
                            if app.help_expanded.len() != count {
                                app.help_expanded.resize(count, false);
                            }
                            match k.code {
                                KeyCode::Up => {
                                    app.help_selected = app.help_selected.saturating_sub(1);
                                }
                                KeyCode::Down => {
                                    app.help_selected =
                                        (app.help_selected + 1).min(count.saturating_sub(1));
                                }
                                KeyCode::Left => {
                                    if let Some(e) = app.help_expanded.get_mut(app.help_selected) {
                                        *e = false; // collapse the highlighted cluster
                                    }
                                }
                                KeyCode::Right => {
                                    if let Some(e) = app.help_expanded.get_mut(app.help_selected) {
                                        *e = true; // reveal the highlighted cluster
                                    }
                                }
                                KeyCode::Enter | KeyCode::Char(' ') => {
                                    if let Some(e) = app.help_expanded.get_mut(app.help_selected) {
                                        *e = !*e; // toggle
                                    }
                                }
                                KeyCode::PageUp => app.help_scroll = app.help_scroll.saturating_sub(10),
                                KeyCode::PageDown => app.help_scroll = (app.help_scroll + 10).min(max),
                                KeyCode::Home => app.help_scroll = 0,
                                KeyCode::End => app.help_scroll = max,
                                KeyCode::Esc | KeyCode::F(1) => {
                                    // Esc dismisses; F1 toggles the overlay back shut
                                    app.show_help = false;
                                    app.help_scroll = 0;
                                }
                                _ => {} // ignore other keys so the menu stays put
                            }
                            // clamp scroll in case collapsing shrank the content
                            let max = term
                                .size()
                                .map(|s| ui::help_max_scroll(s.width, s.height, &app, &theme))
                                .unwrap_or(0);
                            app.help_scroll = app.help_scroll.min(max);
                        } else if k.code == KeyCode::F(1) {
                            app.open_help(); // F1 from any mode
                        } else if k.code == KeyCode::F(2) {
                            if app.sandbox.is_none() {
                            } else if app.can_drive() {
                                app.driving = !app.driving;
                            } else {
                                app.sys("you don't have drive permission — the owner can /grant you");
                            }
                        } else if k.code == KeyCode::F(4) {
                            // Fullscreen the terminal (cycle terminal → chat → split).
                            // Caught before the drive branch so it works mid-session;
                            // F-keys aren't forwarded to the shell anyway (key_to_pty).
                            if app.sandbox.is_some() {
                                app.layout.cycle_zoom();
                                announced_dims = None; // re-sync PTY to the new height
                                app.sys(format!("⛧ {}", app.layout.describe()));
                            } else {
                                app.sys("no sandbox to fullscreen — /sbx <type> first");
                            }
                        } else if k.code == KeyCode::F(5) {
                            // Enter/cycle interactive layout editing: pick a pane to
                            // resize (Terminal → Chat → Roster → off). Clicking a pane
                            // selects it directly; arrows then resize it.
                            app.cycle_focus();
                            match app.focused_pane {
                                Some(p) => app.sys(format!(
                                    "⛧ editing {} — arrows resize · Esc done",
                                    pane_label(p)
                                )),
                                None => app.sys("⛧ layout editing off"),
                            }
                        } else if app.focused_pane.is_some() && !app.driving {
                            // Interactive layout editing: a pane is selected (via click
                            // or F5). Arrows resize it live; Esc/Enter finishes. Sits
                            // before the driving branch but is gated on !driving so the
                            // shell still gets its keys while you hold the PTY.
                            let pane = app.focused_pane.unwrap();
                            let has_term = app.sandbox.is_some();
                            let dir = match k.code {
                                KeyCode::Up => Some(Dir::Up),
                                KeyCode::Down => Some(Dir::Down),
                                KeyCode::Left => Some(Dir::Left),
                                KeyCode::Right => Some(Dir::Right),
                                _ => None,
                            };
                            match k.code {
                                KeyCode::Esc | KeyCode::Enter => {
                                    app.focused_pane = None;
                                    app.sys(format!("⛧ {}", app.layout.describe()));
                                }
                                _ if dir.is_some() => {
                                    // Every pane is resizable on both axes wherever a
                                    // divider bounds it; resize_focused moves the right
                                    // one (or reports NoAxisHere so we can hint).
                                    match app.layout.resize_focused(pane, dir.unwrap(), has_term) {
                                        Resize::Moved => {
                                            // Any divider move can change the terminal's
                                            // rect (height *or* width now), so force a
                                            // PTY re-sync to rebroadcast the new grid.
                                            announced_dims = None;
                                            app.sys(format!("⛧ {}", app.layout.describe()));
                                        }
                                        Resize::NoAxisHere => {
                                            app.sys(
                                                "no divider on that axis here — F5 to the input bar to grow the compose box, or launch a sandbox for terminal height",
                                            );
                                        }
                                    }
                                }
                                // Typing a printable char is an escape hatch: drop out
                                // of layout-edit mode and feed the key to the compose box.
                                // Without this, a stray click into edit mode silently
                                // swallows every keystroke and feels like a freeze.
                                KeyCode::Char(c)
                                    if !k.modifiers.contains(KeyModifiers::CONTROL)
                                        && !k.modifiers.contains(KeyModifiers::ALT) =>
                                {
                                    app.focused_pane = None;
                                    app.input.push(c);
                                }
                                _ => {} // ignore other keys while editing
                            }
                        } else if app.driving {
                            // Esc is NOT a release key here — vim & friends need it,
                            // so it's forwarded to the PTY like any other key (see
                            // key_to_pty). Press F2 (handled above) to release the
                            // shell back to chat.
                            if k.code == KeyCode::PageUp {
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
                                    handle_command(&line, &mut app, &mut theme, &mut send_seq,
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
                            // Left-click a pane to select it for interactive resize
                            // (then arrows adjust it; Esc finishes). Skipped while
                            // driving the shell or with an overlay up, so clicks there
                            // don't hijack the terminal/help/picker.
                            MouseEventKind::Down(MouseButton::Left)
                                if !app.driving && !app.show_help && app.vbox_picker.is_none() =>
                            {
                                if let Ok(sz) = term.size() {
                                    match ui::pane_at(sz.width, sz.height, &app, m.column, m.row) {
                                        // Clicking the compose box must NOT enter layout-edit
                                        // mode — that's where you type, and locking it on a
                                        // click is exactly the "stuck, can't type" footgun.
                                        Some(p) if p != Pane::Input => {
                                            app.focused_pane = Some(p);
                                            app.sys(format!(
                                                "⛧ editing {} — arrows resize · Esc done",
                                                pane_label(p)
                                            ));
                                        }
                                        _ => {
                                            app.focused_pane = None;
                                        }
                                    }
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
                // Drain a burst of incoming frames per turn. The reader funnels both
                // chat and high-volume `_sbx:data` terminal output through this one
                // channel, and the loop redraws once per turn — so handling a single
                // frame per redraw lets a busy sandbox stream bury chat arbitrarily far
                // back in the queue. Pulling up to a cap of ready frames now keeps chat
                // latency bounded no matter how hard the shared shell is scrolling.
                let Some(first) = net else { break Ok(()) };
                let mut burst = vec![first];
                drain_ready(&mut rx, &mut burst, 256);
                for ev in burst {
                    match ev {
                        Net::SbxInput { from, bytes } => {
                            if let Some(sb) = &mut broker {
                                if app.drivers.contains(&from) {
                                    let _ = sb.write_input(&bytes);
                                }
                            }
                        }
                        Net::Ft(f) => {
                            // On a received, SHA-verified file, auto-bridge it into
                            // a sandbox we host so the whole clergy can use it from
                            // the shared shell. Runs off the UI thread (docker/mp
                            // exec + tar stream) and reports back via the app channel.
                            // A local backend already shares the host fs — nothing to do.
                            if let Some(path) =
                                handle_ft(f, &mut app, &mut active_send, &out_tx, &session.room, &downloads)
                            {
                                // A received VirtualBox appliance auto-imports so the VM
                                // registers locally and the recipient can immediately
                                // `/sbx vbox gui <vm>`. Off-thread (VBoxManage import
                                // is slow); reports back via the app channel.
                                let ext = path
                                    .extension()
                                    .and_then(|s| s.to_str())
                                    .map(|s| s.to_ascii_lowercase());
                                if matches!(ext.as_deref(), Some("ova") | Some("ovf")) {
                                    let tx = app_tx.clone();
                                    let ova = path.clone();
                                    tokio::spawn(async move {
                                        let res = tokio::task::spawn_blocking(move || {
                                            sbx::import_appliance(&ova)
                                        })
                                        .await;
                                        let _ = match res {
                                            Ok(Ok(vm)) => tx.send(Net::Sys(format!(
                                                "⛧ imported VM ‘{vm}’ — `/sbx vbox gui {vm}` to boot it"
                                            ))),
                                            Ok(Err(e)) => tx.send(Net::Sys(format!(
                                                "(received .ova not auto-imported: {e})"
                                            ))),
                                            Err(e) => tx.send(Net::Err(format!("import task: {e}"))),
                                        };
                                    });
                                }
                                if let Some((be, name)) = &broker_meta {
                                    if !matches!(be, sbx::Backend::Local) {
                                        let (be, name) = (*be, name.clone());
                                        let run_user = sbx::run_user_for(
                                            be,
                                            app.owner.as_deref().unwrap_or(app.me.as_str()),
                                        );
                                        let tx = app_tx.clone();
                                        tokio::spawn(async move {
                                            let res = tokio::task::spawn_blocking(move || {
                                                sbx::push(be, &name, &run_user, &path)
                                            })
                                            .await;
                                            let _ = match res {
                                                Ok(Ok(dest)) => tx.send(Net::Sys(format!(
                                                    "⛧ bridged into sandbox → {dest}"
                                                ))),
                                                Ok(Err(e)) => tx.send(Net::Sys(format!(
                                                    "(received file not bridged into sandbox: {e})"
                                                ))),
                                                Err(e) => tx.send(Net::Err(format!("bridge task: {e}"))),
                                            };
                                        });
                                    }
                                }
                            }
                        }
                        // The broker renders its sandbox locally from the PTY, so it
                        // ignores its own echoed status/data; everyone else uses them.
                        Net::SbxData(b) => {
                            if broker.is_none() {
                                if let Some(v) = &mut app.sandbox { v.parser.process(&b); }
                            }
                        }
                        Net::SbxStatus { .. } if broker.is_some() => {}
                        ev @ Net::Joined(_) => {
                            // Someone (re)entered and missed everything broadcast
                            // before they connected. If we host the sandbox, replay
                            // it so it reappears for them: `status:ready` makes the
                            // pane show up, the screen snapshot (`_sbx:data`) paints
                            // its current contents instead of a blank pane, and the
                            // ACL re-broadcast re-syncs grant state (also what makes
                            // `/ai start <m> allow` reach a freshly-joined agent).
                            // The status/data handlers are idempotent, so members
                            // already in the room aren't disturbed by the replay.
                            if broker.is_some() {
                                if let (Some(v), Some((be, sbx_name))) = (&app.sandbox, &broker_meta) {
                                    let (rows, cols) = v.parser.screen().size();
                                    send_frame(&out_tx, &session.room, json!({
                                        "_sbx":"status","state":"ready",
                                        "backend": be.label(),"engine": be.engine(),"name": sbx_name,
                                        "rows": rows,"cols": cols
                                    }));
                                    let snap = v.parser.screen().contents_formatted();
                                    send_frame(&out_tx, &session.room, json!({
                                        "_sbx":"data","b64": STANDARD.encode(&snap)
                                    }));
                                }
                                broadcast_acl(&out_tx, &session.room, &app);
                            }
                            app.apply(ev);
                        }
                        Net::SendReady { id, src, temp, name, size, to } => {
                            // The off-thread hash finished and emitted the offer; record
                            // the staged send so a peer's /accept can start streaming.
                            active_send = Some(ActiveSend { id, src, temp, sending: false });
                            match to {
                                Some(t) => app.sys(format!(
                                    "offered {} ({}) to {} — waiting for an /accept",
                                    name,
                                    ft::human(size as usize),
                                    t
                                )),
                                None => app.sys(format!(
                                    "offered {} ({}) to the room — waiting for an /accept",
                                    name,
                                    ft::human(size as usize)
                                )),
                            }
                        }
                        other => app.apply(other),
                    }
                }
            }
            msg = broker_rx.recv() => {
                match msg {
                    Some(BrokerMsg::Ready { sb, backend, name, rows, cols }) => {
                        broker = Some(sb);
                        broker_meta = Some((backend, name.clone()));
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
                        if app.agent_sbx_allow {
                            // Re-apply a `/ai start … allow` grant the launch reset.
                            if let Some(n) = &app.agent_name {
                                app.drivers.insert(n.clone());
                            }
                        }
                        send_frame(&out_tx, &session.room, json!({
                            "_sbx":"status","state":"ready","backend": backend.label(),
                            "engine": backend.engine(),"name": name,"rows": rows,"cols": cols
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
                                    if let Some((be, sbx_name)) = &broker_meta {
                                        if let Some(v) = &app.sandbox {
                                            let (rows, cols) = v.parser.screen().size();
                                            send_frame(&out_tx, &session.room, json!({
                                                "_sbx":"status","state":"ready",
                                                "backend": be.label(),"engine": be.engine(),"name": sbx_name,
                                                "rows": rows,"cols": cols
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
                if let Some(s) = new_sink {
                    sink = s;
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

/// Pull up to `cap` *already-ready* items out of `rx` (without awaiting) in FIFO
/// order, appending to `buf`. The UI loop uses this to drain a burst of incoming
/// frames per turn so a high-volume `_sbx:data` stream can't bury chat behind a
/// one-frame-per-redraw cap.
fn drain_ready<T>(rx: &mut UnboundedReceiver<T>, buf: &mut Vec<T>, cap: usize) {
    while buf.len() < cap {
        match rx.try_recv() {
            Ok(m) => buf.push(m),
            Err(_) => break, // empty or disconnected — nothing more to take right now
        }
    }
}

#[allow(clippy::too_many_arguments)]
fn handle_command(
    line: &str,
    app: &mut App,
    theme: &mut Theme,
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
        app.open_help();
    } else if line == "/clear" || line == "/cls" {
        // Local-only: wipe this client's chat scrollback. Doesn't touch the
        // room — other peers keep their own history.
        app.lines.clear();
        app.chat_scroll = 0;
        app.sys("⛧ chat cleared");
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
                "vestments: {} · random · save [name] — /theme <name>",
                Theme::available().join(" · ")
            ));
        } else if name == "random" {
            // Same as Ctrl+Alt+P — roll a fresh procedural vestment.
            *theme = Theme::random();
            app.sys(format!(
                "{} conjured vestment '{}'",
                theme.sigil, theme.name
            ));
        } else if name == "save" || name.starts_with("save ") {
            // Persist the vestment you're currently wearing (e.g. a `random`
            // roll you like) to themes/<slug>.toml so it sticks around. Bare
            // `/theme save` reuses the theme's own generated name.
            let want = name[4..].trim();
            let want = if want.is_empty() {
                theme.name.clone()
            } else {
                want.to_string()
            };
            match theme.save(&want) {
                Ok(slug) => app.sys(format!(
                    "{} saved vestment '{slug}' — re-don it anytime with /theme {slug}",
                    theme.sigil
                )),
                Err(e) => app.err(format!("couldn't save vestment: {e}")),
            }
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
    } else if let Some(rest) = line.strip_prefix("/layout") {
        // Named layout presets only — live resizing is interactive now (F4
        // fullscreen, F5 or click a pane then arrows). `/layout` keeps just the
        // memorable verbs for saving/recalling arrangements, plus `reset`.
        // Loading a preset clears announced_dims so the run loop re-syncs (and
        // broadcasts) the live PTY size on the next tick.
        let rest = rest.trim();
        let (cmd, arg) = rest
            .split_once(char::is_whitespace)
            .map(|(c, a)| (c, a.trim()))
            .unwrap_or((rest, ""));
        let mut resized = false;
        match cmd {
            "" => {
                app.sys(format!("⛧ layout: {}", app.layout.describe()));
                app.sys("  resize live: F4 fullscreen · F5 or click a pane, then arrows · Esc done");
                app.sys(format!(
                    "  presets: {} — /layout save <name> · load <name> · rm <name>",
                    once_or_none(Layout::available())
                ));
            }
            "reset" | "default" => {
                let roster = app.layout.roster_width();
                app.layout = Layout::default();
                app.layout.set_roster_width(roster); // keep your roster choice on reset
                resized = true;
                app.sys(format!("⛧ layout reset — {}", app.layout.describe()));
            }
            "save" if !arg.is_empty() => match app.layout.save(arg) {
                Ok(slug) => app.sys(format!(
                    "⛧ saved layout '{slug}' — re-apply anytime with /layout load {slug}"
                )),
                Err(e) => app.err(format!("couldn't save layout: {e}")),
            },
            "load" | "apply" if !arg.is_empty() => match Layout::by_name(arg) {
                Ok(l) => {
                    app.layout = l;
                    resized = true;
                    app.sys(format!("⛧ loaded layout '{arg}' — {}", app.layout.describe()));
                }
                Err(_) => app.err(format!(
                    "no saved layout '{arg}' — saved: {}",
                    once_or_none(Layout::available())
                )),
            },
            "list" | "presets" | "ls" => {
                app.sys(format!("⛧ saved layouts: {}", once_or_none(Layout::available())));
            }
            "rm" | "delete" | "del" if !arg.is_empty() => match Layout::remove(arg) {
                Ok(()) => app.sys(format!("⛧ deleted layout '{arg}'")),
                Err(e) => app.err(format!("{e}")),
            },
            // Bare `/layout <name>` → load a saved preset if it exists.
            name => match Layout::by_name(name) {
                Ok(l) => {
                    app.layout = l;
                    resized = true;
                    app.sys(format!("⛧ loaded layout '{name}' — {}", app.layout.describe()));
                }
                Err(_) => app.sys(
                    "usage: /layout [save <name>|load <name>|list|rm <name>|reset] — resize live with F4/F5 or click",
                ),
            },
        }
        if resized {
            *announced_dims = None; // force the run loop to re-sync the PTY size
        }
    } else if line == "/drive" {
        // Mobile-friendly alternative to F2 (no function key needed).
        if app.sandbox.is_none() {
            app.sys("no sandbox running — /sbx <type> first");
        } else if app.can_drive() {
            app.driving = true;
            app.sys("⛧ drive mode ON — type into the shell (Esc reaches vim etc.) · press F2 to release");
        } else {
            app.sys("you don't have drive permission — the owner can /grant you");
        }
    } else if let Some(rest) = line.strip_prefix("/sendroom ") {
        // Offer a file/dir to the whole room — anyone may /accept.
        offer_payload(app, send_seq, out_tx, room, app_tx, rest.trim(), None);
    } else if let Some(rest) = line.strip_prefix("/send ") {
        // Direct send to one member: `/send <user> <path>`. Everyone receives the
        // broadcast offer, but only <user> is prompted to /accept.
        let rest = rest.trim();
        match rest.split_once(char::is_whitespace) {
            Some((who, path)) => {
                let (who, path) = (who.trim(), path.trim());
                if who == app.me {
                    app.sys("can't /send to yourself — use /sendroom <path> for everyone");
                } else if path.is_empty() {
                    app.sys("usage: /send <user> <path>  ·  /sendroom <path> for everyone");
                } else if !app.users.iter().any(|u| u.username == who) {
                    let roster = app
                        .users
                        .iter()
                        .map(|u| u.username.as_str())
                        .filter(|u| *u != app.me)
                        .collect::<Vec<_>>()
                        .join(" · ");
                    app.err(format!("no member '{who}' in the room — try: {roster}"));
                } else {
                    offer_payload(app, send_seq, out_tx, room, app_tx, path, Some(who));
                }
            }
            None => app.sys("usage: /send <user> <path>  ·  /sendroom <path> for everyone"),
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
            // Grammar: `/sbx <vm type> <option>` — the backend leads, e.g.
            // `/sbx podman gui`, `/sbx docker`, `/sbx multipass`, `/sbx local`.
            // vbox is the exception that takes extra options (a VM name, `new`,
            // confirm): `/sbx vbox gui <vm>`, `/sbx vbox new [name]`.
            // `launch` stays accepted as a transitional alias (`/sbx launch
            // <backend> …`) so older muscle memory and help strings keep working.
            Some(
                sub @ ("launch" | "docker" | "podman" | "multipass" | "local" | "vbox"
                | "virtualbox"),
            ) => {
                // `--start` (alias `--start-daemon` / `-y`) opts in to booting a
                // stopped Docker daemon; everything else is positional. The first
                // positional selects the backend: docker | podman | multipass |
                // vbox | local. Each runs on the *invoker's* own machine.
                let mut args: Vec<&str> = p.collect();
                // Backend-led form: the matched token IS the backend, so push it to
                // the front of the positional args where the `first` logic below
                // expects it. The `launch` alias leaves the backend in `args` as-is.
                if sub != "launch" {
                    args.insert(0, sub);
                }
                let start_daemon = args
                    .iter()
                    .any(|a| matches!(*a, "--start" | "--start-daemon" | "-y"));
                // Consent token: if the selected backend's binary is missing,
                // `install` opts in to installing it (detect-then-install). It's
                // a positional keyword (not the image), so filter it out below.
                let want_install = args.iter().any(|a| matches!(*a, "install" | "--install"));
                // `gui` option (`/sbx podman gui`): provision an XFCE/noVNC desktop
                // in the container and publish it on the host loopback. Keyword, not
                // an image, so it's filtered out of the positionals like `install`.
                let want_gui = args.iter().any(|a| matches!(*a, "gui"));
                let mut pos = args
                    .iter()
                    .copied()
                    .filter(|a| !a.starts_with('-') && !matches!(*a, "install" | "gui"));
                let first = pos.next();
                if matches!(first, Some("virtualbox") | Some("vbox")) {
                    // VirtualBox runs locally in its own GUI — host & guest each
                    // open their own copy, nothing is relayed. It's independent of
                    // the shared-PTY sandbox, so it bypasses the "already running"
                    // guard. Grammar: `/sbx vbox [gui] <vm> [yes]`; a bare
                    // `/sbx vbox` opens the arrow-navigable VM picker.
                    let mut rest = pos.peekable();
                    if rest.peek() == Some(&"new") {
                        // `/sbx vbox new [name]` — build a brand-new VM from
                        // a cloud image (cloud-init toolchain), not an existing one.
                        rest.next();
                        let name = rest.next().unwrap_or("hh-vbox").to_string();
                        launch_vbox_new(app, name, app.me.clone(), app_tx);
                    } else {
                        // The optional `gui` keyword is already stripped from `pos`
                        // (it's a global option filter), so the next positional is
                        // the VM name directly.
                        match rest.next() {
                            Some(vm) => {
                                let confirmed = rest
                                    .any(|t| matches!(t, "yes" | "y" | "go" | "confirm" | "ok"));
                                launch_vbox_gui(app, vm.to_string(), confirmed, app_tx, out_tx, room);
                            }
                            None => open_vbox_picker(app),
                        }
                    }
                } else if app.sandbox.is_some() || broker.is_some() || *launching {
                    app.sys("a sandbox is already running");
                } else {
                    let backend = first
                        .and_then(sbx::Backend::parse)
                        .unwrap_or(sbx::Backend::Local);
                    let image = pos
                        .next()
                        .map(str::to_string)
                        .unwrap_or_else(|| backend.default_image().to_string());
                    // Is this backend's binary present? Docker/Podman/Multipass can
                    // be installed on consent; Local needs nothing and vbox is
                    // handled in its own branch above. Podman is daemonless and
                    // rootless, so unlike Docker it skips the daemon/sudo gates
                    // below entirely (those are guarded by `== Backend::Docker`).
                    let installed = match backend {
                        sbx::Backend::Docker => sbx::docker_installed(),
                        sbx::Backend::Podman => sbx::podman_installed(),
                        sbx::Backend::Multipass => sbx::multipass_installed(),
                        _ => true,
                    };
                    let token = first.unwrap_or("docker");
                    // GUI is a container-only feature (headless containers get a
                    // published noVNC port). Asking for it on multipass/local is a
                    // no-op we flag rather than silently drop.
                    let gui = want_gui && matches!(backend, sbx::Backend::Docker | sbx::Backend::Podman);
                    if want_gui && !gui {
                        app.sys(format!(
                            "`gui` only applies to docker/podman sandboxes — ignoring it for {}",
                            backend.label()
                        ));
                    }
                    if !installed && !want_install {
                        app.err(format!(
                            "{} is not installed — retry with `/sbx {token} install` to install it (needs sudo)",
                            backend.label()
                        ));
                    } else if installed
                        && backend == sbx::Backend::Docker
                        && !start_daemon
                        && !sbx::docker_daemon_up()
                    {
                        app.err("docker daemon is not running — retry with `/sbx docker --start` to boot it (sudo), or run ./scripts/ensure-docker.sh in a terminal first");
                    } else {
                        // install_first ⇒ binary missing + consent given: install
                        // it off-thread before provisioning (a fresh Docker
                        // install also leaves its daemon up).
                        let install_first = !installed;
                        // Installing Docker needs root; booting its daemon needs
                        // root too — *except* Docker Desktop, whose engine starts
                        // via a per-user systemd unit with no sudo at all.
                        let needs_sudo = install_first
                            || (start_daemon
                                && backend == sbx::Backend::Docker
                                && !sbx::docker_daemon_up()
                                && !sbx::docker_desktop());
                        let sz = term.size().map(|s| (s.width, s.height)).unwrap_or((80, 24));
                        let (rows, cols) = sbx_grid(sz.0, sz.1, &app.layout);
                        let members: Vec<String> =
                            app.users.iter().map(|u| u.username.clone()).collect();
                        if needs_sudo && !sbx::sudo_ready() {
                            // Root is needed but sudo isn't cached. sudo can't prompt
                            // on the tty from inside the raw-mode TUI, so capture the
                            // password in a masked, local-only modal and feed it to
                            // `sudo -S`. The launch fires on submit (see the run loop).
                            app.sudo_prompt = Some(SudoPrompt {
                                password: String::new(),
                                pending: PendingPrivileged::Launch(PendingSudoLaunch {
                                    backend,
                                    image,
                                    members,
                                    rows,
                                    cols,
                                    start_daemon,
                                    install_first,
                                    gui,
                                }),
                            });
                            app.sys("🔒 sudo password needed — type it here (hidden), Enter to launch · Esc cancels. Local only: never sent to the room.");
                        } else if let Some(h) = if gui { sbx::port_holder(sbx::GUI_PORT) } else { None } {
                            // The noVNC port is already bound (a stale sandbox, a
                            // leftover websockify, etc.). Don't blindly collide —
                            // ask the owner whether to kill the holder, then launch.
                            app.sys(format!(
                                "⚠ port {} (noVNC) in use by pid {} ({}) — kill it and proceed? [y/N] · Esc cancels",
                                sbx::GUI_PORT, h.pid, h.name
                            ));
                            app.port_prompt = Some(PortPrompt {
                                pid: h.pid,
                                holder: h.name,
                                pending: PendingGuiLaunch {
                                    backend,
                                    image,
                                    members,
                                    rows,
                                    cols,
                                    start_daemon,
                                    install_first,
                                    gui,
                                    password: None,
                                },
                            });
                        } else {
                            *launching = true;
                            if install_first {
                                app.sys(format!(
                                    "installing {} (needs sudo)… then summoning the sandbox",
                                    backend.label()
                                ));
                            } else {
                                app.sys(format!(
                                    "summoning {} sandbox… (provisioning unix users; multipass boot ~30s)",
                                    backend.label()
                                ));
                            }
                            if gui {
                                app.sys(format!(
                                    "🖥 gui sandbox: installing the desktop (heavy first pull) — when it's up, open http://127.0.0.1:{} in a browser (noVNC, default password 'hackhouse')",
                                    sbx::GUI_PORT
                                ));
                            }
                            spawn_launch(
                                backend,
                                image,
                                app.me.clone(),
                                members,
                                rows,
                                cols,
                                start_daemon,
                                install_first,
                                gui,
                                None,
                                pty_tx.clone(),
                                broker_tx.clone(),
                                app_tx.clone(),
                            );
                        }
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
            Some("save") => {
                // `--local` (alias `-l`) also writes a portable, standalone copy
                // under hh-snapshots/ (a docker `.tar`) the saver fully owns; the
                // default keeps just the in-backend snapshot. The label is the
                // first non-flag arg.
                let args: Vec<&str> = p.collect();
                let local = args.iter().any(|a| matches!(*a, "--local" | "-l"));
                let label = args
                    .iter()
                    .copied()
                    .find(|a| !a.starts_with('-'))
                    .unwrap_or("snap")
                    .to_string();
                if !is_snap_label(&label) {
                    app.sys("snapshot label must be alphanumerics, '.', '_' or '-'");
                } else if let Some((be, name)) = broker_meta.clone() {
                    app.sys(format!(
                        "saving sandbox state as '{label}'{}…",
                        if local { " (+ local copy)" } else { "" }
                    ));
                    // Docker commits a *live* container, so its save is non-
                    // disruptive. Multipass can only snapshot a powered-off
                    // instance, so saving it necessarily stops the shared shell —
                    // tear the live session down (same as `/sbx stop`, but WITHOUT
                    // purging: the instance must survive so the snapshot does too).
                    if be == sbx::Backend::Multipass {
                        if let Some(mut sb) = broker.take() {
                            sb.stop();
                        }
                        broker_meta.take();
                        *announced_dims = None;
                        send_frame(out_tx, room, json!({"_sbx":"status","state":"stopped"}));
                        app.sys("multipass must power off to snapshot — stopping the shared shell, then saving (reload it with `/sbx load`)");
                    }
                    let (tx, lbl) = (app_tx.clone(), label.clone());
                    tokio::spawn(async move {
                        let res = tokio::task::spawn_blocking(move || sbx::save_state(be, &name, &label, local)).await;
                        let _ = match res {
                            Ok(Ok(desc)) => tx.send(Net::Sys(format!(
                                "⛧ saved sandbox → {desc} · reload with `/sbx load {lbl}`"))),
                            Ok(Err(e)) => tx.send(Net::Err(format!("save failed: {e}"))),
                            Err(e) => tx.send(Net::Err(format!("save task: {e}"))),
                        };
                    });
                } else {
                    app.sys("only the sandbox host can /sbx save (launch one first)");
                }
            }
            Some("load") => match p.next() {
                None => app.sys("usage: /sbx load <label>  (a docker, podman or multipass snapshot saved via /sbx save)"),
                Some(label) if !is_snap_label(label) => {
                    app.sys("snapshot label must be alphanumerics, '.', '_' or '-'");
                }
                Some(label) => {
                    if app.sandbox.is_some() || broker.is_some() || *launching {
                        app.sys("stop the current sandbox first (`/sbx stop`) before loading a snapshot");
                    } else {
                        // A bare label is backend-ambiguous, so probe which backend
                        // actually holds the snapshot, then restore through it:
                        // docker reruns a committed image; multipass restores the
                        // (stopped, preserved) instance and re-attaches its shell.
                        let label = label.to_string();
                        let sz = term.size().map(|s| (s.width, s.height)).unwrap_or((80, 24));
                        let (rows, cols) = sbx_grid(sz.0, sz.1, &app.layout);
                        *launching = true;
                        let members: Vec<String> =
                            app.users.iter().map(|u| u.username.clone()).collect();
                        let owner = app.me.clone();
                        app.sys(format!("loading snapshot '{label}'…"));
                        let (pty, btx, atx) =
                            (pty_tx.clone(), broker_tx.clone(), app_tx.clone());
                        tokio::spawn(async move {
                            let (n, lbl) = (SBX_NAME.to_string(), label.clone());
                            let kind = tokio::task::spawn_blocking(move || {
                                sbx::locate_snapshot(&n, &lbl)
                            })
                            .await
                            .unwrap_or(sbx::SnapKind::None);
                            match kind {
                                sbx::SnapKind::Docker => {
                                    if !sbx::docker_daemon_up() {
                                        let _ = atx.send(Net::Err("docker daemon is not running — `/sbx docker --start` once to boot it, then retry".into()));
                                        let _ = btx.send(BrokerMsg::Failed);
                                        return;
                                    }
                                    let image = format!("{}:{}", sbx::SNAP_REPO, label);
                                    let _ = atx.send(Net::Sys(format!("loading docker sandbox from {image}…")));
                                    spawn_launch(
                                        sbx::Backend::Docker, image, owner, members, rows,
                                        cols, false, false, false, None, pty, btx, atx,
                                    );
                                }
                                sbx::SnapKind::Podman => {
                                    // Podman is daemonless — no daemon-up check; the
                                    // committed image reruns directly via the engine.
                                    let image = format!("{}:{}", sbx::SNAP_REPO, label);
                                    let _ = atx.send(Net::Sys(format!("loading podman sandbox from {image}…")));
                                    spawn_launch(
                                        sbx::Backend::Podman, image, owner, members, rows,
                                        cols, false, false, false, None, pty, btx, atx,
                                    );
                                }
                                sbx::SnapKind::Multipass => {
                                    let lbl = label.clone();
                                    let res = tokio::task::spawn_blocking(move || {
                                        sbx::mp_restore(SBX_NAME, &lbl)
                                    })
                                    .await;
                                    match res {
                                        Ok(Ok(desc)) => {
                                            let _ = atx.send(Net::Sys(format!("⛧ {desc} · booting…")));
                                            spawn_launch(
                                                sbx::Backend::Multipass,
                                                sbx::Backend::Multipass.default_image().to_string(),
                                                owner, members, rows, cols, false, false, false, None, pty, btx, atx,
                                            );
                                        }
                                        Ok(Err(e)) => {
                                            let _ = atx.send(Net::Err(format!("load failed: {e}")));
                                            let _ = btx.send(BrokerMsg::Failed);
                                        }
                                        Err(e) => {
                                            let _ = atx.send(Net::Err(format!("load task: {e}")));
                                            let _ = btx.send(BrokerMsg::Failed);
                                        }
                                    }
                                }
                                sbx::SnapKind::None => {
                                    let _ = atx.send(Net::Err(format!("no saved snapshot '{label}' — list with `/sbx snaps`")));
                                    let _ = btx.send(BrokerMsg::Failed);
                                }
                            }
                        });
                    }
                }
            },
            Some("snaps") | Some("snapshots") => {
                let be = broker_meta
                    .as_ref()
                    .map(|(b, _)| *b)
                    .unwrap_or(sbx::Backend::Docker);
                let (tx, name) = (app_tx.clone(), SBX_NAME.to_string());
                tokio::spawn(async move {
                    let res = tokio::task::spawn_blocking(move || sbx::list_snapshots(be, &name)).await;
                    let _ = match res {
                        Ok(Ok(v)) if !v.is_empty() => {
                            tx.send(Net::Sys(format!("saved snapshots: {}", v.join(", "))))
                        }
                        Ok(Ok(_)) => tx.send(Net::Sys(
                            "no saved snapshots yet — `/sbx save [label]` to make one".into())),
                        Ok(Err(e)) => tx.send(Net::Err(format!("snaps: {e}"))),
                        Err(e) => tx.send(Net::Err(format!("snaps task: {e}"))),
                    };
                });
            }
            Some("vms") => {
                let tx = app_tx.clone();
                tokio::spawn(async move {
                    let res = tokio::task::spawn_blocking(|| {
                        let ver = sbx::vbox_version().ok_or_else(|| {
                            "VirtualBox isn't installed — install it with `/sbx gui <vm> --install`, or run ./scripts/ensure-vbox.sh".to_string()
                        })?;
                        let vms = sbx::list_vms().map_err(|e| e.to_string())?;
                        Ok::<_, String>((ver, vms))
                    })
                    .await;
                    let _ = match res {
                        Ok(Ok((ver, v))) if !v.is_empty() => tx.send(Net::Sys(format!(
                            "VirtualBox {ver} detected · VMs: {}",
                            v.join(", ")
                        ))),
                        Ok(Ok((ver, _))) => tx.send(Net::Sys(format!(
                            "VirtualBox {ver} detected · no VMs registered"
                        ))),
                        Ok(Err(e)) => tx.send(Net::Err(e)),
                        Err(e) => tx.send(Net::Err(format!("vms task: {e}"))),
                    };
                });
            }
            Some("vmsave") => {
                // Snapshot a local VirtualBox VM. `--local` (alias `-l`) also
                // exports a portable `.ova` appliance under hh-snapshots/.
                let args: Vec<&str> = p.collect();
                let local = args.iter().any(|a| matches!(*a, "--local" | "-l"));
                let mut pos = args.iter().copied().filter(|a| !a.starts_with('-'));
                match pos.next() {
                    None => app.sys(
                        "usage: /sbx vmsave <vm> [label] [--local]  (snapshot a VirtualBox VM; list VMs with /sbx vms)",
                    ),
                    Some(vm) => {
                        let label = pos.next().unwrap_or("snap").to_string();
                        if !is_snap_label(&label) {
                            app.sys("snapshot label must be alphanumerics, '.', '_' or '-'");
                        } else {
                            app.sys(format!(
                                "snapshotting VM '{vm}' as '{label}'{}…",
                                if local { " (+ local .ova)" } else { "" }
                            ));
                            let (tx, vm) = (app_tx.clone(), vm.to_string());
                            tokio::spawn(async move {
                                let res = tokio::task::spawn_blocking(move || {
                                    sbx::vm_save_state(&vm, &label, local)
                                })
                                .await;
                                let _ = match res {
                                    Ok(Ok(desc)) => tx.send(Net::Sys(format!("⛧ saved VM → {desc}"))),
                                    Ok(Err(e)) => tx.send(Net::Err(format!("vmsave failed: {e}"))),
                                    Err(e) => tx.send(Net::Err(format!("vmsave task: {e}"))),
                                };
                            });
                        }
                    }
                }
            }
            Some("vmsnaps") => {
                match p.next() {
                    None => app.sys("usage: /sbx vmsnaps <vm>  (list a VirtualBox VM's snapshots)"),
                    Some(vm) => {
                        let (tx, vm) = (app_tx.clone(), vm.to_string());
                        tokio::spawn(async move {
                            let res = tokio::task::spawn_blocking(move || sbx::vm_snapshots(&vm)).await;
                            let _ = match res {
                                Ok(Ok(v)) if !v.is_empty() => {
                                    tx.send(Net::Sys(format!("VM snapshots: {}", v.join(", "))))
                                }
                                Ok(Ok(_)) => tx.send(Net::Sys(
                                    "no VM snapshots yet — `/sbx vmsave <vm> [label]` to make one".into())),
                                Ok(Err(e)) => tx.send(Net::Err(format!("vmsnaps: {e}"))),
                                Err(e) => tx.send(Net::Err(format!("vmsnaps task: {e}"))),
                            };
                        });
                    }
                }
            }
            Some("vmload") => {
                // Restore a VirtualBox VM to a snapshot (inverse of /sbx vmsave)
                // then boot its GUI locally. `<label>` picks a named snapshot;
                // omit it to restore the VM's current snapshot.
                let mut vpos = p.filter(|a| !a.starts_with('-'));
                match vpos.next() {
                    None => app.sys(
                        "usage: /sbx vmload <vm> [label]  (restore a VirtualBox snapshot + boot; list with /sbx vmsnaps <vm>)",
                    ),
                    Some(vm) => {
                        let label = vpos.next().map(str::to_string);
                        if label.as_deref().is_some_and(|l| !is_snap_label(l)) {
                            app.sys("snapshot label must be alphanumerics, '.', '_' or '-'");
                        } else {
                            app.sys(format!(
                                "restoring VM '{vm}'{} then booting…",
                                label
                                    .as_deref()
                                    .map(|l| format!(" to '{l}'"))
                                    .unwrap_or_default()
                            ));
                            let (tx, vm) = (app_tx.clone(), vm.to_string());
                            tokio::spawn(async move {
                                let res = tokio::task::spawn_blocking(move || {
                                    let desc = sbx::vm_restore(&vm, label.as_deref())?;
                                    let boot = sbx::gui_launch(&vm)?;
                                    Ok::<_, anyhow::Error>(format!("{desc} · {boot}"))
                                })
                                .await;
                                let _ = match res {
                                    Ok(Ok(desc)) => tx.send(Net::Sys(format!("⛧ {desc}"))),
                                    Ok(Err(e)) => tx.send(Net::Err(format!("vmload failed: {e}"))),
                                    Err(e) => tx.send(Net::Err(format!("vmload task: {e}"))),
                                };
                            });
                        }
                    }
                }
            }
            Some("gui") => {
                // Convenience alias for `/sbx vbox gui <vm> [yes]` — opens a
                // local VirtualBox VM's GUI on your own machine. Bare `/sbx gui`
                // opens the VM picker, same as `/sbx vbox`.
                let mut gpos = p.filter(|a| !a.starts_with('-'));
                match gpos.next() {
                    Some(vm) => {
                        let confirmed =
                            gpos.any(|t| matches!(t, "yes" | "y" | "go" | "confirm" | "ok"));
                        launch_vbox_gui(app, vm.to_string(), confirmed, app_tx, out_tx, room);
                    }
                    None => open_vbox_picker(app),
                }
            }
            other => {
                let usage = "usage: /sbx <type> <option> — /sbx docker|podman|multipass|local [gui] [image] (gui = noVNC desktop, docker/podman only) · /sbx vbox [gui] <vm> [yes] (host opens directly; non-host appends yes) · /sbx vbox new [name] (fresh VM) · vms · stop · save [label] [--local] · load <label> · snaps · vmsave <vm> [label] [--local] · vmload <vm> [label] · vmsnaps <vm>";
                // Bare `/sbx` → usage. An unrecognised subcommand → suggest the
                // closest documented one before the usage line.
                match other.and_then(|bad| closest(bad, SBX_SUBCOMMANDS).map(|s| (bad, s))) {
                    Some((bad, s)) => {
                        app.sys(format!("unknown `/sbx {bad}` — did you mean `/sbx {s}`?  {usage}"))
                    }
                    None => app.sys(usage.to_string()),
                }
            }
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
            // Drop any sandbox drive the agent held so a dead handle can't act.
            app.agent_sbx_allow = false;
            let revoked = app
                .agent_name
                .take()
                .is_some_and(|n| app.drivers.remove(&n) | app.sudoers.remove(&n));
            if revoked && app.sandbox.is_some() {
                broadcast_acl(out_tx, room, app);
            }
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
            // Trailing flag words (any order): `allow` auto-grants the agent
            // sandbox drive on launch; `plain` selects the legacy one-shot
            // injector instead of the default Goose harness.
            let mut raw = rest.trim();
            let mut grant_sbx = false;
            let mut plain = false;
            loop {
                if let Some(head) = raw
                    .strip_suffix("allow")
                    .filter(|h| h.is_empty() || h.ends_with(' '))
                {
                    grant_sbx = true;
                    raw = head.trim();
                    continue;
                }
                if let Some(head) = raw
                    .strip_suffix("plain")
                    .filter(|h| h.is_empty() || h.ends_with(' '))
                {
                    plain = true;
                    raw = head.trim();
                    continue;
                }
                break;
            }
            let harness = if plain { Some("simple") } else { None };
            // A bare name (no ':' tag, no '/' path) is a models.toml profile;
            // anything else is treated as a literal Ollama model tag.
            let (profile, model): (Option<&str>, &str) = if raw.is_empty() {
                (None, "qwen2.5:3b")
            } else if raw.contains(':') || raw.contains('/') {
                (None, raw)
            } else {
                (Some(raw), raw)
            };
            // Name the agent after what it runs, for clarity in the roster: a
            // profile keeps its label; a direct Ollama model uses its tag
            // (e.g. "qwen2.5:3b" — model name + parameter size).
            let name = profile.unwrap_or(model);
            match spawn_agent(params, &app.password, name, profile, model, harness) {
                Ok(child) => {
                    *agent = Some(child);
                    app.agent_name = Some(name.to_string());
                    app.agent_sbx_allow = grant_sbx;
                    let desc = match profile {
                        Some(p) => format!("profile {p}"),
                        None => format!("ollama/{model}"),
                    };
                    let hdesc = if plain { ", simple harness" } else { "" };
                    app.sys(format!(
                        "⛧ summoning {name} ({desc}{hdesc})… it will announce when online"
                    ));
                    if grant_sbx {
                        // Grant now if a sandbox is already running; otherwise the
                        // Ready handler applies it when one launches.
                        if app.sandbox.is_some() && app.owner.as_deref() == Some(app.me.as_str()) {
                            app.drivers.insert(name.to_string());
                            broadcast_acl(out_tx, room, app);
                        }
                        app.sys(format!(
                            "⛧ {name} will get sandbox drive — Ctrl-X kills all drive in a pinch"
                        ));
                    }
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
                              `ollama pull qwen2.5:3b` or run ./scripts/bootstrap-ai.sh"
                        .to_string(),
                    Err(_) => "ollama not reachable at localhost:11434 — run \
                               ./scripts/bootstrap-ai.sh, or `/ai start <profile>` for a cloud model"
                        .to_string(),
                };
                let _ = tx.send(Net::Sys(msg));
            });
        }
    } else if line.starts_with('/') {
        // Leading slash but no command branch matched. Either a known command
        // family that intentionally falls through to chat (notably `/ai
        // <question>`, which a running agent reads from the room), or a typo we
        // should help with instead of silently broadcasting as a chat message.
        let cmd = line.split_whitespace().next().unwrap_or(line);
        if KNOWN_COMMANDS.contains(&cmd) {
            if app.connected {
                let _ = out_tx.send(WsMsg::Text(room.encrypt(line.as_bytes())));
            }
        } else if let Some(s) = closest(cmd, KNOWN_COMMANDS) {
            app.sys(format!("unknown command ‘{cmd}’ — did you mean `{s}`?  (/help lists all)"));
        } else {
            app.sys(format!("unknown command ‘{cmd}’ — /help lists all commands"));
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

/// Every top-level slash command (the leading token). Used both to recognise a
/// known command family that legitimately falls through to chat (e.g. `/ai
/// <question>`) and as the candidate set for the "did you mean" suggester.
const KNOWN_COMMANDS: &[&str] = &[
    "/help", "/?", "/clear", "/cls", "/pw", "/password", "/theme", "/layout", "/drive",
    "/sendroom", "/send", "/accept", "/reject", "/sbx", "/unsudo", "/sudo", "/grant", "/revoke",
    "/ai",
];

/// Canonical `/sbx` subcommands (backends + actions) for the subcommand-level
/// "did you mean". Aliases (`launch`, `virtualbox`, `snapshots`, `gui`) are
/// omitted so suggestions point at the documented form.
const SBX_SUBCOMMANDS: &[&str] = &[
    "docker", "podman", "multipass", "local", "vbox", "stop", "save", "load", "snaps", "vms",
    "vmsave", "vmload", "vmsnaps",
];

/// Classic Levenshtein edit distance (insert/delete/substitute, each cost 1).
fn levenshtein(a: &str, b: &str) -> usize {
    let a: Vec<char> = a.chars().collect();
    let b: Vec<char> = b.chars().collect();
    let mut prev: Vec<usize> = (0..=b.len()).collect();
    let mut cur = vec![0usize; b.len() + 1];
    for (i, &ca) in a.iter().enumerate() {
        cur[0] = i + 1;
        for (j, &cb) in b.iter().enumerate() {
            let cost = usize::from(ca != cb);
            cur[j + 1] = (prev[j + 1] + 1).min(cur[j] + 1).min(prev[j] + cost);
        }
        std::mem::swap(&mut prev, &mut cur);
    }
    prev[b.len()]
}

/// The nearest candidate to `input` by edit distance, if one is close enough to
/// be a plausible typo: distance ≤ 3 and strictly less than the input length (so
/// a tiny stray token doesn't "match" everything). Case-insensitive; ties go to
/// the first candidate. Returns `None` when nothing is close.
fn closest<'a>(input: &str, candidates: &[&'a str]) -> Option<&'a str> {
    let input = input.to_ascii_lowercase();
    let len = input.chars().count();
    candidates
        .iter()
        .map(|c| (levenshtein(&input, &c.to_ascii_lowercase()), *c))
        .filter(|(d, _)| *d <= 3 && *d < len)
        .min_by_key(|(d, _)| *d)
        .map(|(_, c)| c)
}

/// A safe snapshot label — compatible with both a Docker image tag and a
/// multipass snapshot name (alphanumerics plus `.`, `_`, `-`).
fn is_snap_label(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 64
        && s.chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-'))
}

/// Find a local VirtualBox appliance (`.ova`/`.ovf`) for `vm` so a non-host can
/// import ("pull") it. Looks in `hh-snapshots/` (where `/sbx vmsave --local`
/// writes exports) and `./downloads/` (where a /sent appliance lands). Matches
/// on the file stem equalling, prefixing (`<vm>-…`), or containing the VM name.
fn find_local_appliance(vm: &str) -> Option<std::path::PathBuf> {
    let dirs = [
        std::path::PathBuf::from(sbx::SNAP_DIR),
        std::path::PathBuf::from("./downloads"),
    ];
    for dir in dirs {
        let Ok(entries) = std::fs::read_dir(&dir) else {
            continue;
        };
        for e in entries.flatten() {
            let path = e.path();
            let ext_ok = path
                .extension()
                .and_then(|s| s.to_str())
                .map(|s| s.eq_ignore_ascii_case("ova") || s.eq_ignore_ascii_case("ovf"))
                .unwrap_or(false);
            let name_match = path
                .file_stem()
                .and_then(|s| s.to_str())
                .map(|s| s == vm || s.starts_with(&format!("{vm}-")) || s.contains(vm))
                .unwrap_or(false);
            if ext_ok && name_match {
                return Some(path);
            }
        }
    }
    None
}

/// Open the arrow-navigable VM picker (populated synchronously — a single
/// `VBoxManage list vms`, same blocking style as the other vbox probes here).
/// Empty/uninstalled cases steer the user instead of opening an empty list.
fn open_vbox_picker(app: &mut App) {
    if !sbx::vbox_installed() {
        app.sys("VirtualBox isn't installed — `/sbx vbox gui <vm> yes` installs it, or run ./scripts/ensure-vbox.sh");
        return;
    }
    match sbx::list_vms() {
        Ok(vms) if !vms.is_empty() => {
            app.vbox_picker = Some(VboxPicker { vms, selected: 0 });
            app.sys("⛧ pick a VM — ↑/↓ move · Enter/Tab choose · Esc dismiss");
        }
        Ok(_) => app.sys(
            "no VirtualBox VMs registered — a host can /send you a .ova, then `/sbx vbox gui <vm> yes` to import it",
        ),
        Err(e) => app.err(format!("listing VMs: {e}")),
    }
}

/// Launch (or pull-then-launch) a local VirtualBox VM's GUI on the caller's OWN
/// machine — nothing is relayed; every member opens their own copy. Shared by
/// `/sbx vbox gui <vm>` and the `/sbx gui <vm>` alias.
///
/// Frictionless for a "host" who already has VirtualBox AND the VM imported with
/// no other VM holding VT-x: it just boots. A non-host missing VirtualBox or the
/// VM image — or who'd have to stop another VM first — must re-issue with a
/// trailing `yes`, which then installs VirtualBox, imports the shared appliance,
/// and/or frees VT-x before booting.
/// Build + boot a brand-new VirtualBox VM from a cloud image (the only path that
/// creates a VM from scratch — `launch_vbox_gui` opens existing ones). The heavy
/// lifting (image download, cloud-init seed, VBox create/boot) is in
/// scripts/vbox-new.sh; we run it off-thread and report through `app_tx` so the
/// first-run ~600MB download never stalls the TUI.
fn launch_vbox_new(app: &mut App, name: String, user: String, app_tx: &UnboundedSender<Net>) {
    if !is_snap_label(&name) {
        app.sys("VM name must be alphanumerics, '.', '_' or '-'");
        return;
    }
    if !sbx::vbox_installed() {
        app.sys("VirtualBox isn't installed — `/sbx gui <vm> --install` or run ./scripts/ensure-vbox.sh first");
        return;
    }
    if sbx::vm_registered(&name) {
        app.sys(format!(
            "a VM named ‘{name}’ already exists — pick another name, or boot it with `/sbx gui {name}`"
        ));
        return;
    }
    app.sys(format!(
        "building fresh VirtualBox VM ‘{name}’ (first run downloads a ~600MB Ubuntu cloud image; cloud-init installs the toolchain on boot)…"
    ));
    let tx = app_tx.clone();
    tokio::spawn(async move {
        let (n, u) = (name.clone(), user);
        let res = tokio::task::spawn_blocking(move || sbx::vbox_new(&n, &u)).await;
        let _ = match res {
            Ok(Ok(desc)) => tx.send(Net::Sys(format!("⛧ {desc}"))),
            Ok(Err(e)) => tx.send(Net::Err(format!("vbox new failed: {e}"))),
            Err(e) => tx.send(Net::Err(format!("vbox new task: {e}"))),
        };
    });
}

fn launch_vbox_gui(
    app: &mut App,
    vm: String,
    confirmed: bool,
    app_tx: &UnboundedSender<Net>,
    out_tx: &UnboundedSender<WsMsg>,
    room: &Arc<fernet::Fernet>,
) {
    let installed = sbx::vbox_installed();
    if installed && sbx::vm_running(&vm) {
        app.sys(format!(
            "{vm} is already running — look for its window on your desktop"
        ));
        return;
    }
    let have_vm = installed && sbx::vm_registered(&vm);
    let conflicts = sbx::vtx_holders();
    let needs_install = !installed;
    let needs_import = !have_vm;

    // Host path: VirtualBox + this VM already present and nothing to stop → boot
    // immediately, no confirmation.
    if installed && have_vm && conflicts.is_empty() {
        spawn_vm_execute(vm, conflicts, false, false, app, app_tx, out_tx, room, None);
        return;
    }

    // Otherwise a side effect is required (install / import / stop another VM).
    // The non-host opts in with a trailing `yes`.
    if !confirmed {
        let mut steps: Vec<String> = Vec::new();
        if needs_install {
            steps.push("install VirtualBox (needs sudo)".to_string());
        }
        if needs_import {
            match find_local_appliance(&vm) {
                Some(p) => steps.push(format!("import {}", p.display())),
                None => {
                    app.sys(format!(
                        "no local appliance for ‘{vm}’ yet — have the host export it (`/sbx vmsave {vm} --local`) and /send you the .ova, then `/sbx vbox gui {vm} yes`"
                    ));
                    return;
                }
            }
        }
        if !conflicts.is_empty() {
            steps.push(format!("stop {} other VM(s) holding VT-x", conflicts.len()));
        }
        app.sys(format!(
            "opening ‘{vm}’ on YOUR machine will {}.  →  `/sbx vbox gui {vm} yes` to proceed",
            steps.join(", ")
        ));
        return;
    }

    // Confirmed. Refuse VT-x holders we can't cleanly restart rather than kill them.
    if let Some(bad) = conflicts.iter().find(|h| !h.stoppable) {
        app.err(format!(
            "can't free VT-x automatically — {} is running and I won't kill it. Stop it yourself, then retry `/sbx vbox gui {vm} yes`.",
            bad.label
        ));
        return;
    }
    // Installing VirtualBox needs root. If creds aren't cached, capture the
    // password in the same masked, local-only modal the container path uses —
    // sudo can't prompt on the tty from inside the raw-mode TUI. The install +
    // VM boot fires on submit (see the run loop's sudo handler).
    if needs_install && !sbx::sudo_ready() {
        app.sudo_prompt = Some(SudoPrompt {
            password: String::new(),
            pending: PendingPrivileged::VboxInstall(PendingVboxInstall {
                vm,
                conflicts,
                needs_install,
                needs_import,
                room: room.clone(),
            }),
        });
        app.sys("🔒 sudo password needed to install VirtualBox — type it here (hidden), Enter to proceed · Esc cancels. Local only: never sent to the room.");
        return;
    }
    spawn_vm_execute(vm, conflicts, needs_install, needs_import, app, app_tx, out_tx, room, None);
}

/// Final step of the gauntlet: off-thread, stop any VT-x holders (reversibly),
/// install VirtualBox if needed, then boot the VM's GUI. Reports through the
/// app channel so the blocking work never stalls the TUI. On success it also
/// broadcasts a `_sbx:vm` frame so the *other* party sees the shared appliance
/// go live and can `/sbx gui <vm>` their own local copy — anyone in the room may
/// do so (the per-client consent gauntlet *is* the client's permission; this is
/// deliberately not owner-gated, unlike `/sbx save`).
#[allow(clippy::too_many_arguments)] // launch context: vm + flags + 3 channels
fn spawn_vm_execute(
    vm: String,
    conflicts: Vec<sbx::VtxHolder>,
    needs_install: bool,
    needs_import: bool,
    app: &mut App,
    app_tx: &UnboundedSender<Net>,
    out_tx: &UnboundedSender<WsMsg>,
    room: &Arc<fernet::Fernet>,
    // sudo password captured by the masked modal when VBox install needed root
    // and creds weren't cached. Fed to the install's `sudo -S` over stdin; `None`
    // means creds are already cached (the install step uses `sudo -n`).
    password: Option<String>,
) {
    // Resolve the appliance path now (on the app thread) so the blocking task
    // doesn't have to re-scan; None is fine unless an import actually proves
    // necessary inside the task.
    let appliance = if needs_import {
        find_local_appliance(&vm)
    } else {
        None
    };
    if conflicts.is_empty() {
        app.sys(format!("launching ‘{vm}’…"));
    } else {
        app.sys(format!(
            "freeing VT-x ({} VM(s)) then launching ‘{vm}’…",
            conflicts.len()
        ));
    }
    let tx = app_tx.clone();
    let out = out_tx.clone();
    let room = room.clone();
    let announce_vm = vm.clone();
    let pw = password; // moved into the blocking install step below
    tokio::spawn(async move {
        let res = tokio::task::spawn_blocking(move || {
            for h in &conflicts {
                sbx::stop_vtx_holder(h).map_err(|e| format!("freeing VT-x: {e}"))?;
            }
            if needs_install && !sbx::vbox_installed() {
                // VirtualBox install needs root. `pw` is `Some` when the masked
                // modal captured a password (creds weren't cached) → fed to
                // `sudo -S`; `None` means creds are cached → `sudo -n`. Either way
                // it never hangs on a tty prompt the raw-mode TUI can't host.
                sbx::ensure_vbox_install(pw).map_err(|e| format!("install failed: {e}"))?;
            }
            // Pull the shared appliance in if the VM still isn't registered.
            if needs_import && !sbx::vm_registered(&vm) {
                let ova = appliance.ok_or_else(|| {
                    format!("no local .ova appliance found for ‘{vm}’ to import")
                })?;
                sbx::import_appliance(&ova).map_err(|e| format!("import failed: {e}"))?;
            }
            sbx::gui_launch(&vm).map_err(|e| e.to_string())
        })
        .await;
        let _ = match res {
            Ok(Ok(desc)) => {
                // Tell the room the shared VM is live (others can open their own).
                let frame = json!({"_sbx": "vm", "vm": announce_vm});
                let _ = out.send(WsMsg::Text(room.encrypt(frame.to_string().as_bytes())));
                tx.send(Net::Sys(format!("⛧ {desc}")))
            }
            Ok(Err(e)) => tx.send(Net::Err(e)),
            Err(e) => tx.send(Net::Err(format!("gui task: {e}"))),
        };
    });
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
    install_first: bool,
    // GUI sandbox: publish noVNC on the host loopback and provision the desktop
    // stack inside the container. Only honoured for Docker/Podman.
    gui: bool,
    // The sudo password captured by the in-TUI masked prompt, if escalation was
    // needed. Local-only: it is fed to `sudo -S` via stdin inside the blocking
    // install/prepare steps and never enters chat, the PTY, or any outbound frame.
    password: Option<String>,
    pty_tx: UnboundedSender<Vec<u8>>,
    broker_tx: UnboundedSender<BrokerMsg>,
    app_tx: UnboundedSender<Net>,
) {
    tokio::spawn(async move {
        // Optional install step: the backend's binary was missing and the user
        // opted in. Runs before provisioning; failure aborts the launch (and
        // clears the *launching guard via BrokerMsg::Failed).
        if install_first {
            let pw = password.clone();
            let res = tokio::task::spawn_blocking(move || match backend {
                sbx::Backend::Docker => sbx::ensure_docker_install(pw),
                sbx::Backend::Podman => sbx::ensure_podman_install(pw),
                sbx::Backend::Multipass => sbx::ensure_multipass_install(pw),
                _ => Ok(()),
            })
            .await;
            if let Err(e) = res.unwrap_or_else(|e| Err(anyhow::anyhow!("join: {e}"))) {
                let _ = app_tx.send(Net::Err(format!("install failed: {e:#}")));
                let _ = broker_tx.send(BrokerMsg::Failed);
                return;
            }
        }
        let name = SBX_NAME.to_string();
        let prep = {
            let (n, img, pw) = (name.clone(), image.clone(), password.clone());
            tokio::task::spawn_blocking(move || sbx::prepare(backend, &n, &img, start_daemon, pw, gui))
                .await
        };
        if let Err(e) = prep.unwrap_or_else(|e| Err(anyhow::anyhow!("join: {e}"))) {
            let _ = app_tx.send(Net::Err(format!("sandbox prepare failed: {e:#}")));
            let _ = broker_tx.send(BrokerMsg::Failed);
            return;
        }
        // Provision real unix accounts (owner = sudoer) → the shell's run-user.
        let run_user = {
            let (n, o, ms) = (name.clone(), owner.clone(), members.clone());
            tokio::task::spawn_blocking(move || sbx::provision(backend, &n, &o, &ms, gui))
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
    harness: Option<&str>,
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
            cmd.arg("--provider")
                .arg("ollama")
                .arg("--model")
                .arg(model);
        }
    }
    // Override the agent's default `!task` harness (goose) when the launcher
    // asked for the legacy one-shot injector via `/ai start … plain`.
    if let Some(h) = harness {
        cmd.arg("--harness").arg(h);
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

#[cfg(test)]
mod tests {
    use super::{drain_ready, App, Net, Role, User, VboxPicker};
    use tokio::sync::mpsc::unbounded_channel;

    /// The picker highlights the first VM and wraps a clamped selection; this is
    /// the pure state the key handler drives with ↑/↓.
    #[test]
    fn vbox_picker_selection_is_clamped() {
        let mut app = App::new("alice".into());
        app.vbox_picker = Some(VboxPicker {
            vms: vec!["WinLab".into(), "KaliBox".into()],
            selected: 0,
        });
        let p = app.vbox_picker.as_mut().unwrap();
        // moving up from the top stays at 0 (saturating)
        p.selected = p.selected.saturating_sub(1);
        assert_eq!(p.selected, 0);
        // moving down is capped at the last index
        p.selected = (p.selected + 1).min(p.vms.len() - 1);
        p.selected = (p.selected + 1).min(p.vms.len() - 1);
        assert_eq!(p.selected, 1, "selection must not run past the last VM");
    }

    /// A `_sbx:vm` broadcast from ANOTHER member surfaces a "open your own copy"
    /// notice to the room.
    #[test]
    fn vm_opened_from_peer_notifies_the_room() {
        let mut app = App::new("alice".into());
        app.apply(Net::VmOpened {
            by: "bob".into(),
            vm: "WinLab".into(),
        });
        let last = &app.lines.last().unwrap().text;
        assert!(last.contains("bob") && last.contains("WinLab"));
    }

    /// ...but our OWN echo is skipped — we already printed the local "launched"
    /// line, so we must not double-report it.
    #[test]
    fn vm_opened_self_echo_is_skipped() {
        let mut app = App::new("alice".into());
        let before = app.lines.len();
        app.apply(Net::VmOpened {
            by: "alice".into(),
            vm: "WinLab".into(),
        });
        assert_eq!(app.lines.len(), before, "self-echo must not add a line");
    }

    /// `drain_ready` pulls a bounded burst in FIFO order and stops at the cap.
    #[tokio::test]
    async fn drain_ready_is_fifo_and_capped() {
        let (tx, mut rx) = unbounded_channel::<u32>();
        for i in 0..1000 {
            tx.send(i).unwrap();
        }
        // Mimic the loop: one awaited frame, then drain the ready burst.
        let first = rx.recv().await.unwrap();
        let mut buf = vec![first];
        drain_ready(&mut rx, &mut buf, 256);
        assert_eq!(buf.len(), 256, "burst must be capped");
        assert_eq!(buf, (0..256).collect::<Vec<_>>(), "burst must stay FIFO");
    }

    /// An empty channel leaves the buffer untouched (no spurious items, no hang).
    #[tokio::test]
    async fn drain_ready_on_empty_is_a_noop() {
        let (tx, mut rx) = unbounded_channel::<u32>();
        tx.send(7).unwrap();
        let first = rx.recv().await.unwrap();
        let mut buf = vec![first];
        drain_ready(&mut rx, &mut buf, 256);
        assert_eq!(buf, vec![7]);
    }

    /// Regression for the "starting a sandbox stalls chat" bug: chat and a flood of
    /// `_sbx:data` frames share one channel. Handling one frame per redraw would let
    /// chat fall ~800 turns behind; batch draining must surface it within
    /// ceil(801 / 256) = 4 turns no matter how hard the shell is scrolling.
    #[tokio::test]
    async fn chat_surfaces_promptly_under_sbx_flood() {
        let (tx, mut rx) = unbounded_channel::<&'static str>();
        for _ in 0..800 {
            tx.send("sbx").unwrap();
        }
        tx.send("CHAT").unwrap();
        for _ in 0..800 {
            tx.send("sbx").unwrap();
        }

        let mut turns = 0usize;
        let mut saw_chat = false;
        while let Ok(first) = rx.try_recv() {
            let mut burst = vec![first];
            drain_ready(&mut rx, &mut burst, 256);
            turns += 1;
            if burst.contains(&"CHAT") {
                saw_chat = true;
                break;
            }
        }
        assert!(saw_chat, "chat frame must be observed");
        assert!(
            turns <= 4,
            "chat took {turns} turns to surface (expected <= 4)"
        );
    }

    fn user(name: &str) -> User {
        User {
            user_id: format!("id-{name}"),
            username: name.into(),
        }
    }

    /// The host is the first occupant of the roster, and an empty roster has no
    /// host — so a freshly-built app (no users yet) confers the title on nobody.
    #[test]
    fn host_is_first_roster_member() {
        let mut app = App::new("alice".into());
        assert_eq!(app.host(), None, "empty roster has no host");
        app.users = vec![user("alice"), user("bob")];
        assert_eq!(app.host(), Some("alice"));
        // Host follows roster order, not who `me` is.
        app.users = vec![user("bob"), user("alice")];
        assert_eq!(app.host(), Some("bob"));
    }

    /// The host wears the Host badge with zero sandbox activity — Option A: the
    /// title shows the moment they're in the room, not only after a launch.
    #[test]
    fn host_badge_shows_without_a_sandbox() {
        let mut app = App::new("alice".into());
        app.users = vec![user("alice"), user("bob")];
        assert_eq!(app.roles_of("alice"), vec![Role::Host]);
        assert_eq!(app.roles_of("bob"), vec![Role::Member]);
    }

    /// Badges stack: a host who summoned a sandbox (sudoer) and can drive holds
    /// all three at once, in paint order Host → Sudoer → Driver.
    #[test]
    fn roles_stack_additively() {
        let mut app = App::new("alice".into());
        app.users = vec![user("alice"), user("bob")];
        app.sudoers.insert("alice".into());
        app.drivers.insert("alice".into());
        assert_eq!(
            app.roles_of("alice"),
            vec![Role::Host, Role::Sudoer, Role::Driver]
        );
    }

    /// A non-host can hold powers independently of the host title: bob granted
    /// drive shows Driver only — never Host, and not a bare Member.
    #[test]
    fn non_host_powers_are_independent_of_the_title() {
        let mut app = App::new("alice".into());
        app.users = vec![user("alice"), user("bob")];
        app.drivers.insert("bob".into());
        assert_eq!(app.roles_of("bob"), vec![Role::Driver]);
        assert_eq!(app.roles_of("alice"), vec![Role::Host]);
    }
}
