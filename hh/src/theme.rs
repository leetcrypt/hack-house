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
    fn default() -> Self {
        Self {
            name: "crypt".into(),
            border: Color::DarkGray,
            title: Color::White,
            accent: Color::White,
            dim: Color::DarkGray,
            me: Color::White,
            other: Color::Gray,
            system: Color::DarkGray,
            input: Color::White,
            roster_me: Color::White,
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
