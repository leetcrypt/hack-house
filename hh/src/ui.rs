//! ratatui rendering — top bar, chat, roster, input.

use crate::app::{App, ChatLine};
use crate::theme::Theme;
use ratatui::layout::{Constraint, Layout, Position, Rect};
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Clear, List, ListItem, Paragraph, Wrap};
use ratatui::Frame;

pub fn draw(f: &mut Frame, app: &App, theme: &Theme) {
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
        draw_help(f, f.area(), theme);
    }
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

fn draw_help(f: &mut Frame, area: Rect, theme: &Theme) {
    let acc = Style::default().fg(theme.accent).add_modifier(Modifier::BOLD);
    let key = Style::default().fg(theme.title);
    let dim = Style::default().fg(theme.system);
    let kv = |k: &str, v: &str| {
        Line::from(vec![
            Span::styled(format!("  {k:<26}"), key),
            Span::styled(v.to_string(), dim),
        ])
    };
    let head = |s: &str| Line::from(Span::styled(s.to_string(), acc));
    let lines = vec![
        head("⛧ COMMANDS (type in the input bar)"),
        kv("/sbx launch [backend]", "summon a sandbox: local | docker | multipass"),
        kv("/sbx stop", "tear down the sandbox (purges the VM)"),
        kv("/drive", "type into the shared shell  (Esc releases)"),
        kv("/grant <user>", "let a member drive the shell        (owner)"),
        kv("/revoke <user>", "take back drive permission          (owner)"),
        kv("/sudo <user>", "delegate VM superuser (real sudo)   (owner)"),
        kv("/unsudo <user>", "revoke VM superuser                 (owner)"),
        kv("/send <file>", "offer a file to the room"),
        kv("/sendd <dir>", "offer a directory (sent as a tar)"),
        kv("/accept  ·  /reject", "respond to an incoming file offer"),
        kv("/help", "show / hide this menu"),
        Line::from(""),
        head("⛧ KEYS"),
        kv("Enter", "send chat message"),
        kv("F1  ·  /help", "toggle this help (any key closes it)"),
        kv("F2  ·  /drive", "take the shell  ·  Esc releases it"),
        kv("Ctrl-C  (while driving)", "interrupt the running command"),
        kv("PgUp / PgDn", "scroll chat  ·  Home/End = oldest/live"),
        kv("Up / Down", "scroll the sandbox terminal (when not driving)"),
        kv("Ctrl-Q", "quit hack-house"),
        Line::from(""),
        head("⛧ ROSTER GLYPHS"),
        kv("⛧ owner   ⚡ sudoer", "◆ may drive    • member"),
        Line::from(""),
        Line::from(Span::styled(
            "  malware bless · press any key to close",
            Style::default().fg(theme.dim).add_modifier(Modifier::ITALIC),
        )),
    ];
    let w = centered(78, 90, area);
    f.render_widget(Clear, w);
    let help = Paragraph::new(lines)
        .block(
            Block::bordered()
                .border_style(Style::default().fg(theme.accent))
                .title(Span::styled(
                    " ⛧ hack-house — help ⛧ ",
                    Style::default().fg(theme.title).add_modifier(Modifier::BOLD),
                )),
        )
        .wrap(Wrap { trim: false });
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
    let drive = if app.driving {
        " · DRIVING — type here · Esc to release".to_string()
    } else if app.sbx_scroll > 0 {
        format!(" · ↑{} scrollback (↓/End=live)", app.sbx_scroll)
    } else {
        " · /drive (or F2) · ↑/↓ scroll".to_string()
    };
    let title = format!(" sandbox · {}{} ", sv.backend, drive);
    let border = if app.driving { theme.accent } else { theme.border };
    let pane = Paragraph::new(lines).block(
        Block::bordered()
            .border_style(Style::default().fg(border))
            .title(Span::styled(title, Style::default().fg(theme.title))),
    );
    f.render_widget(pane, area);
}

fn draw_top(f: &mut Frame, area: ratatui::layout::Rect, app: &App, theme: &Theme) {
    let cap = if app.capacity > 0 { app.capacity } else { app.users.len() };
    let status = if app.connected { "🔒 e2e" } else { "✖ closed" };
    let bar = Line::from(vec![
        Span::styled(
            " ⛧ hack-house ⛧ ",
            Style::default().fg(theme.accent).add_modifier(Modifier::BOLD),
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
        return Line::from(Span::styled(
            format!("  ⛧ {}", l.text),
            Style::default().fg(theme.system).add_modifier(Modifier::ITALIC),
        ));
    }
    let name_color = if l.username == app.me { theme.me } else { theme.other };
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
    let visible = area.height.saturating_sub(2) as usize;
    let len = app.lines.len();
    // Window ends `chat_scroll` lines above the live bottom.
    let end = len.saturating_sub(app.chat_scroll);
    let start = end.saturating_sub(visible);
    let lines: Vec<Line> = app.lines[start..end].iter().map(|l| fmt_line(l, app, theme)).collect();
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
        .wrap(Wrap { trim: false });
    f.render_widget(chat, area);
}

fn draw_roster(f: &mut Frame, area: ratatui::layout::Rect, app: &App, theme: &Theme) {
    let items: Vec<ListItem> = app
        .users
        .iter()
        .map(|u| {
            let me = u.username == app.me;
            // ⛧ owner · ⚡ sudoer (VM superuser) · ◆ may drive · • member
            let owner = app.owner.as_deref() == Some(u.username.as_str());
            let mark = if owner {
                "⛧"
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
            .title(Span::styled(" coven ", Style::default().fg(theme.title))),
    );
    f.render_widget(roster, area);
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
                    Some(o) => format!(" ⛧ incoming: {} — /accept or /reject ", o.name),
                    None if app.driving => " ⛧ DRIVING the shell — Esc to release ".to_string(),
                    None => " message · enter send · /drive for shell · ctrl-q quit ".to_string(),
                },
                Style::default().fg(theme.title),
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
