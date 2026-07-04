//! Window layout ("cell plan"): how the chat, roster and sandbox-terminal panes
//! divide the screen, plus a fullscreen ("zoom") mode for the terminal or chat.
//!
//! The split is a single source of truth shared by `ui::draw` (what's painted)
//! and `app::sbx_dims` (the PTY grid we resize the real shell to). Because the
//! run loop re-syncs the PTY every tick, changing these values is all it takes
//! to live-resize the terminal and broadcast the new dims to the room.
//!
//! Named arrangements can be saved to `layouts/<slug>.toml` and re-applied later
//! with `/layout load <name>`, mirroring how `theme.rs` persists vestments.

use anyhow::Context;
use serde::{Deserialize, Serialize};

/// Where saved layout presets live (mirrors `theme::THEMES_DIR`).
pub const LAYOUTS_DIR: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/layouts");

/// Fullscreen state. `Normal` honours the `pty_pct` split; `Term` gives the
/// whole body to the sandbox terminal (chat + roster hidden); `Chat` hides the
/// terminal so chat + roster fill the body. The 3-line input bar always stays
/// visible, so you can always type `/layout normal` or press F4 to get back.
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

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct Layout {
    /// Sandbox terminal's share of the body height, as a percentage (clamped
    /// 20–90 so neither chat nor terminal can collapse to nothing).
    pub pty_pct: u16,
    /// Roster column width in cells. `0` hides the roster entirely.
    pub roster_width: u16,
    /// Fullscreen state (not all presets care; defaults to `Normal`).
    pub zoom: Zoom,
}

impl Default for Layout {
    fn default() -> Self {
        Self {
            pty_pct: 55,
            roster_width: 22,
            zoom: Zoom::Normal,
        }
    }
}

impl Layout {
    /// Lower/upper bounds for the terminal's height share.
    pub const MIN_PCT: u16 = 20;
    pub const MAX_PCT: u16 = 90;
    /// Upper bound on roster width (keeps it from eating the whole chat column).
    pub const MAX_ROSTER: u16 = 60;

    /// Re-clamp every field into its valid range. Call after any mutation that
    /// came from user input so out-of-range values can never reach the layout.
    pub fn clamp(&mut self) {
        self.pty_pct = self.pty_pct.clamp(Self::MIN_PCT, Self::MAX_PCT);
        self.roster_width = self.roster_width.min(Self::MAX_ROSTER);
    }

    /// The terminal's effective height share once zoom is taken into account:
    /// `Term` fills the body (100%); `Normal`/`Chat` use the stored split (the
    /// terminal is simply not painted under `Chat`, so its size stays steady).
    pub fn effective_pty_pct(&self) -> u16 {
        match self.zoom {
            Zoom::Term => 100,
            _ => self.pty_pct,
        }
    }

    /// Cycle the fullscreen state: Normal → Term → Chat → Normal. Bound to F4.
    pub fn cycle_zoom(&mut self) {
        self.zoom = match self.zoom {
            Zoom::Normal => Zoom::Term,
            Zoom::Term => Zoom::Chat,
            Zoom::Chat => Zoom::Normal,
        };
    }

    /// Grow the terminal pane by `step` percent (drops out of any zoom first so
    /// the change is visible). Stays within [MIN_PCT, MAX_PCT].
    pub fn grow_pty(&mut self, step: u16) {
        self.zoom = Zoom::Normal;
        self.pty_pct = (self.pty_pct + step).min(Self::MAX_PCT);
    }

    /// Shrink the terminal pane by `step` percent (counterpart of `grow_pty`).
    pub fn shrink_pty(&mut self, step: u16) {
        self.zoom = Zoom::Normal;
        self.pty_pct = self.pty_pct.saturating_sub(step).max(Self::MIN_PCT);
    }

    /// A one-line description of the current arrangement (for `/layout`).
    pub fn describe(&self) -> String {
        let zoom = match self.zoom {
            Zoom::Normal => "split",
            Zoom::Term => "terminal-fullscreen",
            Zoom::Chat => "chat-fullscreen",
        };
        let roster = if self.roster_width == 0 {
            "off".to_string()
        } else {
            self.roster_width.to_string()
        };
        format!(
            "terminal {}% · chat {}% · roster {} · {}",
            self.pty_pct,
            100 - self.pty_pct,
            roster,
            zoom
        )
    }

    /// Persist this arrangement to `layouts/<slug>.toml` so it can be re-applied
    /// later with `/layout load <slug>`. Returns the slug actually written.
    pub fn save(&self, name: &str) -> anyhow::Result<String> {
        let slug = slugify(name);
        anyhow::ensure!(!slug.is_empty(), "give the layout a name (letters/digits)");
        std::fs::create_dir_all(LAYOUTS_DIR)
            .with_context(|| format!("creating {LAYOUTS_DIR}"))?;
        let path = format!("{LAYOUTS_DIR}/{slug}.toml");
        let body = toml::to_string_pretty(self)
            .with_context(|| format!("serialize layout '{slug}'"))?;
        std::fs::write(&path, body).with_context(|| format!("write {path}"))?;
        Ok(slug)
    }

    /// Load a saved arrangement by name (`layouts/<slug>.toml`), re-clamped.
    pub fn by_name(name: &str) -> anyhow::Result<Self> {
        let slug = slugify(name);
        let path = format!("{LAYOUTS_DIR}/{slug}.toml");
        let s = std::fs::read_to_string(&path)
            .with_context(|| format!("layout '{slug}' ({path})"))?;
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

    #[test]
    fn clamp_bounds_pct_and_roster() {
        let mut l = Layout {
            pty_pct: 999,
            roster_width: 999,
            zoom: Zoom::Normal,
        };
        l.clamp();
        assert_eq!(l.pty_pct, Layout::MAX_PCT);
        assert_eq!(l.roster_width, Layout::MAX_ROSTER);
        let mut low = Layout {
            pty_pct: 1,
            roster_width: 0,
            zoom: Zoom::Normal,
        };
        low.clamp();
        assert_eq!(low.pty_pct, Layout::MIN_PCT);
        assert_eq!(low.roster_width, 0); // 0 is valid (hidden)
    }

    #[test]
    fn effective_pct_full_under_term_zoom() {
        let mut l = Layout::default();
        assert_eq!(l.effective_pty_pct(), 55);
        l.zoom = Zoom::Term;
        assert_eq!(l.effective_pty_pct(), 100);
        l.zoom = Zoom::Chat;
        assert_eq!(l.effective_pty_pct(), 55); // chat hidden ≠ resize the pty
    }

    #[test]
    fn cycle_and_resize_drop_out_of_zoom() {
        let mut l = Layout::default();
        l.cycle_zoom();
        assert_eq!(l.zoom, Zoom::Term);
        l.grow_pty(5);
        assert_eq!(l.zoom, Zoom::Normal);
        assert_eq!(l.pty_pct, 60);
        l.shrink_pty(100);
        assert_eq!(l.pty_pct, Layout::MIN_PCT);
    }
}
