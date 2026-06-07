//! ratatui rendering — top bar, chat, roster, input.

use crate::app::{App, ChatLine, Role};
use crate::layout::Zoom;
use crate::theme::Theme;
use ratatui::layout::{Constraint, Layout, Position, Rect};
use ratatui::style::{Color, Modifier, Style};
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

    // Divide the body into chat / roster / sandbox-terminal rects. `body_areas`
    // is the single source of truth for that geometry, shared with `pane_at`
    // (mouse hit-testing) so what you click is exactly what's painted.
    let areas = body_areas(rows[1], app);
    if let Some(chat_area) = areas.chat {
        draw_chat(f, chat_area, app, theme);
    }
    if let Some(roster_area) = areas.roster {
        draw_roster(f, roster_area, app, theme);
    }
    if let Some(area) = areas.sbx {
        draw_sandbox(f, area, app, theme);
    }
    draw_input(f, rows[2], app, theme);

    if app.show_help {
        draw_help(f, f.area(), app, theme);
    }
    if app.vbox_picker.is_some() {
        draw_vbox_picker(f, f.area(), app, theme);
    }
    if let Some(msg) = &app.error {
        draw_error(f, f.area(), theme, msg);
    }
}

/// The body's pane rectangles. Any field is `None` when that pane is hidden
/// (terminal absent, fullscreen zoom, or roster width 0).
struct BodyAreas {
    chat: Option<Rect>,
    roster: Option<Rect>,
    sbx: Option<Rect>,
}

/// Carve the body region (`rows[1]`) into chat / roster / sandbox rectangles,
/// honouring the sandbox presence, `Zoom`, and roster width. This is the single
/// source of truth used by both `draw` (painting) and `pane_at` (hit-testing).
fn body_areas(body: Rect, app: &App) -> BodyAreas {
    // Vertical split: chat-column vs sandbox terminal.
    let (chat_col, sbx) = if app.sandbox.is_some() {
        match app.layout.zoom {
            Zoom::Term => (None, Some(body)), // terminal fullscreen
            Zoom::Chat => (Some(body), None), // chat fullscreen (terminal hidden)
            Zoom::Normal => {
                let pty = app.layout.pty_pct;
                let split = Layout::vertical([
                    Constraint::Percentage(100 - pty),
                    Constraint::Percentage(pty),
                ])
                .split(body);
                (Some(split[0]), Some(split[1]))
            }
        }
    } else {
        (Some(body), None) // no sandbox → chat owns the whole body
    };

    // Horizontal split of the chat column into chat vs roster.
    let (chat, roster) = match chat_col {
        Some(col) if app.layout.roster_width != 0 => {
            let lr = Layout::horizontal([
                Constraint::Min(1),
                Constraint::Length(app.layout.roster_width),
            ])
            .split(col);
            (Some(lr[0]), Some(lr[1]))
        }
        Some(col) => (Some(col), None), // roster hidden
        None => (None, None),
    };

    BodyAreas { chat, roster, sbx }
}

/// Hit-test a screen cell against the laid-out panes, for click-to-select in
/// interactive layout editing. Recomputes the exact rects `draw` used (via the
/// shared `body_areas`) so a click lands on the pane the user actually sees.
pub fn pane_at(w: u16, h: u16, app: &App, col: u16, row: u16) -> Option<crate::app::Pane> {
    use crate::app::Pane;
    let area = Rect {
        x: 0,
        y: 0,
        width: w,
        height: h,
    };
    // Mirror draw()'s top-bar / body / input split; only the body is selectable.
    let rows = Layout::vertical([
        Constraint::Length(1),
        Constraint::Min(1),
        Constraint::Length(3),
    ])
    .split(area);
    let areas = body_areas(rows[1], app);
    let p = Position { x: col, y: row };
    if areas.sbx.is_some_and(|r| r.contains(p)) {
        Some(Pane::Terminal)
    } else if areas.roster.is_some_and(|r| r.contains(p)) {
        Some(Pane::Roster)
    } else if areas.chat.is_some_and(|r| r.contains(p)) {
        Some(Pane::Chat)
    } else {
        None
    }
}

/// Border decoration for a pane: when it's the one selected for interactive
/// resize (F5 / click), give it a bold accent border and an "✎" title marker so
/// it's obvious which pane the arrows will move; otherwise the plain `base`.
fn edit_decor(app: &App, pane: crate::app::Pane, theme: &Theme, base: Color) -> (Style, &'static str) {
    if app.focused_pane == Some(pane) {
        (
            Style::default()
                .fg(theme.accent)
                .add_modifier(Modifier::BOLD),
            "✎ ",
        )
    } else {
        (Style::default().fg(base), "")
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
pub fn help_max_scroll(width: u16, height: u16, app: &App, theme: &Theme) -> u16 {
    let w = help_popup(Rect::new(0, 0, width, height));
    let inner_w = w.width.saturating_sub(2);
    let inner_h = w.height.saturating_sub(2);
    if inner_w == 0 {
        return 0;
    }
    let total = Paragraph::new(help_render_lines(app, theme))
        .wrap(Wrap { trim: false })
        .line_count(inner_w) as u16;
    total.saturating_sub(inner_h)
}

/// One named, collapsible group of help entries.
struct HelpCluster {
    title: &'static str,
    items: Vec<(String, String)>,
}

/// The help content, grouped into topical clusters. Order here is the order the
/// up/down highlight walks through.
fn help_clusters(theme: &Theme) -> Vec<HelpCluster> {
    let kv = |k: &str, v: &str| (k.to_string(), v.to_string());
    // List whatever vestments are actually installed, so new themes show up here
    // automatically (church · neon · crypt · blush · matrix · wraith · …).
    let theme_help = format!(
        "vestments: {} · random · save [name]",
        Theme::available().join(" · ")
    );
    vec![
        HelpCluster {
            title: "VIRTUAL MACHINES",
            items: vec![
                // ── launch (one verb per backend) ──
                kv(
                    "/sbx launch docker [image] [install]",
                    "Linux container — shared shell relayed to the room (default ubuntu:24.04 + auto dev toolchain; append install if Docker is missing)",
                ),
                kv(
                    "/sbx launch multipass [image] [install]",
                    "full Ubuntu VM — shared shell relayed to the room (same dev toolchain; append install if Multipass is missing)",
                ),
                kv(
                    "/sbx launch vbox",
                    "VirtualBox VM picker — local GUI (↑↓ move · Enter/Tab boot · Esc dismiss)",
                ),
                kv(
                    "/sbx launch vbox new [name]",
                    "build a FRESH Ubuntu VM from a cloud image (cloud-init installs the dev toolchain)",
                ),
                kv(
                    "/sbx launch vbox [gui] <vm> [yes]",
                    "boot a VirtualBox VM's GUI on YOUR machine (non-host appends yes to install/import first)",
                ),
                kv("/sbx launch local", "a plain shell on your own machine — no VM"),
                kv("/sbx stop", "tear down the running sandbox (purges the VM/container)"),
                // ── save (docker/multipass share a verb; vbox has its own) ──
                kv(
                    "/sbx save [label] [--local]",
                    "save docker/multipass state (--local on docker also writes a portable .tar)",
                ),
                kv(
                    "/sbx vmsave <vm> [label] [--local]",
                    "save a VirtualBox VM snapshot (--local also exports a portable .ova to /send)",
                ),
                // ── load + list ──
                kv(
                    "/sbx load <label>",
                    "relaunch a saved docker/multipass snapshot (auto-detects backend)",
                ),
                kv(
                    "/sbx vmload <vm> [label]",
                    "restore a VirtualBox snapshot + boot (omit label for the VM's current snapshot)",
                ),
                kv("/sbx snaps", "list saved docker/multipass snapshots"),
                kv(
                    "/sbx vms  ·  /sbx vmsnaps <vm>",
                    "list local VirtualBox VMs · a VM's snapshots",
                ),
                kv("/sbx gui <vm> [yes]", "alias of /sbx launch vbox gui <vm> [yes]"),
                kv("/drive  ·  F2", "type into the shared shell (F2 releases; Esc reaches vim)"),
            ],
        },
        HelpCluster {
            title: "AI AGENTS",
            items: vec![
                kv(
                    "/ai start [model|profile]",
                    "spawn an agent (ollama tag or models.toml profile)",
                ),
                kv(
                    "/ai start <model> allow",
                    "spawn + auto-grant the agent sandbox drive",
                ),
                kv("/ai stop", "dismiss the agent you started"),
                kv(
                    "/ai <question>",
                    "ask an agent in the room (/ai <name> <q> if many)",
                ),
                kv(
                    "/ai <name> !<task>",
                    "have a granted agent run a task in the sandbox",
                ),
                kv("/ai list", "list AI agents present + their provider/model"),
                kv(
                    "/ai models",
                    "show models the active agent's backend can serve",
                ),
            ],
        },
        HelpCluster {
            title: "PERMISSIONS (owner)",
            items: vec![
                kv(
                    "/grant <user|agent>",
                    "let a member OR an AI agent drive the shell",
                ),
                kv("/revoke <user|agent>", "take back sandbox drive permission"),
                kv("/sudo <user>", "delegate VM superuser (real sudo)"),
                kv("/unsudo <user>", "revoke VM superuser"),
                kv(
                    "/ai start <name> allow",
                    "shortcut: grant the agent drive at spawn",
                ),
            ],
        },
        HelpCluster {
            title: "FILES",
            items: vec![
                kv("/send <user> <path>", "send a file/dir directly to one member"),
                kv("/sendroom <path>", "offer a file/dir to the whole room"),
                kv("/accept  ·  /reject", "respond to an incoming file offer"),
            ],
        },
        HelpCluster {
            title: "APPEARANCE",
            items: vec![
                kv("/theme [name]", &theme_help),
                kv(
                    "/theme save [name]",
                    "keep the vestment you're wearing for reuse",
                ),
                kv(
                    "Ctrl+Alt+P  ·  /theme random",
                    "conjure a random vestment (palette + sigil)",
                ),
            ],
        },
        HelpCluster {
            title: "LAYOUT (resize panes)",
            items: vec![
                kv("F4", "fullscreen the terminal (cycle: terminal → chat → split)"),
                kv(
                    "click a pane  ·  F5",
                    "select a pane to resize (✎ marks it) — F5 cycles terminal → chat → roster",
                ),
                kv(
                    "↑ / ↓  (terminal/chat selected)",
                    "grow / shrink that pane's height share",
                ),
                kv("← / →  (roster selected)", "narrow / widen the roster column"),
                kv("Esc / Enter", "finish editing the selected pane"),
                kv("/layout reset", "restore the default split"),
                kv(
                    "/layout save <name>  ·  load <name>",
                    "remember an arrangement and re-apply it later",
                ),
                kv("/layout list  ·  rm <name>", "list or delete saved layouts"),
            ],
        },
        HelpCluster {
            title: "KEYS",
            items: vec![
                kv("Enter", "send chat message"),
                kv("F1  ·  /help", "toggle this help"),
                kv("F4 · F5 · click", "layout: fullscreen terminal · select pane to resize (see LAYOUT)"),
                kv("Ctrl-C  (while driving)", "interrupt the running command"),
                kv(
                    "Ctrl-X  (owner, not driving)",
                    "kill switch — revoke all drive + interrupt the shell (while driving it reaches the shell, e.g. nano)",
                ),
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
                kv("/pw", "show this room's password (local only)"),
                kv("/clear", "wipe your chat scrollback (local only)"),
                kv("Ctrl-C  ·  Ctrl-Q", "quit hack-house"),
            ],
        },
        HelpCluster {
            title: "ROSTER GLYPHS (badges stack)",
            items: vec![
                kv(
                    &format!("{} host", theme.sigil),
                    "opened the house (first in the room)",
                ),
                kv("⚡ sudoer", "VM superuser in the sandbox"),
                kv("◆ driver", "may drive the shell"),
                kv("• member", "present — no extra powers"),
            ],
        },
    ]
}

/// Number of help clusters — used to size/clamp the navigation state.
pub fn help_cluster_count(theme: &Theme) -> usize {
    help_clusters(theme).len()
}

/// Flatten the clusters into rendered lines given the current collapse state and
/// highlighted cluster. Shared by `draw_help` and `help_max_scroll` so the
/// scroll math always matches what's painted.
fn help_render_lines(app: &App, theme: &Theme) -> Vec<Line<'static>> {
    let clusters = help_clusters(theme);
    let acc = Style::default()
        .fg(theme.accent)
        .add_modifier(Modifier::BOLD);
    let sel = Style::default()
        .fg(theme.bg)
        .bg(theme.accent)
        .add_modifier(Modifier::BOLD);
    let key = Style::default().fg(theme.title);
    let dim = Style::default().fg(theme.system);
    let sig = theme.sigil.clone();
    let mut lines: Vec<Line<'static>> = Vec::new();
    for (i, c) in clusters.iter().enumerate() {
        let expanded = app.help_expanded.get(i).copied().unwrap_or(false);
        let marker = if expanded { "▾" } else { "▸" };
        let style = if i == app.help_selected { sel } else { acc };
        lines.push(Line::from(Span::styled(
            format!("{sig} {marker} {}  ({})", c.title, c.items.len()),
            style,
        )));
        if expanded {
            for (k, v) in &c.items {
                lines.push(Line::from(vec![
                    Span::styled(format!("    {k:<26}"), key),
                    Span::styled(v.clone(), dim),
                ]));
            }
            lines.push(Line::from(""));
        }
    }
    lines.push(Line::from(Span::styled(
        "  ↑/↓ select · ←/→ or Enter expand · PgUp/PgDn scroll · Esc closes",
        Style::default()
            .fg(theme.dim)
            .add_modifier(Modifier::ITALIC),
    )));
    lines
}

/// Arrow-navigable VirtualBox VM picker — a small dropdown anchored just above
/// the input box. The highlighted row is inverted (theme bg on accent); Enter or
/// Tab boots it, Esc dismisses. Key handling lives in the run loop.
fn draw_vbox_picker(f: &mut Frame, area: Rect, app: &App, theme: &Theme) {
    let Some(picker) = &app.vbox_picker else {
        return;
    };
    // Width: widest VM name (plus a little chrome), clamped to the terminal.
    let longest = picker
        .vms
        .iter()
        .map(|v| v.chars().count())
        .max()
        .unwrap_or(0);
    let w = (longest as u16 + 6).clamp(24, area.width.saturating_sub(2)).max(8);
    // Height: one row per VM + borders, capped so it never swallows the screen.
    let max_rows = area.height.saturating_sub(6).max(1);
    let body = (picker.vms.len() as u16).min(max_rows);
    let h = body + 2;
    // Anchor bottom-left, riding just above the 3-row input box.
    let x = area.x + 1;
    let y = area
        .y
        .saturating_add(area.height.saturating_sub(h + 3));
    let rect = Rect {
        x,
        y,
        width: w,
        height: h,
    };

    // Scroll the window so the selection stays visible in tall lists.
    let first = picker.selected.saturating_sub(body.saturating_sub(1) as usize);
    let items: Vec<ListItem> = picker
        .vms
        .iter()
        .enumerate()
        .skip(first)
        .take(body as usize)
        .map(|(i, vm)| {
            let style = if i == picker.selected {
                Style::default()
                    .fg(theme.bg)
                    .bg(theme.accent)
                    .add_modifier(Modifier::BOLD)
            } else {
                Style::default().fg(theme.title)
            };
            let marker = if i == picker.selected { "▸ " } else { "  " };
            ListItem::new(Line::from(Span::styled(format!("{marker}{vm}"), style)))
        })
        .collect();

    f.render_widget(Clear, rect);
    let list = List::new(items).style(Style::default().bg(theme.bg)).block(
        Block::bordered()
            .border_style(Style::default().fg(theme.accent))
            .title(Span::styled(
                format!(" {} pick a VM · ↑↓ ⏎ Esc ", theme.sigil),
                Style::default().fg(theme.title).add_modifier(Modifier::BOLD),
            )),
    );
    f.render_widget(list, rect);
}

fn draw_help(f: &mut Frame, area: Rect, app: &App, theme: &Theme) {
    let w = help_popup(area);
    let inner_w = w.width.saturating_sub(2);
    let inner_h = w.height.saturating_sub(2);
    let scroll = app.help_scroll;
    let para = Paragraph::new(help_render_lines(app, theme)).wrap(Wrap { trim: false });
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
    let base = if app.driving {
        theme.accent
    } else {
        theme.border
    };
    let (border_style, mark) = edit_decor(app, crate::app::Pane::Terminal, theme, base);
    let title = format!(" {mark}sandbox · {}{} ", sv.backend, drive);
    let pane = Paragraph::new(lines).block(
        Block::bordered()
            .border_style(border_style)
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
    // Author's current badge inline, so a message's authority is legible right
    // in the transcript — not only in the clergy panel.
    let badges = role_badges(app, &l.username, theme);
    Line::from(vec![
        Span::styled(format!("{} ", l.ts), Style::default().fg(theme.dim)),
        Span::styled(format!("{badges} "), Style::default().fg(theme.dim)),
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
    let mut lines: Vec<Line> = app.lines.iter().map(|l| fmt_line(l, app, theme)).collect();

    // Live preview bubbles for agents currently streaming a reply, rendered
    // below the committed history. Dim + italic so they read as in-progress;
    // replaced by the real message once the agent posts it.
    let mut streaming: Vec<(&String, &String)> = app.ai_stream.iter().collect();
    streaming.sort_unstable_by_key(|(name, _)| name.as_str());
    for (name, text) in streaming {
        lines.push(Line::from(vec![
            Span::styled(
                format!("{name} "),
                Style::default()
                    .fg(theme.other)
                    .add_modifier(Modifier::BOLD),
            ),
            Span::styled("⠿ ", Style::default().fg(theme.dim)),
            Span::styled(
                text.as_str(),
                Style::default()
                    .fg(theme.dim)
                    .add_modifier(Modifier::ITALIC),
            ),
        ]));
    }

    // Measure the TRUE wrapped height and scroll to the bottom. Selecting N
    // logical lines and letting the Paragraph top-anchor them clips any wrapped
    // line off the bottom — so the newest messages stay hidden until later ones
    // push them up into view (worse when the sandbox shrinks the chat pane).
    let total_rows = Paragraph::new(lines.clone())
        .wrap(Wrap { trim: false })
        .line_count(text_w);
    let max_scroll = total_rows.saturating_sub(inner_h);
    let scroll = max_scroll.saturating_sub(app.chat_scroll) as u16;

    let (border_style, mark) = edit_decor(app, crate::app::Pane::Chat, theme, theme.border);
    let title = if app.chat_scroll > 0 {
        format!(" {mark}chat ↑{} (End=live) ", app.chat_scroll)
    } else {
        format!(" {mark}chat ")
    };
    let chat = Paragraph::new(lines)
        .block(
            Block::bordered()
                .border_style(border_style)
                .title(Span::styled(title, Style::default().fg(theme.title))),
        )
        .wrap(Wrap { trim: false })
        .scroll((scroll, 0));
    f.render_widget(chat, area);
}

/// Glyph for a single role. The host sigil is theme-dependent (✝ for crypt, ⛧
/// for the default), the rest are fixed.
fn role_glyph(role: Role, theme: &Theme) -> &str {
    match role {
        Role::Host => theme.sigil.as_str(),
        Role::Sudoer => "⚡",
        Role::Driver => "◆",
        Role::Member => "•",
    }
}

/// The stacked badge string for `name` — e.g. `⛧⚡◆` for a host who summoned a
/// sandbox and can drive, or a lone `•` for a plain member. Single source of
/// truth shared by the roster and the chat author prefix so both always agree
/// with each other and with what the broker enforces.
fn role_badges(app: &App, name: &str, theme: &Theme) -> String {
    app.roles_of(name)
        .into_iter()
        .map(|r| role_glyph(r, theme))
        .collect()
}

fn draw_roster(f: &mut Frame, area: ratatui::layout::Rect, app: &App, theme: &Theme) {
    let items: Vec<ListItem> = app
        .users
        .iter()
        .map(|u| {
            let me = u.username == app.me;
            // Stacked badges: <sigil> host · ⚡ sudoer · ◆ driver · • member.
            let badges = role_badges(app, &u.username, theme);
            let color = if me { theme.roster_me } else { theme.other };
            ListItem::new(Line::from(Span::styled(
                format!(" {badges} {}", u.username),
                Style::default().fg(color),
            )))
        })
        .collect();
    let (border_style, mark) = edit_decor(app, crate::app::Pane::Roster, theme, theme.border);
    let roster = List::new(items).block(
        Block::bordered()
            .border_style(border_style)
            .title(Span::styled(
                format!(" {mark}clergy "),
                Style::default().fg(theme.title),
            )),
    );
    f.render_widget(roster, area);
}

/// Animated "⠋ <agent> is thinking…" title shown while AI agents generate a
/// reply. The name(s) come from `app.ai_typing`, i.e. each agent's own handle.
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
