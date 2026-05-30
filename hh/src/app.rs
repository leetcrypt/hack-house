//! TUI application state, network event model, and the async run loop.

use crate::net::{self, Session};
use crate::theme::Theme;
use crate::ui;
use anyhow::Result;
use crossterm::event::{Event, EventStream, KeyCode, KeyEventKind, KeyModifiers};
use crossterm::terminal::{
    disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen,
};
use crossterm::execute;
use futures_util::{SinkExt, StreamExt};
use ratatui::backend::CrosstermBackend;
use ratatui::Terminal;
use std::time::Duration;
use tokio::sync::mpsc::unbounded_channel;
use tokio_tungstenite::tungstenite::Message as WsMsg;

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
    Closed,
}

pub struct App {
    pub me: String,
    pub lines: Vec<ChatLine>,
    pub users: Vec<User>,
    pub capacity: usize,
    pub input: String,
    pub connected: bool,
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
            Net::Closed => {
                self.connected = false;
                self.sys("connection closed");
            }
        }
    }
}

/// Authenticate already done; connect the websocket and drive the UI.
pub async fn run(session: Session, theme: Theme) -> Result<()> {
    let ws = net::connect(&session).await?;
    let (mut write, read) = ws.split();
    let (tx, mut rx) = unbounded_channel::<Net>();
    tokio::spawn(net::reader(read, session.room.clone(), tx));

    enable_raw_mode()?;
    let mut stdout = std::io::stdout();
    execute!(stdout, EnterAlternateScreen)?;
    let mut term = Terminal::new(CrosstermBackend::new(stdout))?;

    let mut app = App::new(session.username.clone());
    let mut events = EventStream::new();
    let mut tick = tokio::time::interval(Duration::from_millis(200));

    let result = loop {
        if let Err(e) = term.draw(|f| ui::draw(f, &app, &theme)) {
            break Err(e.into());
        }

        tokio::select! {
            maybe = events.next() => {
                match maybe {
                    Some(Ok(Event::Key(k))) if k.kind == KeyEventKind::Press => {
                        match (k.modifiers, k.code) {
                            (KeyModifiers::CONTROL, KeyCode::Char('c')) => break Ok(()),
                            (_, KeyCode::Esc) => break Ok(()),
                            (_, KeyCode::Enter) => {
                                let line = app.input.trim().to_string();
                                app.input.clear();
                                if !line.is_empty() && app.connected {
                                    let ct = session.room.encrypt(line.as_bytes());
                                    if write.send(WsMsg::Text(ct)).await.is_err() {
                                        app.connected = false;
                                    }
                                }
                            }
                            (_, KeyCode::Backspace) => { app.input.pop(); }
                            (_, KeyCode::Char(c)) => app.input.push(c),
                            _ => {}
                        }
                    }
                    Some(Err(e)) => break Err(e.into()),
                    _ => {}
                }
            }
            net = rx.recv() => {
                match net {
                    Some(n) => app.apply(n),
                    None => break Ok(()),
                }
            }
            _ = tick.tick() => {}
        }
    };

    disable_raw_mode()?;
    execute!(term.backend_mut(), LeaveAlternateScreen)?;
    term.show_cursor()?;
    result
}
