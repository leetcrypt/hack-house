//! ratatui rendering — top bar, chat, roster, input.

use crate::app::{App, ChatLine};
use crate::theme::Theme;
use ratatui::layout::{Constraint, Layout, Position};
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, List, ListItem, Paragraph, Wrap};
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
        " · DRIVING (esc to release)"
    } else {
        " · F2 to drive"
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
    let start = app.lines.len().saturating_sub(visible);
    let lines: Vec<Line> = app.lines[start..].iter().map(|l| fmt_line(l, app, theme)).collect();
    let chat = Paragraph::new(lines)
        .block(
            Block::bordered()
                .border_style(Style::default().fg(theme.border))
                .title(Span::styled(" chat ", Style::default().fg(theme.title))),
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
            let mark = if me { "⛧" } else { "•" };
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
            .border_style(Style::default().fg(theme.border))
            .title(Span::styled(
                " message · enter send · esc quit ",
                Style::default().fg(theme.dim),
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
