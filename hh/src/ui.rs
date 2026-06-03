//! ratatui rendering — top bar, chat, roster, input.

use crate::app::{App, ChatLine};
use crate::theme::Theme;
use ratatui::layout::{Constraint, Layout, Position, Rect};
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Clear, List, ListItem, Paragraph, Wrap};
use ratatui::Frame;

pub fn draw(f: &mut Frame, app: &App, theme: &Theme) {
    // Paint the whole frame in the theme background first so every panel (and the
    // gaps between them) sits on the same surface. With bg = Reset this is a no-op
    // and we ride the terminal's own colour.
    f.render_widget(
        Block::default().style(Style::default().bg(theme.bg)),
        f.area(),
    );

    let rows = Layout::vertical([
        Constraint::Length(1),
        Constraint::Min(1),
        Constraint::Length(3),
    ])
    .split(f.area());

    draw_top(f, rows[0], app, theme);

    // When a sandbox is live, split the body: chat+roster on top, PTY below.
    let (chat_area, sbx_area) = if app.sandbox.is_some() {
        let split = Layout::vertical([Constraint::Percentage(45), Constraint::Percentage(55)])
            .split(rows[1]);
        (split[0], Some(split[1]))
    } else {
        (rows[1], None)
    };

    let body = Layout::horizontal([Constraint::Min(1), Constraint::Length(theme.roster_width)])
        .split(chat_area);
    draw_chat(f, body[0], app, theme);
    draw_roster(f, body[1], app, theme);
    if let Some(area) = sbx_area {
        draw_sandbox(f, area, app, theme);
    }
    draw_input(f, rows[2], app, theme);

    if app.show_help {
        draw_help(f, f.area(), theme, app.help_scroll);
    }
    if let Some(msg) = &app.error {
        draw_error(f, f.area(), theme, msg);
    }
}

/// Transient error popup, anchored top-right over the clergy so it never bleeds
/// onto the input box. Cleared by the next keypress (see the run loop).
fn draw_error(f: &mut Frame, area: Rect, theme: &Theme, msg: &str) {
    let text = format!("⚠ {msg}");
    let w = area.width.saturating_sub(2).clamp(16, 48);
    let inner = w.saturating_sub(2).max(1);
    let rows = (text.chars().count() as u16).div_ceil(inner) + 2;
    let h = rows.min(area.height.saturating_sub(2)).max(3);
    let x = area.x + area.width.saturating_sub(w + 1); // hug the right edge
    let y = area.y + 1; // just under the top bar, over the clergy
    let rect = Rect {
        x,
        y,
        width: w,
        height: h,
    };
    f.render_widget(Clear, rect);
    let popup = Paragraph::new(text)
        .style(Style::default().fg(theme.title).bg(theme.bg))
        .block(
            Block::bordered()
                .border_style(
                    Style::default()
                        .fg(theme.accent)
                        .add_modifier(Modifier::BOLD),
                )
                .title(Span::styled(
                    format!(" {} error · any key ", theme.sigil),
                    Style::default()
                        .fg(theme.accent)
                        .add_modifier(Modifier::BOLD),
                )),
        )
        .wrap(Wrap { trim: false });
    f.render_widget(popup, rect);
}

fn centered(percent_x: u16, percent_y: u16, area: Rect) -> Rect {
    let vy = (100u16.saturating_sub(percent_y)) / 2;
    let vx = (100u16.saturating_sub(percent_x)) / 2;
    let col = Layout::vertical([
        Constraint::Percentage(vy),
        Constraint::Percentage(percent_y),
        Constraint::Percentage(vy),
    ])
    .split(area)[1];
    Layout::horizontal([
        Constraint::Percentage(vx),
        Constraint::Percentage(percent_x),
        Constraint::Percentage(vx),
    ])
    .split(col)[1]
}

// Popup geometry — shared by the renderer and the scroll-clamp helper so the two
// always agree on how many rows are visible.
const HELP_PCT_X: u16 = 78;
const HELP_PCT_Y: u16 = 90;
fn help_popup(area: Rect) -> Rect {
    centered(HELP_PCT_X, HELP_PCT_Y, area)
}

/// Largest vertical scroll offset for the help overlay given the full terminal
/// area: total wrapped content rows minus the visible viewport (0 if it all fits).
pub fn help_max_scroll(width: u16, height: u16, theme: &Theme) -> u16 {
    let w = help_popup(Rect::new(0, 0, width, height));
    let inner_w = w.width.saturating_sub(2);
    let inner_h = w.height.saturating_sub(2);
    if inner_w == 0 {
        return 0;
    }
    let total = Paragraph::new(help_lines(theme))
        .wrap(Wrap { trim: false })
        .line_count(inner_w) as u16;
    total.saturating_sub(inner_h)
}

fn help_lines(theme: &Theme) -> Vec<Line<'static>> {
    let acc = Style::default()
        .fg(theme.accent)
        .add_modifier(Modifier::BOLD);
    let key = Style::default().fg(theme.title);
    let dim = Style::default().fg(theme.system);
    let kv = |k: &str, v: &str| {
        Line::from(vec![
            Span::styled(format!("  {k:<26}"), key),
            Span::styled(v.to_string(), dim),
        ])
    };
    let sig = theme.sigil.clone();
    let head = |s: &str| Line::from(Span::styled(format!("{sig} {s}"), acc));
    // List whatever vestments are actually installed, so new themes show up here
    // automatically (church · neon · crypt · blush · matrix · wraith · …).
    let theme_help = format!(
        "vestments: {} · random · save [name]",
        Theme::available().join(" · ")
    );
    vec![
        head("COMMANDS (type in the input bar)"),
        kv(
            "/sbx launch [backend]",
            "summon a sandbox: local | docker | multipass",
        ),
        kv("/sbx stop", "tear down the sandbox (purges the VM)"),
        kv("/drive", "type into the shared shell  (Esc releases)"),
        kv(
            "/ai start [model|profile]",
            "spawn an AI agent  (ollama model tag, or a models.toml profile)",
        ),
        kv("/ai stop", "dismiss the agent you started"),
        kv(
            "/ai <question>",
            "ask an AI agent in the room (/ai <name> <q> if many)",
        ),
        kv("/ai list", "list AI agents present + their provider/model"),
        kv("/ai models", "show models the active agent's backend can serve"),
        kv(
            "/grant <user>",
            "let a member drive the shell        (owner)",
        ),
        kv(
            "/revoke <user>",
            "take back drive permission          (owner)",
        ),
        kv(
            "/sudo <user>",
            "delegate VM superuser (real sudo)   (owner)",
        ),
        kv(
            "/unsudo <user>",
            "revoke VM superuser                 (owner)",
        ),
        kv("/send <file>", "offer a file to the room"),
        kv("/sendd <dir>", "offer a directory (sent as a tar)"),
        kv("/accept  ·  /reject", "respond to an incoming file offer"),
        kv("/theme [name]", &theme_help),
        kv("/pw", "show this room's password (local only)"),
        kv("/help", "show / hide this menu"),
        Line::from(""),
        head("KEYS"),
        kv("Enter", "send chat message"),
        kv("F1  ·  /help", "toggle this help (any key closes it)"),
        kv("F2  ·  /drive", "take the shell  ·  Esc releases it"),
        kv("Ctrl-C  (while driving)", "interrupt the running command"),
        kv("PgUp / PgDn", "scroll chat  ·  Home/End = oldest/live"),
        kv(
            "PgUp / PgDn  (driving)",
            "scroll the sandbox terminal's scrollback",
        ),
        kv(
            "Up / Down  ·  wheel",
            "scroll the sandbox terminal (mouse works while driving)",
        ),
        kv(
            "Ctrl-R  (when closed)",
            "reconnect to the house after a drop / AFK",
        ),
        kv(
            "Ctrl+Alt+P  ·  /theme random",
            "conjure a random vestment (new palette + sigil)",
        ),
        kv(
            "/theme save [name]",
            "keep the vestment you're wearing for reuse",
        ),
        kv("Ctrl-C  ·  Ctrl-Q", "quit hack-house"),
        Line::from(""),
        head("ROSTER GLYPHS"),
        kv(
            &format!("{} owner   ⚡ sudoer", theme.sigil),
            "◆ may drive    • member",
        ),
        Line::from(""),
        Line::from(Span::styled(
            "  malware bless · ↑/↓ · PgUp/PgDn scroll · Esc closes",
            Style::default()
                .fg(theme.dim)
                .add_modifier(Modifier::ITALIC),
        )),
    ]
}

fn draw_help(f: &mut Frame, area: Rect, theme: &Theme, scroll: u16) {
    let w = help_popup(area);
    let inner_w = w.width.saturating_sub(2);
    let inner_h = w.height.saturating_sub(2);
    let para = Paragraph::new(help_lines(theme)).wrap(Wrap { trim: false });
    // Clamp so you can never scroll past the last line; show a hint when there's
    // more below the fold.
    let max = (para.line_count(inner_w) as u16).saturating_sub(inner_h);
    let off = scroll.min(max);
    let title = if max == 0 {
        format!(" {0} hack-house — help {0} ", theme.sigil)
    } else {
        format!(" {0} hack-house — help {0}  ({off}/{max} ▼) ", theme.sigil)
    };
    f.render_widget(Clear, w);
    let help = para
        .style(Style::default().bg(theme.bg)) // fill the popup with the theme surface
        .block(
            Block::bordered()
                .border_style(Style::default().fg(theme.accent))
                .title(Span::styled(
                    title,
                    Style::default()
                        .fg(theme.title)
                        .add_modifier(Modifier::BOLD),
                )),
        )
        .scroll((off, 0));
    f.render_widget(help, w);
}

fn draw_sandbox(f: &mut Frame, area: ratatui::layout::Rect, app: &App, theme: &Theme) {
    let Some(sv) = &app.sandbox else { return };
    let screen = sv.parser.screen();
    let (_rows, cols) = screen.size();
    let lines: Vec<Line> = screen
        .rows(0, cols)
        .map(|r| Line::from(Span::styled(r, Style::default().fg(theme.title))))
        .collect();
    let drive = if app.driving && app.sbx_scroll > 0 {
        format!(" · DRIVING · ↑{} scrollback (PgDn=live)", app.sbx_scroll)
    } else if app.driving {
        " · DRIVING — type here · Esc · PgUp/wheel scroll".to_string()
    } else if app.sbx_scroll > 0 {
        format!(" · ↑{} scrollback (↓/End=live)", app.sbx_scroll)
    } else {
        " · /drive (or F2) · ↑/↓/wheel scroll".to_string()
    };
    let title = format!(" sandbox · {}{} ", sv.backend, drive);
    let border = if app.driving {
        theme.accent
    } else {
        theme.border
    };
    let pane = Paragraph::new(lines).block(
        Block::bordered()
            .border_style(Style::default().fg(border))
            .title(Span::styled(title, Style::default().fg(theme.title))),
    );
    f.render_widget(pane, area);
}

fn draw_top(f: &mut Frame, area: ratatui::layout::Rect, app: &App, theme: &Theme) {
    let cap = if app.capacity > 0 {
        app.capacity
    } else {
        app.users.len()
    };
    let status = if app.connected {
        "🔒 e2e"
    } else if app.reconnecting {
        "… reconnecting"
    } else {
        "✖ closed · Ctrl-R to reconnect"
    };
    let bar = Line::from(vec![
        Span::styled(
            format!(" {0} hack-house {0} ", theme.sigil),
            Style::default()
                .fg(theme.accent)
                .add_modifier(Modifier::BOLD),
        ),
        Span::styled(format!("· {status} "), Style::default().fg(theme.dim)),
        Span::styled(
            format!("· house {}/{} ", app.users.len(), cap),
            Style::default().fg(theme.title),
        ),
    ]);
    f.render_widget(Paragraph::new(bar), area);
}

fn fmt_line<'a>(l: &'a ChatLine, app: &App, theme: &Theme) -> Line<'a> {
    if l.system {
        // System lines carry the canonical ⛧ as a placeholder for "the house
        // sigil"; swap it for the active theme's sigil so e.g. crypt shows ✝,
        // never a pentagram. User messages (l.system == false) are left as typed.
        let text = l.text.replace('⛧', &theme.sigil);
        return Line::from(Span::styled(
            format!("  {} {}", theme.sigil, text),
            Style::default()
                .fg(theme.system)
                .add_modifier(Modifier::ITALIC),
        ));
    }
    let name_color = if l.username == app.me {
        theme.me
    } else {
        theme.other
    };
    Line::from(vec![
        Span::styled(format!("{} ", l.ts), Style::default().fg(theme.dim)),
        Span::styled(
            l.username.clone(),
            Style::default().fg(name_color).add_modifier(Modifier::BOLD),
        ),
        Span::styled(": ", Style::default().fg(theme.dim)),
        Span::styled(l.text.as_str(), Style::default().fg(theme.title)),
    ])
}

fn draw_chat(f: &mut Frame, area: ratatui::layout::Rect, app: &App, theme: &Theme) {
    let inner_h = area.height.saturating_sub(2) as usize; // rows inside the border
    let text_w = area.width.saturating_sub(2).max(1); // wrap width inside the border
    let lines: Vec<Line> = app.lines.iter().map(|l| fmt_line(l, app, theme)).collect();

    // Measure the TRUE wrapped height and scroll to the bottom. Selecting N
    // logical lines and letting the Paragraph top-anchor them clips any wrapped
    // line off the bottom — so the newest messages stay hidden until later ones
    // push them up into view (worse when the sandbox shrinks the chat pane).
    let total_rows = Paragraph::new(lines.clone())
        .wrap(Wrap { trim: false })
        .line_count(text_w);
    let max_scroll = total_rows.saturating_sub(inner_h);
    let scroll = max_scroll.saturating_sub(app.chat_scroll) as u16;

    let title = if app.chat_scroll > 0 {
        format!(" chat ↑{} (End=live) ", app.chat_scroll)
    } else {
        " chat ".to_string()
    };
    let chat = Paragraph::new(lines)
        .block(
            Block::bordered()
                .border_style(Style::default().fg(theme.border))
                .title(Span::styled(title, Style::default().fg(theme.title))),
        )
        .wrap(Wrap { trim: false })
        .scroll((scroll, 0));
    f.render_widget(chat, area);
}

fn draw_roster(f: &mut Frame, area: ratatui::layout::Rect, app: &App, theme: &Theme) {
    let items: Vec<ListItem> = app
        .users
        .iter()
        .map(|u| {
            let me = u.username == app.me;
            // <sigil> owner · ⚡ sudoer (VM superuser) · ◆ may drive · • member
            let owner = app.owner.as_deref() == Some(u.username.as_str());
            let mark = if owner {
                theme.sigil.as_str()
            } else if app.sudoers.contains(&u.username) {
                "⚡"
            } else if app.drivers.contains(&u.username) {
                "◆"
            } else {
                "•"
            };
            let color = if me { theme.roster_me } else { theme.other };
            ListItem::new(Line::from(Span::styled(
                format!(" {mark} {}", u.username),
                Style::default().fg(color),
            )))
        })
        .collect();
    let roster = List::new(items).block(
        Block::bordered()
            .border_style(Style::default().fg(theme.border))
            .title(Span::styled(" clergy ", Style::default().fg(theme.title))),
    );
    f.render_widget(roster, area);
}

/// Animated "⠋ oracle is thinking…" title shown while AI agents generate a reply.
fn ai_thinking_title(app: &App) -> String {
    const FRAMES: [&str; 10] = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];
    let glyph = FRAMES[(app.spin / 2) % FRAMES.len()];
    let mut names: Vec<&str> = app.ai_typing.iter().map(String::as_str).collect();
    names.sort_unstable();
    let who = names.join(", ");
    let verb = if names.len() > 1 { "are" } else { "is" };
    format!(" {glyph} {who} {verb} thinking… ")
}

fn draw_input(f: &mut Frame, area: ratatui::layout::Rect, app: &App, theme: &Theme) {
    let input = Paragraph::new(Line::from(vec![
        Span::styled("> ", Style::default().fg(theme.accent)),
        Span::styled(app.input.as_str(), Style::default().fg(theme.input)),
    ]))
    .block(
        Block::bordered()
            .border_style(Style::default().fg(if app.pending_offer.is_some() {
                theme.accent
            } else {
                theme.border
            }))
            .title(Span::styled(
                match &app.pending_offer {
                    Some(o) => format!(
                        " {} incoming: {} — /accept or /reject ",
                        theme.sigil, o.name
                    ),
                    None if app.driving => {
                        format!(" {} DRIVING the shell — Esc to release ", theme.sigil)
                    }
                    None if !app.ai_typing.is_empty() => ai_thinking_title(app),
                    None => " message · enter send · /drive for shell · ctrl-q quit ".to_string(),
                },
                Style::default().fg(if app.ai_typing.is_empty() {
                    theme.title
                } else {
                    theme.accent
                }),
            )),
    );
    f.render_widget(input, area);

    // Cursor after the "> " prompt + current input.
    let cx = area.x + 3 + app.input.chars().count() as u16;
    let cy = area.y + 1;
    if cx < area.x + area.width.saturating_sub(1) {
        f.set_cursor_position(Position::new(cx, cy));
    }
}
