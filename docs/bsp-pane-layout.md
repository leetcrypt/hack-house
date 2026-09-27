# Spec: BSP pane layout (resize every pane in both dimensions)

Status: **implemented** · Branch: `feat/bsp-pane-layout`

> Implementation note: the shipped tree uses two **typed** splits
> (`VSplit`/`HSplit`) rather than a single uniform `Split{axis, ratio}`. This
> keeps the roster a fixed-cell column (not a percentage) and lets the type
> system encode "chat-over-terminal" vs "column-beside-roster" directly. The
> sections below describe the original uniform model; the typed shape is called
> out inline where they differ.

## Motivation

Today the window layout is two scalars over a *fixed* 3-pane arrangement
(`layout::Layout { pty_pct, roster_width, zoom }`):

- `pty_pct` — sandbox terminal's share of body **height** (chat takes the rest).
- `roster_width` — roster column **width** in cells.

Consequences the user hit:

1. **No sandbox → almost nothing adjustable.** `body_areas` only creates the
   chat/terminal vertical split `if app.sandbox.is_some()`. Without a sandbox the
   chat owns the whole body, so the only live dimension is the roster *width*.
   The `↑/↓` keys still call `grow_pty`/`shrink_pty`, but with no terminal pane
   to trade height against, nothing moves.
2. **Each pane is locked to one axis.** The terminal only changes height (always
   full width); the roster only changes width (always full height). You can't,
   e.g., make the terminal narrower or give the roster its own height.

Goal: let the user resize **each pane in both width and height**, using the
standard BSP (binary space-partition) pane-tree model (tmux/i3-style).
Researched in chat: ratatui core has no interactive-resize/BSP primitive (only
the Cassowary/`kasuari` solver + the "store-percent, mutate, re-solve" pattern);
the `ratkit` crate's `ResizableGrid` proves the BSP-tree approach. We roll a
small in-repo tree (option 2 from the research) so the PTY-resync integration
stays first-class and we take no new dependency.

## Model

Replace the two scalars with a binary tree. Leaves are the three *semantic*
panes; internal nodes are splits with an axis + ratio.

Original uniform sketch:

```rust
pub enum PaneKind { Chat, Roster, Terminal }
pub enum Axis { Horizontal, Vertical } // H: side-by-side (|), V: stacked (—)
pub enum Node {
    Leaf(PaneKind),
    Split { axis: Axis, ratio: u16, a: Box<Node>, b: Box<Node> },
}
```

**As shipped** (`hh/src/layout.rs`) — typed splits, reusing `app::Pane` as the
leaf type so it can be (de)serialized into presets:

```rust
pub enum Node {
    Leaf(Pane),                                           // Pane = Chat|Roster|Terminal
    VSplit { top_pct: u16, top: Box<Node>, bottom: Box<Node> },    // chat over terminal, by %
    HSplit { right_cells: u16, left: Box<Node>, right: Box<Node> },// column | roster, by cells
}

pub struct Layout { root: Node, pub zoom: Zoom }          // Zoom: Normal | Term | Chat
```

The roster width lives as `HSplit.right_cells` (0 = hidden); there's no separate
`roster_seed` — the active theme seeds it once via `Layout::set_roster_width`.

### Default arrangement (reproduces today's look)

With a sandbox present (typed shape as shipped):

```
HSplit(right_cells=22,                   // left column | roster (right, full height)
   left  = VSplit(top_pct=45,            // chat (top) / terminal (bottom)
            top    = Leaf(Chat),
            bottom = Leaf(Terminal)),
   right = Leaf(Roster))
```

The tree is **static**; the terminal and roster are *pruned at paint time*
rather than rebuilt. `fill()` skips the `VSplit` when no sandbox is up (chat
fills the left column) and skips the `HSplit` divider when `right_cells == 0`
(roster hidden). So `/sbx launch`/`stop` and hiding the roster need no rebuild —
the same tree just paints fewer leaves. `top_pct` is clamped to `[10, 80]`;
`right_cells` to `[0, 60]`.

## Geometry (single source of truth)

`layout.rs` owns recursive area computation; `ui.rs::body_areas` becomes a thin
wrapper so `draw` and `pane_at` keep sharing one geometry.

As shipped, `regions` just returns the visible leaves (no `Divider` struct yet —
that's deferred to phase 5), and takes `has_terminal` so it can prune:

```rust
pub fn regions(&self, body: Rect, has_terminal: bool) -> Vec<(Pane, Rect)>;
pub fn rect_of(&self, body: Rect, pane: Pane, has_terminal: bool) -> Option<Rect>;
```

`ui.rs::body_areas` is now a thin wrapper that fans `regions` out into its
`{chat, roster, sbx}` rects, so `draw` and `pane_at` share one geometry.
`Zoom::Term`/`Zoom::Chat` short-circuit `regions` (one pane fills the body; the
other leaves are omitted).

## Resize semantics (keyboard)

A pane is selected via `F5` (cycle over `present_panes`) or mouse click
(`pane_at`). Then arrows resize the split that bounds it on the matching axis:

- `↑` / `↓` → the `VSplit` (chat/terminal height). Only meaningful with a
  sandbox up and a non-roster pane selected — otherwise `NoAxisHere`.
- `←` / `→` → the `HSplit` (roster `right_cells`). Growing the roster widens it;
  growing a left-column pane narrows it.

This makes *every* pane adjustable on both axes wherever such a split exists. If
none does (e.g. only Chat+Roster, press `↑`), emit a one-line hint instead of
silently doing nothing — fixes consequence (1).

```rust
pub fn resize_focused(&mut self, pane: Pane, dir: Dir, has_terminal: bool) -> Resize;
// Resize::Moved | ::NoAxisHere  (caller turns the latter into a sys hint)
```

The step size is fixed in `layout.rs` (`PCT_STEP = 4`, `CELL_STEP = 2`), not a
caller-supplied `step`.

## PTY integration (the critical wrinkle)

Today `sbx_dims(term_w, term_h, pty_pct)` derives the PTY grid from the height
scalar and assumes **full width**. In the tree model the terminal can be any
rect, so the grid must come from the Terminal leaf's actual `Rect`:

As shipped it returns a concrete grid (falling back to the full body if no
Terminal leaf is laid out), rather than an `Option`:

```rust
fn sbx_grid(term_w: u16, term_h: u16, layout: &Layout) -> (u16, u16) {
    let body = Rect { x: 0, y: 1, width: term_w, height: term_h.saturating_sub(4) };
    let r = layout.rect_of(body, Pane::Terminal, true).unwrap_or(body);
    (r.height.saturating_sub(2).max(1), r.width.saturating_sub(2).max(1))
}
```

Every existing `sbx_dims(...)` call site (run loop, the two `/sbx`
launch/restore command handlers) switches to `sbx_grid`, and `announced_dims =
None` still forces a re-sync after any resize — so the shared PTY rebroadcasts
new dims to the room exactly as before. **This now also lets terminal *width*
changes propagate** (any divider move sets `announced_dims = None`), which the
old height-only code couldn't express.

## Persistence (presets)

`/layout save|load|list|rm|reset` keep working; the on-disk TOML now serializes
the tree instead of two scalars (`layouts/<slug>.toml`). Old single-scalar
preset files are not migrated (presets are throwaway; `reset` rebuilds default).
`serde` derives on `Node`/`Zoom`/`Pane` handle (de)serialization (`Pane` gained
`Serialize, Deserialize` in `app.rs` for this).

## Out of scope (phase 5, optional, follow-up)

- **Mouse-drag dividers.** Not scaffolded yet — `regions` returns only leaves; a
  follow-up adds a `Divider`/`hit_threshold` return and drag handling (hover
  highlight, `dragging_split`). Keyboard resize is the phase-1..4 deliverable.
- Arbitrary user-created splits / moving a pane to a new position. The tree
  supports it, but the UI only exposes the three named panes for now.

## Work plan

1. ✅ **layout.rs** — typed tree (`VSplit`/`HSplit`), default builder,
   `regions`/`rect_of`, `resize_focused`, clamps, zoom, `describe`, serde,
   presets. 11 unit tests (geometry sums to body, resize honors clamps, no-axis
   case, serde round-trip, terminal/roster prune).
2. ✅ **ui.rs** — `body_areas` is now a thin wrapper over `regions`; `pane_at`
   shares it; `edit_decor` marker on the focused pane.
3. ✅ **app.rs** — `sbx_grid` replaces `sbx_dims`; key handler resizes both axes
   via `resize_focused` (+ hint on `NoAxisHere`); `cycle_focus` over
   `present_panes`; F4 zoom + `announced_dims` re-sync unchanged.
4. ✅ **docs** — in-app `/help` LAYOUT cluster + README "Window layout" updated
   to describe both-axis resize and the no-axis hint; this spec reconciled.
5. *(optional, not done)* mouse-drag dividers.

## Acceptance

- `cargo test` green; `cargo build` warning-free.
- No sandbox: selecting Chat and pressing `←/→` resizes vs. roster; `↑/↓` prints
  the "no pane to trade height with — launch a sandbox" hint.
- Sandbox up: Chat/Terminal resize in **both** axes; roster width still works;
  terminal width changes now re-sync the PTY grid to the room.
- `/layout save` + `/layout load` round-trips the new tree.
- F4 fullscreen + `Ctrl+R` reconnect re-announce unchanged.
