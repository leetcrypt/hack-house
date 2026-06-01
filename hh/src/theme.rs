//! Loadable colour/layout themes ("vestments"). Default is "crypt": occult
//! monochrome — white/grey ink on a lifted slate surface, ✝ accents. Switch to
//! the neon "church" vestments with `/theme church` or a TOML via `--theme`.

use anyhow::Context;
use ratatui::style::Color;
use serde::Deserialize;

/// Where the bundled `*.toml` vestments live, so `/theme <name>` can resolve a
/// bare name to a file at runtime (mirrors sbx.rs's script path const).
pub const THEMES_DIR: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/themes");

#[derive(Debug, Clone, Deserialize)]
#[serde(default)]
pub struct Theme {
    pub name: String,
    pub border: Color,
    pub title: Color,
    pub accent: Color,
    pub dim: Color,
    pub me: Color,
    pub other: Color,
    pub system: Color,
    pub input: Color,
    pub roster_me: Color,
    /// Panel/background fill. `Reset` = the terminal's own background (no fill);
    /// a solid colour lifts the text onto a contrasting surface for legibility.
    pub bg: Color,
    /// Width of the roster column.
    pub roster_width: u16,
    /// Glyph flanking the "hack-house" title (and used for occult accents).
    /// Each theme picks its own sigil (crypt: ✝, church: ⛧, …).
    pub sigil: String,
}

impl Default for Theme {
    /// "crypt" — occult monochrome: white ink on a lifted slate surface. Gray
    /// window-chrome, readable mid-gray timestamps, muted-gray system lines, a
    /// ✝ sigil. Neutral by default; switch to the neon "church" vestments with
    /// `/theme church` or `--theme`.
    fn default() -> Self {
        Self {
            name: "crypt".into(),
            border: Color::Rgb(0x6e, 0x6e, 0x7a), // gray chrome, defined against the surface
            title: Color::Rgb(0xff, 0xff, 0xff),  // white
            accent: Color::Rgb(0xff, 0xff, 0xff), // white (sigil / prompt)
            dim: Color::Rgb(0x9a, 0x9a, 0xa6),    // timestamps — readable mid-gray
            me: Color::Rgb(0xff, 0xff, 0xff),     // your messages = white
            other: Color::Rgb(0xc8, 0xc8, 0xd0),  // others = bright gray
            system: Color::Rgb(0xb0, 0xb0, 0xbc), // system / occult = legible muted gray
            input: Color::Rgb(0xff, 0xff, 0xff),
            roster_me: Color::Rgb(0xff, 0xff, 0xff), // you / owner = white
            bg: Color::Rgb(0x1c, 0x1c, 0x22),        // slate panel lifts text off pure-black
            roster_width: 22,
            sigil: "✝".into(), // inverted cross / crypt
        }
    }
}

impl Theme {
    pub fn load(path: &str) -> anyhow::Result<Self> {
        let s = std::fs::read_to_string(path)?;
        Ok(toml::from_str(&s)?)
    }

    /// Resolve a bare vestment name (e.g. "neon") to a bundled `themes/<name>.toml`.
    /// Used by the in-session `/theme` command for live switching.
    pub fn by_name(name: &str) -> anyhow::Result<Self> {
        let path = format!("{THEMES_DIR}/{name}.toml");
        Self::load(&path).with_context(|| format!("theme '{name}' ({path})"))
    }

    /// Names of the bundled vestments, sorted, for `/theme` with no argument.
    pub fn available() -> Vec<String> {
        let mut names: Vec<String> = std::fs::read_dir(THEMES_DIR)
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
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_is_crypt() {
        let t = Theme::default();
        assert_eq!(t.name, "crypt");
        assert_eq!(t.accent, Color::Rgb(0xff, 0xff, 0xff));
    }

    #[test]
    fn hex_theme_toml_deserializes() {
        // The --theme files use #rrggbb; make sure ratatui's serde accepts it.
        let toml = r##"
name = "x"
border = "#19b3ff"
accent = "#39ff14"
me = "#39ff14"
roster_width = 24
"##;
        let t: Theme = toml::from_str(toml).expect("hex theme must parse");
        assert_eq!(t.border, Color::Rgb(0x19, 0xb3, 0xff));
        assert_eq!(t.roster_width, 24);
        // missing fields fall back to the church default
        assert_eq!(t.system, Theme::default().system);
    }
}
