//! Window layout ("cell plan"): a small binary space-partition (BSP) pane tree
//! describing how the chat, roster and sandbox-terminal panes divide the screen,
//! plus a fullscreen ("zoom") mode for the terminal or chat.
//!
//! The tree is the single source of truth shared by `ui::draw` (what's painted),
//! `ui::pane_at` (mouse hit-testing) and `app::sbx_grid` (the PTY grid we resize
//! the real shell to). Because the run loop re-syncs the PTY every tick, changing
//! a divider is all it takes to live-resize the terminal and broadcast the new
//! dims to the room.
//!
//! Two typed splits cover the three semantic panes:
//!   * `VSplit` — chat (top) over terminal (bottom), by **percentage** of height.
//!   * `HSplit` — left column beside the roster, the roster a **fixed cell** column
//!     on the right (`0` cells = hidden), matching the stable narrow-column look.
//! Nesting them gives every pane both-axis adjustability: the left column (chat +
//! terminal) shares the `HSplit` width, and chat/terminal share the `VSplit`
//! height. The roster is a full-height column, so only its width is adjustable.
//!
//! Named arrangements can be saved to `layouts/<slug>.toml` and re-applied later
//! with `/layout load <name>`, mirroring how `theme.rs` persists vestments.

use crate::app::Pane;
use anyhow::Context;
use ratatui::layout::{Constraint, Direction, Layout as RLayout, Rect};
use serde::{Deserialize, Serialize};

/// Where saved layout presets live (mirrors `theme::THEMES_DIR`).
pub const LAYOUTS_DIR: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/layouts");

/// Fullscreen state. `Normal` honours the tree; `Term` gives the whole body to
/// the sandbox terminal (chat + roster hidden); `Chat` hides the terminal so chat
/// + roster fill the body. The 3-line input bar always stays visible, so F4 (or
/// `/layout reset`) always brings you back.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum Zoom {
    Normal,
    Term,
    Chat,
}

impl Default for Zoom {
    fn default() -> Self {
        Zoom::Normal
    }
}

/// Which divider a resize key acts on: vertical (↑/↓ height) or horizontal
/// (←/→ width). `grow` is true for ↑/→ (enlarge the focused pane), false for ↓/←.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Dir {
    Up,
    Down,
    Left,
    Right,
}

impl Dir {
    fn grows(self) -> bool {
        matches!(self, Dir::Up | Dir::Right)
    }
}

/// Outcome of a resize attempt, so the caller can hint when an axis isn't
/// adjustable for the focused pane (e.g. height with no sandbox up).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Resize {
    /// A divider moved.
    Moved,
    /// No divider on that axis bounds the focused pane (caller shows a hint).
    NoAxisHere,
}

/// A node in the layout tree: a leaf pane, or one of the two typed splits.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum Node {
    Leaf(Pane),
    /// Chat (top) over terminal (bottom); `top_pct` is the top's share of height.
    VSplit {
        top_pct: u16,
        top: Box<Node>,
        bottom: Box<Node>,
    },
    /// Left column beside the roster; `right_cells` is the roster column width
    /// (`0` hides it). The left side takes the remaining width.
    HSplit {
        right_cells: u16,
        left: Box<Node>,
        right: Box<Node>,
    },
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct Layout {
    root: Node,
    pub zoom: Zoom,
    /// Height (in rows, borders included) of the bottom message/compose box. The
    /// input bar lives outside `root` (it's the frame's fixed bottom row), so it
    /// can't be a tree leaf — this scalar is its one adjustable axis. `↑/↓` while
    /// the Input pane is focused grows/shrinks it so a long, wrapped message is
    /// readable even with no sandbox up.
    input_height: u16,
}

impl Default for Layout {
    fn default() -> Self {
        Self {
            root: default_root(),
            zoom: Zoom::Normal,
            input_height: DEFAULT_INPUT,
        }
    }
}

/// The default arrangement: roster as a right-hand column, with chat over the
/// sandbox terminal in the left column (reproduces the historical look).
fn default_root() -> Node {
    Node::HSplit {
        right_cells: DEFAULT_ROSTER,
        left: Box::new(Node::VSplit {
            top_pct: DEFAULT_TOP_PCT,
            top: Box::new(Node::Leaf(Pane::Chat)),
            bottom: Box::new(Node::Leaf(Pane::Terminal)),
        }),
        right: Box::new(Node::Leaf(Pane::Roster)),
    }
}

/// Default chat share of the left-column height (terminal gets the rest).
const DEFAULT_TOP_PCT: u16 = 45;
/// Default roster column width in cells.
const DEFAULT_ROSTER: u16 = 22;
/// Default input-bar height (1 content line + 2 border rows) — the historical look.
const DEFAULT_INPUT: u16 = 3;

impl Layout {
    /// Lower/upper bounds for the chat (top) height share — neither chat nor
    /// terminal may collapse to nothing.
    pub const MIN_TOP: u16 = 10;
    pub const MAX_TOP: u16 = 80;
    /// Upper bound on roster width (keeps it from eating the whole chat column).
    pub const MAX_ROSTER: u16 = 60;
    /// Bounds on the input-bar height (rows, borders included). Min keeps the one
    /// content line + its borders; max stops it swallowing the whole body.
    pub const MIN_INPUT: u16 = 3;
    pub const MAX_INPUT: u16 = 16;
    /// Cells/percent/rows moved per arrow press.
    const PCT_STEP: u16 = 4;
    const CELL_STEP: u16 = 2;
    const ROW_STEP: u16 = 1;

    /// Seed the roster column width (called once from the active theme).
    pub fn set_roster_width(&mut self, cells: u16) {
        if let Node::HSplit { right_cells, .. } = &mut self.root {
            *right_cells = cells.min(Self::MAX_ROSTER);
        }
    }

    /// Re-clamp every divider into its valid range.
    pub fn clamp(&mut self) {
        clamp_node(&mut self.root);
        self.input_height = self.input_height.clamp(Self::MIN_INPUT, Self::MAX_INPUT);
    }

    /// Cycle the fullscreen state: Normal → Term → Chat → Normal. Bound to F4.
    pub fn cycle_zoom(&mut self) {
        self.zoom = match self.zoom {
            Zoom::Normal => Zoom::Term,
            Zoom::Term => Zoom::Chat,
            Zoom::Chat => Zoom::Normal,
        };
    }

    /// Carve `body` into the visible pane rectangles, honouring the current zoom
    /// and whether a sandbox terminal exists. The single source of truth for both
    /// painting and hit-testing.
    pub fn regions(&self, body: Rect, has_terminal: bool) -> Vec<(Pane, Rect)> {
        let mut out = Vec::new();
        match self.zoom {
            // Terminal fullscreen (only if one exists; otherwise fall through).
            Zoom::Term if has_terminal => out.push((Pane::Terminal, body)),
            // Chat fullscreen: paint the tree with the terminal hidden so chat +
            // roster fill the body.
            Zoom::Chat => fill(&self.root, body, false, &mut out),
            _ => fill(&self.root, body, has_terminal, &mut out),
        }
        out
    }

    /// The rectangle a given pane occupies in `body`, if visible.
    pub fn rect_of(&self, body: Rect, pane: Pane, has_terminal: bool) -> Option<Rect> {
        self.regions(body, has_terminal)
            .into_iter()
            .find(|(p, _)| *p == pane)
            .map(|(_, r)| r)
    }

    /// Panes currently visible, in a stable cycle order (chat → terminal → roster
    /// → input) for F5 focus cycling. The input bar is always present, so it's
    /// always a stop — that's the one resize you can do with no sandbox up.
    pub fn present_panes(&self, has_terminal: bool) -> Vec<Pane> {
        let mut v = vec![Pane::Chat];
        if has_terminal {
            v.push(Pane::Terminal);
        }
        if self.roster_width() > 0 {
            v.push(Pane::Roster);
        }
        v.push(Pane::Input);
        v
    }

    /// Current input-bar height (rows, borders included).
    pub fn input_height(&self) -> u16 {
        self.input_height
    }

    /// Resize the divider bounding `pane` on the axis of `dir`. `grow` (↑/→)
    /// enlarges the focused pane. Vertical resize always does *something* now:
    /// chat trades against the terminal when a sandbox is up, and otherwise chat
    /// and the roster drag the body↔input divider — so the chat box visibly
    /// grows/shrinks on ↑/↓ even with no sandbox (space is borrowed from / given
    /// back to the message bar below). Returns `NoAxisHere` only when truly no
    /// divider applies.
    pub fn resize_focused(&mut self, pane: Pane, dir: Dir, has_terminal: bool) -> Resize {
        // The input bar is outside the tree: its only axis is height, adjusted
        // directly here (↑ grows, ↓ shrinks). ←/→ have nothing to act on.
        if pane == Pane::Input {
            return match dir {
                Dir::Up | Dir::Down => {
                    self.input_height = step_rows(self.input_height, dir.grows());
                    Resize::Moved
                }
                Dir::Left | Dir::Right => Resize::NoAxisHere,
            };
        }
        match dir {
            Dir::Up | Dir::Down => {
                // Chat and terminal share the VSplit when a sandbox is up — the
                // chat box trades its height against the terminal directly below.
                if has_terminal && (pane == Pane::Chat || pane == Pane::Terminal) {
                    if let Some((top_pct, _, _)) = find_vsplit(&mut self.root) {
                        // chat is the top; grow chat → bigger top share.
                        let grow_top = (pane == Pane::Chat) == dir.grows();
                        *top_pct = step_pct(*top_pct, grow_top);
                        return Resize::Moved;
                    }
                }
                // Otherwise the pane's downward neighbour is the message bar. Chat
                // (no sandbox) and the roster drag the body↔input divider: growing
                // the pane (↑) grows the body — i.e. the chat box — and shrinks the
                // input bar; ↓ does the reverse. This is what makes the chat box
                // fluctuate on ↑/↓ for both chat and clergy.
                if pane == Pane::Chat || pane == Pane::Roster {
                    self.input_height = step_rows(self.input_height, !dir.grows());
                    return Resize::Moved;
                }
                Resize::NoAxisHere
            }
            Dir::Left | Dir::Right => {
                if let Some(right_cells) = find_hsplit(&mut self.root) {
                    // roster is the right column; growing the roster widens it,
                    // growing a left-column pane narrows it.
                    let grow_right = (pane == Pane::Roster) == dir.grows();
                    *right_cells = step_cells(*right_cells, grow_right);
                    Resize::Moved
                } else {
                    Resize::NoAxisHere
                }
            }
        }
    }

    /// Current roster column width in cells (0 when hidden).
    pub fn roster_width(&self) -> u16 {
        match &self.root {
            Node::HSplit { right_cells, .. } => *right_cells,
            _ => 0,
        }
    }

    /// A one-line description of the current arrangement (for `/layout`).
    pub fn describe(&self) -> String {
        let zoom = match self.zoom {
            Zoom::Normal => "split",
            Zoom::Term => "terminal-fullscreen",
            Zoom::Chat => "chat-fullscreen",
        };
        let top = find_top_pct(&self.root).unwrap_or(DEFAULT_TOP_PCT);
        let roster = match self.roster_width() {
            0 => "off".to_string(),
            n => format!("{n} cells"),
        };
        format!(
            "chat {}% · terminal {}% · roster {} · input {} rows · {}",
            top,
            100 - top,
            roster,
            self.input_height,
            zoom
        )
    }

    /// Persist this arrangement to `layouts/<slug>.toml`. Returns the slug written.
    pub fn save(&self, name: &str) -> anyhow::Result<String> {
        let slug = slugify(name);
        anyhow::ensure!(!slug.is_empty(), "give the layout a name (letters/digits)");
        std::fs::create_dir_all(LAYOUTS_DIR).with_context(|| format!("creating {LAYOUTS_DIR}"))?;
        let path = format!("{LAYOUTS_DIR}/{slug}.toml");
        let body = toml::to_string_pretty(self).with_context(|| format!("serialize layout '{slug}'"))?;
        std::fs::write(&path, body).with_context(|| format!("write {path}"))?;
        Ok(slug)
    }

    /// Load a saved arrangement by name (`layouts/<slug>.toml`), re-clamped.
    pub fn by_name(name: &str) -> anyhow::Result<Self> {
        let slug = slugify(name);
        let path = format!("{LAYOUTS_DIR}/{slug}.toml");
        let s = std::fs::read_to_string(&path).with_context(|| format!("layout '{slug}' ({path})"))?;
        let mut l: Layout = toml::from_str(&s)?;
        l.clamp();
        Ok(l)
    }

    /// Names of saved presets, sorted, for `/layout list`.
    pub fn available() -> Vec<String> {
        let mut names: Vec<String> = std::fs::read_dir(LAYOUTS_DIR)
            .into_iter()
            .flatten()
            .flatten()
            .filter_map(|e| {
                e.file_name()
                    .to_str()
                    .and_then(|f| f.strip_suffix(".toml"))
                    .map(str::to_string)
            })
            .collect();
        names.sort();
        names
    }

    /// Delete a saved preset. Errors if it doesn't exist.
    pub fn remove(name: &str) -> anyhow::Result<()> {
        let slug = slugify(name);
        let path = format!("{LAYOUTS_DIR}/{slug}.toml");
        std::fs::remove_file(&path).with_context(|| format!("no saved layout '{slug}'"))?;
        Ok(())
    }
}

impl Default for Node {
    fn default() -> Self {
        default_root()
    }
}

/// Recursively paint `node` into `rect`, pruning the terminal when absent and the
/// roster when its column is 0-width (the divider vanishes, the sibling fills).
fn fill(node: &Node, rect: Rect, has_terminal: bool, out: &mut Vec<(Pane, Rect)>) {
    match node {
        Node::Leaf(p) => out.push((*p, rect)),
        Node::VSplit { top_pct, top, bottom } => {
            if !has_terminal {
                fill(top, rect, has_terminal, out); // terminal pruned → chat fills
                return;
            }
            let parts = RLayout::default()
                .direction(Direction::Vertical)
                .constraints([Constraint::Percentage(*top_pct), Constraint::Percentage(100 - *top_pct)])
                .split(rect);
            fill(top, parts[0], has_terminal, out);
            fill(bottom, parts[1], has_terminal, out);
        }
        Node::HSplit { right_cells, left, right } => {
            if *right_cells == 0 {
                fill(left, rect, has_terminal, out); // roster hidden → left fills
                return;
            }
            let parts = RLayout::default()
                .direction(Direction::Horizontal)
                .constraints([Constraint::Min(1), Constraint::Length(*right_cells)])
                .split(rect);
            fill(left, parts[0], has_terminal, out);
            fill(right, parts[1], has_terminal, out);
        }
    }
}

/// Nearest VSplit's `top_pct` (mutable) on the path to a leaf. With our default
/// tree there is exactly one.
fn find_vsplit(node: &mut Node) -> Option<(&mut u16, &mut Box<Node>, &mut Box<Node>)> {
    match node {
        Node::Leaf(_) => None,
        Node::VSplit { top_pct, top, bottom } => Some((top_pct, top, bottom)),
        Node::HSplit { left, right, .. } => find_vsplit(left).or_else(|| find_vsplit(right)),
    }
}

/// Nearest HSplit's `right_cells` (mutable) on the path to a leaf.
fn find_hsplit(node: &mut Node) -> Option<&mut u16> {
    match node {
        Node::Leaf(_) => None,
        Node::HSplit { right_cells, .. } => Some(right_cells),
        Node::VSplit { top, bottom, .. } => find_hsplit(top).or_else(|| find_hsplit(bottom)),
    }
}

fn find_top_pct(node: &Node) -> Option<u16> {
    match node {
        Node::Leaf(_) => None,
        Node::VSplit { top_pct, .. } => Some(*top_pct),
        Node::HSplit { left, right, .. } => find_top_pct(left).or_else(|| find_top_pct(right)),
    }
}

fn step_pct(pct: u16, grow: bool) -> u16 {
    let next = if grow {
        pct + Layout::PCT_STEP
    } else {
        pct.saturating_sub(Layout::PCT_STEP)
    };
    next.clamp(Layout::MIN_TOP, Layout::MAX_TOP)
}

fn step_cells(cells: u16, grow: bool) -> u16 {
    let next = if grow {
        cells + Layout::CELL_STEP
    } else {
        cells.saturating_sub(Layout::CELL_STEP)
    };
    next.min(Layout::MAX_ROSTER)
}

fn step_rows(rows: u16, grow: bool) -> u16 {
    let next = if grow {
        rows + Layout::ROW_STEP
    } else {
        rows.saturating_sub(Layout::ROW_STEP)
    };
    next.clamp(Layout::MIN_INPUT, Layout::MAX_INPUT)
}

fn clamp_node(node: &mut Node) {
    match node {
        Node::Leaf(_) => {}
        Node::VSplit { top_pct, top, bottom } => {
            *top_pct = (*top_pct).clamp(Layout::MIN_TOP, Layout::MAX_TOP);
            clamp_node(top);
            clamp_node(bottom);
        }
        Node::HSplit { right_cells, left, right } => {
            *right_cells = (*right_cells).min(Layout::MAX_ROSTER);
            clamp_node(left);
            clamp_node(right);
        }
    }
}

/// Reduce a free-form name to a safe lowercase `<slug>` (ascii letters/digits,
/// any other run folded to a single '-'), matching theme.rs's convention.
fn slugify(name: &str) -> String {
    let mut out = String::new();
    let mut dash = false;
    for c in name.trim().chars() {
        if c.is_ascii_alphanumeric() {
            if dash && !out.is_empty() {
                out.push('-');
            }
            out.extend(c.to_lowercase());
            dash = false;
        } else {
            dash = true;
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn body() -> Rect {
        Rect::new(0, 0, 100, 40)
    }

    #[test]
    fn regions_cover_body_with_sandbox() {
        let l = Layout::default();
        let regs = l.regions(body(), true);
        // chat, terminal, roster all present.
        assert_eq!(regs.len(), 3);
        assert!(regs.iter().any(|(p, _)| *p == Pane::Chat));
        assert!(regs.iter().any(|(p, _)| *p == Pane::Terminal));
        assert!(regs.iter().any(|(p, _)| *p == Pane::Roster));
        // total painted area equals the body (no gaps/overlap in a binary split).
        let area: u32 = regs.iter().map(|(_, r)| r.area() as u32).sum();
        assert_eq!(area, body().area() as u32);
    }

    #[test]
    fn no_sandbox_prunes_terminal() {
        let l = Layout::default();
        let regs = l.regions(body(), false);
        assert!(!regs.iter().any(|(p, _)| *p == Pane::Terminal));
        assert!(regs.iter().any(|(p, _)| *p == Pane::Chat));
        // chat now owns the full left-column height.
        let chat = l.rect_of(body(), Pane::Chat, false).unwrap();
        assert_eq!(chat.height, body().height);
    }

    #[test]
    fn roster_hidden_at_zero_cells() {
        let mut l = Layout::default();
        l.set_roster_width(0);
        let regs = l.regions(body(), true);
        assert!(!regs.iter().any(|(p, _)| *p == Pane::Roster));
        // chat column now spans full width.
        let chat = l.rect_of(body(), Pane::Chat, true).unwrap();
        assert_eq!(chat.width, body().width);
    }

    #[test]
    fn vertical_resize_grows_focused_pane() {
        let mut l = Layout::default();
        let chat0 = l.rect_of(body(), Pane::Chat, true).unwrap().height;
        assert_eq!(l.resize_focused(Pane::Chat, Dir::Up, true), Resize::Moved);
        let chat1 = l.rect_of(body(), Pane::Chat, true).unwrap().height;
        assert!(chat1 > chat0, "chat grew taller on ↑");
        // terminal up grows the terminal (chat shrinks).
        assert_eq!(l.resize_focused(Pane::Terminal, Dir::Up, true), Resize::Moved);
        let chat2 = l.rect_of(body(), Pane::Chat, true).unwrap().height;
        assert!(chat2 < chat1, "chat shrank when terminal grew");
    }

    #[test]
    fn horizontal_resize_changes_roster_width() {
        let mut l = Layout::default();
        let r0 = l.rect_of(body(), Pane::Roster, true).unwrap().width;
        assert_eq!(l.resize_focused(Pane::Roster, Dir::Right, true), Resize::Moved);
        let r1 = l.rect_of(body(), Pane::Roster, true).unwrap().width;
        assert!(r1 > r0, "roster widened on →");
        // focusing chat and pressing → narrows the roster (grows the left column).
        assert_eq!(l.resize_focused(Pane::Chat, Dir::Right, true), Resize::Moved);
        let r2 = l.rect_of(body(), Pane::Roster, true).unwrap().width;
        assert!(r2 < r1, "roster narrowed when chat grew");
    }

    #[test]
    fn vertical_resize_drags_input_when_no_terminal() {
        let mut l = Layout::default();
        let h0 = l.input_height(); // default = MIN_INPUT
        // Chat ↓ with no sandbox shrinks the chat body → grows the input bar.
        assert_eq!(l.resize_focused(Pane::Chat, Dir::Down, false), Resize::Moved);
        assert!(l.input_height() > h0, "chat ↓ grew the input bar");
        let h1 = l.input_height();
        // Chat ↑ grows the chat body → shrinks the input bar.
        assert_eq!(l.resize_focused(Pane::Chat, Dir::Up, false), Resize::Moved);
        assert!(l.input_height() < h1, "chat ↑ shrank the input bar");
        // The roster trades the same way (even with a sandbox up): clergy borrows
        // height from the chat body via the same divider.
        l.resize_focused(Pane::Roster, Dir::Down, true);
        let h2 = l.input_height();
        assert_eq!(l.resize_focused(Pane::Roster, Dir::Up, true), Resize::Moved);
        assert!(l.input_height() < h2, "roster ↑ shrank the input bar (body grew)");
    }

    #[test]
    fn resize_honours_clamps() {
        let mut l = Layout::default();
        for _ in 0..50 {
            l.resize_focused(Pane::Chat, Dir::Up, true);
        }
        assert_eq!(find_top_pct(&l.root), Some(Layout::MAX_TOP));
        for _ in 0..50 {
            l.resize_focused(Pane::Chat, Dir::Down, true);
        }
        assert_eq!(find_top_pct(&l.root), Some(Layout::MIN_TOP));
    }

    #[test]
    fn present_panes_track_visibility() {
        let mut l = Layout::default();
        assert_eq!(
            l.present_panes(true),
            vec![Pane::Chat, Pane::Terminal, Pane::Roster, Pane::Input]
        );
        assert_eq!(
            l.present_panes(false),
            vec![Pane::Chat, Pane::Roster, Pane::Input]
        );
        l.set_roster_width(0);
        assert_eq!(l.present_panes(false), vec![Pane::Chat, Pane::Input]);
    }

    #[test]
    fn input_resize_grows_and_clamps_without_sandbox() {
        let mut l = Layout::default();
        let h0 = l.input_height();
        // ↑ grows the input bar even with no sandbox (has_terminal = false).
        assert_eq!(l.resize_focused(Pane::Input, Dir::Up, false), Resize::Moved);
        assert!(l.input_height() > h0, "input grew taller on ↑");
        // ←/→ have no axis for the input bar.
        assert_eq!(
            l.resize_focused(Pane::Input, Dir::Left, false),
            Resize::NoAxisHere
        );
        // Clamp at both ends.
        for _ in 0..50 {
            l.resize_focused(Pane::Input, Dir::Up, false);
        }
        assert_eq!(l.input_height(), Layout::MAX_INPUT);
        for _ in 0..50 {
            l.resize_focused(Pane::Input, Dir::Down, false);
        }
        assert_eq!(l.input_height(), Layout::MIN_INPUT);
    }

    #[test]
    fn serde_round_trips_the_tree() {
        let mut l = Layout::default();
        l.resize_focused(Pane::Chat, Dir::Up, true);
        l.set_roster_width(30);
        let s = toml::to_string_pretty(&l).unwrap();
        let back: Layout = toml::from_str(&s).unwrap();
        assert_eq!(back, l);
    }

    #[test]
    fn zoom_term_fills_body() {
        let mut l = Layout::default();
        l.zoom = Zoom::Term;
        let regs = l.regions(body(), true);
        assert_eq!(regs.len(), 1);
        assert_eq!(regs[0].0, Pane::Terminal);
        assert_eq!(regs[0].1, body());
    }

    #[test]
    fn zoom_chat_hides_terminal() {
        let mut l = Layout::default();
        l.zoom = Zoom::Chat;
        let regs = l.regions(body(), true);
        assert!(!regs.iter().any(|(p, _)| *p == Pane::Terminal));
        assert!(regs.iter().any(|(p, _)| *p == Pane::Chat));
    }
}
