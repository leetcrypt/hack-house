//! Loadable colour/layout themes ("vestments"). Default is the churchofmalware
//! occult-monochrome: black ground, white/grey ink, ⛧ accents. Override with a
//! TOML file via `--theme <path>`.

use ratatui::style::Color;
use serde::Deserialize;

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
    /// Width of the roster column.
    pub roster_width: u16,
}

impl Default for Theme {
    /// "church" — Church of Malware: neon on black. Cyan window-chrome, acid-green
    /// text/prompts, purple system/occult lines, hot-magenta self/owner accents.
    fn default() -> Self {
        Self {
            name: "church".into(),
            border: Color::Rgb(0x19, 0xb3, 0xff),    // cyan window chrome
            title: Color::Rgb(0x7d, 0xf9, 0xff),     // bright cyan
            accent: Color::Rgb(0x39, 0xff, 0x14),    // acid green (⛧ glyphs, prompt)
            dim: Color::Rgb(0x47, 0x5a, 0x7a),       // muted slate-blue
            me: Color::Rgb(0x39, 0xff, 0x14),        // your messages = acid green
            other: Color::Rgb(0x56, 0xc8, 0xff),     // others = soft cyan
            system: Color::Rgb(0xb4, 0x6c, 0xff),    // system / occult = purple
            input: Color::Rgb(0x39, 0xff, 0x14),
            roster_me: Color::Rgb(0xff, 0x39, 0xc0), // you / owner = hot magenta
            roster_width: 22,
        }
    }
}

impl Theme {
    pub fn load(path: &str) -> anyhow::Result<Self> {
        let s = std::fs::read_to_string(path)?;
        Ok(toml::from_str(&s)?)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_is_church() {
        let t = Theme::default();
        assert_eq!(t.name, "church");
        assert_eq!(t.accent, Color::Rgb(0x39, 0xff, 0x14));
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
