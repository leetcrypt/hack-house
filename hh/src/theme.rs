//! Loadable colour/layout themes ("vestments"). Default is "crypt": occult
//! monochrome — white/grey ink on a lifted slate surface, ✝ accents. Switch to
//! the neon "church" vestments with `/theme church` or a TOML via `--theme`.

use anyhow::Context;
use ratatui::style::Color;
use serde::{Deserialize, Serialize};

/// Where the bundled `*.toml` vestments live, so `/theme <name>` can resolve a
/// bare name to a file at runtime (mirrors sbx.rs's script path const).
pub const THEMES_DIR: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/themes");

#[derive(Debug, Clone, Deserialize, Serialize)]
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
    /// Each theme picks its own sigil (crypt: ✝, church: †, …).
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

    /// Conjure a brand-new vestment from scratch — not one of the bundled
    /// presets. Rolls a coherent palette in HSV (dark tinted surface, one bright
    /// saturated accent, light legible ink), a random occult sigil, and a
    /// generated arcane name. Bound to Ctrl+Alt+P / `/theme random`.
    pub fn random() -> Theme {
        let mut r = Rng::seeded();
        let base = r.range(0.0, 360.0); // base hue for the surface/ink family
                                        // Accent sits at an analogous or complementary offset for contrast.
        let accent_hue = (base + *r.pick(&[30.0, 150.0, 180.0, 210.0, 330.0_f32])) % 360.0;

        let border = hsv(base, r.range(0.18, 0.42), r.range(0.40, 0.55));
        let accent = hsv(accent_hue, r.range(0.70, 1.0), r.range(0.85, 1.0));
        // Title is either the accent or a near-white tint of the base hue.
        let title = if r.f32() < 0.5 {
            accent
        } else {
            hsv(base, r.range(0.0, 0.12), 1.0)
        };
        let me = hsv(accent_hue, r.range(0.0, 0.22), r.range(0.92, 1.0)); // your ink: bright
        let other = hsv(base, r.range(0.14, 0.38), r.range(0.74, 0.90));
        let system = hsv(base, r.range(0.20, 0.45), r.range(0.58, 0.74));
        let dim = hsv(base, r.range(0.14, 0.34), r.range(0.50, 0.66));

        // The surface is no longer rolled blind: derive it from the ink we just
        // rolled so it's *guaranteed* legible yet as expressive as that allows.
        // `legible_bg` walks the base-hue slate from near-black upward and keeps
        // the lightest tint at which the primary chat ink still clears a contrast
        // floor — so brighter palettes earn a richer, more visible background and
        // cooler ones settle onto a deeper one, all while staying readable. Only
        // the reliably-light inks (`me`, `other`) gate the surface: the muted or
        // accent-tinted inks (`title` can be the accent, `system`/`dim` run dark
        // by design) would otherwise be impossible to satisfy and collapse the
        // background to flat black, so they ride along rather than veto the tint.
        let bg = legible_bg(base, r.range(0.30, 0.60), &[me, other]);

        Theme {
            name: format!("{}-{}", r.pick(&NAME_ADJ), r.pick(&NAME_NOUN)),
            border,
            title,
            accent,
            dim,
            me,
            other,
            system,
            input: me,
            roster_me: accent,
            bg,
            roster_width: 22,
            sigil: r.pick(&SIGILS).to_string(),
        }
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

    /// Persist this vestment to `themes/<name>.toml` so it survives the session
    /// and can be re-donned later with `/theme <name>`. Used to keep a roll from
    /// `random()` you want to wear again. Renames the theme to the saved slug so
    /// the file, the `name` field, and the `/theme` argument all agree.
    pub fn save(&self, name: &str) -> anyhow::Result<String> {
        let slug = slugify(name);
        anyhow::ensure!(
            !slug.is_empty(),
            "give the vestment a name (letters/digits)"
        );
        let mut t = self.clone();
        t.name = slug.clone();
        let path = format!("{THEMES_DIR}/{slug}.toml");
        let body =
            toml::to_string_pretty(&t).with_context(|| format!("serialize vestment '{slug}'"))?;
        std::fs::write(&path, body).with_context(|| format!("write {path}"))?;
        Ok(slug)
    }
}

/// Reduce a free-form name to a safe lowercase `themes/<slug>.toml` filename:
/// keep ascii letters/digits, fold any run of other chars to a single '-'.
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

/// Occult glyphs the randomizer can stamp as the title sigil.
const SIGILS: [&str; 12] = ["✝", "☧", "☥", "†", "‡", "✟", "♰", "☩", "⸸", "⯐", "✠", "☦"];

/// Arcane name fragments — `<adj>-<noun>` makes a memorable vestment name.
const NAME_ADJ: [&str; 16] = [
    "ashen", "umbral", "votive", "hollow", "gilded", "wraith", "septic", "occult", "molten",
    "veiled", "sallow", "rotting", "sacred", "cinder", "obsidian", "vesper",
];
const NAME_NOUN: [&str; 16] = [
    "reliquary",
    "ossuary",
    "vestment",
    "censer",
    "shroud",
    "chancel",
    "crypt",
    "sepulcher",
    "litany",
    "chalice",
    "rood",
    "narthex",
    "thurible",
    "psalter",
    "ossein",
    "vigil",
];

/// Convert HSV (h in degrees 0–360, s/v in 0–1) to an 8-bit-per-channel RGB
/// `Color`. Lets `random()` reason in a perceptual-ish space (pick a hue, then
/// dial saturation/value) instead of fumbling raw RGB.
fn hsv(h: f32, s: f32, v: f32) -> Color {
    let h = h.rem_euclid(360.0);
    let c = v * s;
    let x = c * (1.0 - ((h / 60.0) % 2.0 - 1.0).abs());
    let m = v - c;
    let (r, g, b) = match h as u32 / 60 {
        0 => (c, x, 0.0),
        1 => (x, c, 0.0),
        2 => (0.0, c, x),
        3 => (0.0, x, c),
        4 => (x, 0.0, c),
        _ => (c, 0.0, x),
    };
    let to = |f: f32| ((f + m) * 255.0).round().clamp(0.0, 255.0) as u8;
    Color::Rgb(to(r), to(g), to(b))
}

/// WCAG relative luminance of a colour, 0.0 (black) – 1.0 (white). Channels are
/// linearised (sRGB → linear) before weighting, so it tracks *perceived*
/// brightness rather than raw RGB — what we need to reason about legibility.
fn luminance(c: Color) -> f32 {
    let Color::Rgb(r, g, b) = c else { return 0.0 };
    let lin = |u: u8| {
        let s = u as f32 / 255.0;
        if s <= 0.03928 {
            s / 12.92
        } else {
            ((s + 0.055) / 1.055).powf(2.4)
        }
    };
    0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)
}

/// WCAG contrast ratio between two colours (1.0 = identical, 21.0 = black/white).
/// 4.5 is the threshold for comfortable body text, 3.0 for incidental UI chrome.
fn contrast(a: Color, b: Color) -> f32 {
    let (la, lb) = (luminance(a), luminance(b));
    let (hi, lo) = if la >= lb { (la, lb) } else { (lb, la) };
    (hi + 0.05) / (lo + 0.05)
}

/// Derive a background relationally from the inks rolled for it. Walk the surface
/// in the given `hue`/`sat` family from near-black upward and return the
/// *lightest* value at which every `ink` still clears the 4.5:1 body-text floor.
/// Because contrast only falls as the surface lightens, the lightest passing
/// value is the most expressive background that's still legible. Pass only inks
/// that are light enough to be satisfiable on some surface (e.g. `me`/`other`):
/// an intrinsically dark ink can never clear the floor and would collapse the
/// result to flat black.
fn legible_bg(hue: f32, sat: f32, ink: &[Color]) -> Color {
    const FLOOR: f32 = 4.5;
    let mut best = hsv(hue, sat, 0.0); // darkest, highest-contrast fallback
    let mut v = 0.0_f32;
    while v <= 0.45 {
        let bg = hsv(hue, sat, v);
        if ink.iter().all(|&c| contrast(c, bg) >= FLOOR) {
            best = bg; // still legible at this value — keep climbing for a richer tint
        }
        v += 0.005;
    }
    best
}

/// Tiny seeded xorshift64* PRNG — enough randomness to roll a fresh palette
/// without pulling in the `rand` crate. Seeded from the wall clock so every
/// `Ctrl+Alt+P` conjures a different vestment.
struct Rng(u64);

impl Rng {
    fn seeded() -> Self {
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos() as u64)
            .unwrap_or(0x9e37_79b9_7f4a_7c15);
        // Avoid the all-zero state, which xorshift can't escape.
        Rng(nanos | 1)
    }

    fn next_u64(&mut self) -> u64 {
        let mut x = self.0;
        x ^= x >> 12;
        x ^= x << 25;
        x ^= x >> 27;
        self.0 = x;
        x.wrapping_mul(0x2545_f491_4f6c_dd1d)
    }

    /// Uniform f32 in [0, 1).
    fn f32(&mut self) -> f32 {
        (self.next_u64() >> 40) as f32 / (1u64 << 24) as f32
    }

    /// Uniform f32 in [lo, hi).
    fn range(&mut self, lo: f32, hi: f32) -> f32 {
        lo + self.f32() * (hi - lo)
    }

    /// Borrow a random element from a slice.
    fn pick<'a, T>(&mut self, items: &'a [T]) -> &'a T {
        &items[(self.next_u64() % items.len() as u64) as usize]
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

    #[test]
    fn random_yields_a_usable_vestment() {
        let t = Theme::random();
        // Name is `<adj>-<noun>` and the sigil is one of the occult glyphs.
        assert!(t.name.contains('-'), "name should be adj-noun: {}", t.name);
        assert!(
            SIGILS.contains(&t.sigil.as_str()),
            "sigil from set: {}",
            t.sigil
        );
        assert_eq!(t.roster_width, 22);
        assert!(matches!(t.bg, Color::Rgb(..)), "random bg should be Rgb");
        // The surface is derived from the inks to stay legible: it must never be
        // brighter than the text on it, and `me` (always near-white) must clear
        // the body-text contrast floor against it.
        assert!(
            luminance(t.bg) <= luminance(t.me),
            "bg should not outshine the ink"
        );
        assert!(
            contrast(t.me, t.bg) >= 4.5,
            "your ink must stay readable on the surface: {:.2}",
            contrast(t.me, t.bg)
        );
    }

    #[test]
    fn legible_bg_is_relational_and_readable() {
        // contrast() sanity: identical = 1, black/white = 21.
        let white = Color::Rgb(255, 255, 255);
        let black = Color::Rgb(0, 0, 0);
        assert!((contrast(white, white) - 1.0).abs() < 1e-3);
        assert!((contrast(white, black) - 21.0).abs() < 0.1);

        // Given bright inks, the surface stays under the 4.5 floor yet isn't
        // forced all the way to black — it earns a visible tint.
        let inks = [Color::Rgb(0xff, 0xff, 0xff), Color::Rgb(0xd0, 0xd0, 0xd0)];
        let bg = legible_bg(210.0, 0.5, &inks);
        for ink in inks {
            assert!(
                contrast(ink, bg) >= 4.5,
                "ink unreadable: {:.2}",
                contrast(ink, bg)
            );
        }
        assert!(
            luminance(bg) > 0.0,
            "bright inks should permit a tinted, non-black bg"
        );

        // A darker ink set forces a correspondingly deeper surface — the
        // relationship runs the right way.
        let dim_inks = [Color::Rgb(0x88, 0x88, 0x88)];
        let dark_bg = legible_bg(210.0, 0.5, &dim_inks);
        assert!(
            luminance(dark_bg) <= luminance(bg),
            "dimmer ink should yield a darker surface"
        );
    }

    #[test]
    fn random_theme_serializes_and_reloads() {
        // A saved roll must reload byte-for-identical, so `/theme save` →
        // `/theme <name>` gives you back the exact vestment.
        let t = Theme::random();
        let toml = toml::to_string_pretty(&t).expect("serialize");
        let back: Theme = toml::from_str(&toml).expect("reload");
        assert_eq!(back.border, t.border);
        assert_eq!(back.accent, t.accent);
        assert_eq!(back.bg, t.bg);
        assert_eq!(back.sigil, t.sigil);
        assert_eq!(back.roster_width, t.roster_width);
    }

    #[test]
    fn slugify_makes_safe_names() {
        assert_eq!(slugify("  Cool Theme!! "), "cool-theme");
        assert_eq!(slugify("ashen/crypt"), "ashen-crypt");
        assert_eq!(slugify("votive-litany"), "votive-litany");
        assert_eq!(slugify("!!!"), "");
    }

    #[test]
    fn hsv_primaries_round_trip() {
        assert_eq!(hsv(0.0, 1.0, 1.0), Color::Rgb(255, 0, 0));
        assert_eq!(hsv(120.0, 1.0, 1.0), Color::Rgb(0, 255, 0));
        assert_eq!(hsv(240.0, 1.0, 1.0), Color::Rgb(0, 0, 255));
        assert_eq!(hsv(0.0, 0.0, 1.0), Color::Rgb(255, 255, 255));
    }
}
