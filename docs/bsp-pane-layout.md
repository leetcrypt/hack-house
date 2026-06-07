# Spec: BSP pane layout (resize every pane in both dimensions)

Status: **draft / in progress** · Branch: `feat/bsp-pane-layout`

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

```rust
pub enum PaneKind { Chat, Roster, Terminal }

pub enum Axis { Horizontal, Vertical } // H: side-by-side (|), V: stacked (—)

pub enum Node {
    Leaf(PaneKind),
    Split { axis: Axis, ratio: u16, a: Box<Node>, b: Box<Node> },
    // `ratio` = percent of the split given to `a` (10..=90).
}

pub struct Layout {
    root: Node,
    zoom: Zoom,        // unchanged: Normal | Term | Chat (F4 fullscreen)
    roster_seed: u16,  // theme-provided default roster width, for rebuilds
}
```

### Default arrangement (reproduces today's look)

With a sandbox present:

```
Split(H, ratio=roster_split,            // left column | roster (right, full height)
   a = Split(V, ratio=chat_pct,         // chat (top) / terminal (bottom)
        a = Leaf(Chat),
        b = Leaf(Terminal)),
   b = Leaf(Roster))
```

Without a sandbox the Terminal leaf is absent, so the tree collapses to:

```
Split(H, ratio=roster_split, a = Leaf(Chat), b = Leaf(Roster))
```

The tree is rebuilt (preserving ratios where possible) when the Terminal pane
appears (`/sbx launch`) or disappears (`/sbx stop`), and when the roster is
hidden/shown. Ratios are stored as percentages and clamped to `[10, 90]`.

## Geometry (single source of truth)

`layout.rs` owns recursive area computation; `ui.rs::body_areas` becomes a thin
wrapper so `draw` and `pane_at` keep sharing one geometry.

```rust
pub struct Regions {                 // computed for a given body Rect
    pub panes: Vec<(PaneKind, Rect)>,// one per visible leaf
    pub dividers: Vec<Divider>,      // for mouse hit-testing (phase 5)
}
pub fn regions(&self, body: Rect) -> Regions;
pub fn rect_of(&self, body: Rect, kind: PaneKind) -> Option<Rect>;
```

`Zoom::Term`/`Zoom::Chat` short-circuit `regions` exactly like today (one pane
fills the body; the other leaves are omitted).

## Resize semantics (keyboard)

A pane is selected via `F5` (cycle) or mouse click (`pane_at`). Then arrows
resize the **nearest ancestor split on the matching axis**:

- `↑` / `↓` → nearest ancestor `Split{axis: Vertical}` of the focused leaf;
  grow/shrink that leaf's side of the split.
- `←` / `→` → nearest ancestor `Split{axis: Horizontal}`.

This makes *every* pane adjustable on both axes wherever such an ancestor
exists. If no ancestor on that axis exists (e.g. only Chat+Roster, press `↑`),
emit a one-line hint instead of silently doing nothing — fixes consequence (1).

```rust
pub fn resize_focused(&mut self, pane: PaneKind, dir: Dir, step: u16) -> ResizeOutcome;
// ResizeOutcome::Moved | ::NoAxisHere  (caller turns the latter into a sys hint)
```

## PTY integration (the critical wrinkle)

Today `sbx_dims(term_w, term_h, pty_pct)` derives the PTY grid from the height
scalar and assumes **full width**. In the tree model the terminal can be any
rect, so the grid must come from the Terminal leaf's actual `Rect`:

```rust
fn sbx_grid(term_w: u16, term_h: u16, layout: &Layout) -> Option<(u16, u16)> {
    let body = body_rect(term_w, term_h);          // minus top bar + input
    layout.rect_of(body, PaneKind::Terminal)
        .map(|r| (r.height.saturating_sub(2).max(1), r.width.saturating_sub(2).max(1)))
}
```

Every existing `sbx_dims(...)` call site (run loop ~926, command handlers
~1865/1971) switches to `sbx_grid`, and `announced_dims = None` still forces a
re-sync after any resize — so the shared PTY rebroadcasts new dims to the room
exactly as before. **This now also lets terminal *width* changes propagate**,
which the old code couldn't express.

## Persistence (presets)

`/layout save|load|list|rm|reset` keep working; the on-disk TOML now serializes
the tree instead of two scalars (`layouts/<slug>.toml`). Old single-scalar
preset files are not migrated (presets are throwaway; `reset` rebuilds default).
`serde` derives on `Node`/`Axis`/`PaneKind` handle (de)serialization.

## Out of scope (phase 5, optional, follow-up)

- **Mouse-drag dividers.** `Regions.dividers` + `hit_threshold` are scaffolded
  now; drag handling (hover highlight, `dragging_split`) lands later. Keyboard
  resize is the phase-1..4 deliverable.
- Arbitrary user-created splits / moving a pane to a new position. The tree
  supports it, but the UI only exposes the three named panes for now.

## Work plan

1. **layout.rs** — tree types, default/rebuild builders, `regions`/`rect_of`,
   `resize_focused`, clamps, zoom, `describe`, serde, presets. Unit tests
   (geometry sums to body, resize honors clamps, no-axis case, serde round-trip,
   sandbox add/remove rebuild).
2. **ui.rs** — `body_areas`→tree `regions`; `pane_at` via `regions`; draw each
   leaf; `edit_decor` marker on the focused pane.
3. **app.rs** — `sbx_grid` replaces `sbx_dims`; key handler resizes both axes via
   `resize_focused` (+ hint on `NoAxisHere`); `cycle_focus` over *present* panes;
   F4 zoom + `announced_dims` re-sync unchanged.
4. **docs** — in-app `/help` LAYOUT cluster + README "Window layout" updated to
   describe both-axis resize and the no-axis hint.
5. *(optional)* mouse-drag dividers.

## Acceptance

- `cargo test` green; `cargo build` warning-free.
- No sandbox: selecting Chat and pressing `←/→` resizes vs. roster; `↑/↓` prints
  the "no pane to trade height with — launch a sandbox" hint.
- Sandbox up: Chat/Terminal resize in **both** axes; roster width still works;
  terminal width changes now re-sync the PTY grid to the room.
- `/layout save` + `/layout load` round-trips the new tree.
- F4 fullscreen + `Ctrl+R` reconnect re-announce unchanged.
