//! Background music for terminal sessions — "hacker vibes". Plays bundled
//! CC-BY albums (see `hh/music/*.json`) or the operator's own imported files
//! through an *external* player (mpv / ffplay / cvlc), shelled out like the
//! sandbox backends so the Rust binary itself stays codec- and audio-device
//! free. Driven by `/music` in `app::handle_command`; the run loop's tick calls
//! `Player::tick` to auto-advance between tracks.

use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};

/// Where the bundled albums live, so `/music play <name>` resolves a bare name
/// to a manifest at runtime (mirrors theme.rs's `THEMES_DIR`). Each `*.json`
/// here is one playlist; its `src`s are paths relative to this directory.
pub const MUSIC_DIR: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/music");

/// File extensions we treat as importable audio for `/music import <dir>`.
const AUDIO_EXTS: &[&str] = &[
    "mp3", "ogg", "oga", "flac", "wav", "m4a", "aac", "opus", "wma",
];

#[derive(Debug, Clone, Default, Deserialize, Serialize)]
#[serde(default)]
pub struct Track {
    pub title: String,
    pub artist: String,
    /// A stream URL (`scheme://…`), an absolute file path, or a path relative to
    /// the playlist's own directory.
    pub src: String,
    /// Track length in seconds, if known (0 = unknown). Display-only.
    pub secs: u32,
}

#[derive(Debug, Clone, Default, Deserialize, Serialize)]
#[serde(default)]
pub struct Playlist {
    pub name: String,
    pub about: String,
    pub license: String,
    pub tracks: Vec<Track>,
}

/// External players we know how to drive, in preference order. Each plays a
/// single source to its end and then exits, which is exactly the signal
/// `Player::tick` waits on to advance to the next track.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Engine {
    Mpv,
    Ffplay,
    Cvlc,
}

impl Engine {
    /// First installed player, searching PATH. `None` means the host has no
    /// audio backend and `/music` can't play anything.
    fn detect() -> Option<Engine> {
        for (bin, eng) in [
            ("mpv", Engine::Mpv),
            ("ffplay", Engine::Ffplay),
            ("cvlc", Engine::Cvlc),
        ] {
            if in_path(bin) {
                return Some(eng);
            }
        }
        None
    }

    fn bin(self) -> &'static str {
        match self {
            Engine::Mpv => "mpv",
            Engine::Ffplay => "ffplay",
            Engine::Cvlc => "cvlc",
        }
    }

    /// A headless, quiet, play-once command for a single `target` source.
    fn command(self, target: &str) -> Command {
        let mut c = Command::new(self.bin());
        match self {
            Engine::Mpv => {
                c.args(["--no-video", "--really-quiet", "--no-terminal"]).arg(target);
            }
            Engine::Ffplay => {
                c.args(["-nodisp", "-autoexit", "-hide_banner", "-loglevel", "error"])
                    .arg(target);
            }
            Engine::Cvlc => {
                c.args(["--intf", "dummy", "--play-and-exit", "--quiet"]).arg(target);
            }
        }
        c
    }
}

/// A live playback session: an album, a cursor into it, and the child player
/// process rendering the current track. Kept out of `App` (like the `/ai`
/// agent child) so the UI never touches a process handle; `App::now_playing`
/// mirrors `label()` for display. Dropping it always stops the audio.
pub struct Player {
    playlist: Playlist,
    /// Directory the playlist's relative `src`s resolve against.
    base: PathBuf,
    idx: usize,
    engine: Engine,
    child: Child,
}

impl Player {
    /// Load `name` (user library shadows bundled) and start playing its first
    /// track. Errors carry a chat-ready message.
    pub fn start(name: &str) -> Result<Player, String> {
        let engine = Engine::detect().ok_or_else(|| {
            "no audio player found — install ffplay (ffmpeg), mpv, or vlc".to_string()
        })?;
        let (playlist, base) = load(name)?;
        if playlist.tracks.is_empty() {
            return Err(format!("album '{name}' has no tracks"));
        }
        let target = resolve(&base, &playlist.tracks[0].src);
        let child = spawn(engine, &target)?;
        Ok(Player {
            playlist,
            base,
            idx: 0,
            engine,
            child,
        })
    }

    /// Poll the player once (call on the run-loop tick). Returns:
    /// - `None` — the current track is still playing.
    /// - `Some(Ok(label))` — the track finished; advanced to a new one.
    /// - `Some(Err(e))` — couldn't continue; caller should drop the player.
    pub fn tick(&mut self) -> Option<Result<String, String>> {
        match self.child.try_wait() {
            Ok(Some(_)) => Some(self.advance()),
            Ok(None) => None,
            Err(_) => None, // transient wait error — retry next tick
        }
    }

    /// Kill the current track and jump to the next (wrapping). Backs `/music
    /// next` / `/music skip`.
    pub fn skip(&mut self) -> Result<String, String> {
        let _ = self.child.kill();
        let _ = self.child.wait();
        self.advance()
    }

    /// Stop playback and reap the child.
    pub fn stop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }

    /// "album ▸ Track Title" — mirrored into `App::now_playing` for the top bar.
    pub fn label(&self) -> String {
        format!("{} ▸ {}", self.playlist.name, self.playlist.tracks[self.idx].title)
    }

    fn advance(&mut self) -> Result<String, String> {
        self.idx = (self.idx + 1) % self.playlist.tracks.len();
        let target = resolve(&self.base, &self.playlist.tracks[self.idx].src);
        self.child = spawn(self.engine, &target)?;
        Ok(self.label())
    }
}

impl Drop for Player {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

/// Spawn the chosen player on one source, muting its stdio into a temp log so it
/// can never scribble on the TUI.
fn spawn(engine: Engine, target: &str) -> Result<Child, String> {
    let log = std::env::temp_dir().join("hh-music.log");
    let (out, err) = match std::fs::File::create(&log) {
        Ok(f) => {
            let dup = f.try_clone().map_err(|e| e.to_string())?;
            (Stdio::from(f), Stdio::from(dup))
        }
        Err(_) => (Stdio::null(), Stdio::null()),
    };
    engine
        .command(target)
        .stdin(Stdio::null())
        .stdout(out)
        .stderr(err)
        .spawn()
        .map_err(|e| format!("could not start {} ({e})", engine.bin()))
}

/// Turn a track `src` into something the player can open: URLs and absolute
/// paths pass through; a bare/relative path resolves against the playlist dir.
fn resolve(base: &Path, src: &str) -> String {
    if src.contains("://") {
        return src.to_string();
    }
    let p = Path::new(src);
    if p.is_absolute() {
        src.to_string()
    } else {
        base.join(src).to_string_lossy().into_owned()
    }
}

/// The user's personal album library, where `/music import` writes. `None` if
/// `$HOME` is unset.
pub fn user_dir() -> Option<PathBuf> {
    std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".hh/music"))
}

/// Album search path: the user library first (so it can shadow a bundled name),
/// then the shipped albums.
fn dirs() -> Vec<PathBuf> {
    let mut v = Vec::new();
    if let Some(u) = user_dir() {
        v.push(u);
    }
    v.push(PathBuf::from(MUSIC_DIR));
    v
}

/// Every album name (`*.json` stem) across the search path, deduped + sorted.
pub fn available() -> Vec<String> {
    let mut set = std::collections::BTreeSet::new();
    for dir in dirs() {
        if let Ok(rd) = std::fs::read_dir(&dir) {
            for e in rd.flatten() {
                let path = e.path();
                if path.extension().and_then(|s| s.to_str()) == Some("json") {
                    if let Some(stem) = path.file_stem().and_then(|s| s.to_str()) {
                        set.insert(stem.to_string());
                    }
                }
            }
        }
    }
    set.into_iter().collect()
}

/// Load an album by name (user library shadows bundled). Returns the parsed
/// playlist and the directory its relative `src`s resolve against.
fn load(name: &str) -> Result<(Playlist, PathBuf), String> {
    for dir in dirs() {
        let file = dir.join(format!("{name}.json"));
        if file.is_file() {
            let s = std::fs::read_to_string(&file).map_err(|e| e.to_string())?;
            let mut pl: Playlist =
                serde_json::from_str(&s).map_err(|e| format!("parse {}: {e}", file.display()))?;
            if pl.name.is_empty() {
                pl.name = name.to_string();
            }
            return Ok((pl, dir));
        }
    }
    Err(format!("no album '{name}' — try: {}", once_or_none(available())))
}

/// Roll a random installed album name (backs `/music random`).
pub fn random() -> Option<String> {
    let all = available();
    if all.is_empty() {
        return None;
    }
    let n = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos() as usize)
        .unwrap_or(0);
    Some(all[n % all.len()].clone())
}

/// Import a file or a directory of audio into the user library as a new album,
/// referencing the files in place (absolute paths — nothing is copied).
/// Returns the album name and track count.
pub fn import(path: &str, as_name: Option<&str>) -> Result<(String, usize), String> {
    let p = Path::new(path);
    if !p.exists() {
        return Err(format!("no such path: {path}"));
    }
    let mut tracks = Vec::new();
    if p.is_dir() {
        let mut files: Vec<PathBuf> = std::fs::read_dir(p)
            .map_err(|e| e.to_string())?
            .flatten()
            .map(|e| e.path())
            .filter(|q| is_audio(q))
            .collect();
        files.sort();
        for f in &files {
            tracks.push(track_from(f));
        }
    } else if is_audio(p) {
        tracks.push(track_from(p));
    } else {
        return Err(format!("not an audio file: {path}"));
    }
    if tracks.is_empty() {
        return Err(format!("no audio files found under {path}"));
    }

    let default_stem = p
        .file_stem()
        .or_else(|| p.file_name())
        .and_then(|s| s.to_str())
        .unwrap_or("import");
    let name = as_name
        .map(slugify)
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| slugify(default_stem));
    let name = if name.is_empty() { "import".to_string() } else { name };

    let dir = user_dir().ok_or_else(|| "no $HOME to store the album".to_string())?;
    std::fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    let count = tracks.len();
    let pl = Playlist {
        name: name.clone(),
        about: format!("imported from {path}"),
        license: "user-provided".into(),
        tracks,
    };
    let file = dir.join(format!("{name}.json"));
    let body = serde_json::to_string_pretty(&pl).map_err(|e| e.to_string())?;
    std::fs::write(&file, body).map_err(|e| format!("write {}: {e}", file.display()))?;
    Ok((name, count))
}

fn track_from(p: &Path) -> Track {
    let abs = std::fs::canonicalize(p).unwrap_or_else(|_| p.to_path_buf());
    let title = p
        .file_stem()
        .and_then(|s| s.to_str())
        .unwrap_or("track")
        .to_string();
    Track {
        title,
        artist: String::new(),
        src: abs.to_string_lossy().into_owned(),
        secs: 0,
    }
}

fn is_audio(p: &Path) -> bool {
    p.is_file()
        && p.extension()
            .and_then(|s| s.to_str())
            .map(|e| AUDIO_EXTS.contains(&e.to_ascii_lowercase().as_str()))
            .unwrap_or(false)
}

/// Is `bin` an executable name reachable on `$PATH`? Dependency-free stand-in
/// for the `which` crate — good enough to pick an installed player.
fn in_path(bin: &str) -> bool {
    std::env::var_os("PATH")
        .map(|path| std::env::split_paths(&path).any(|d| d.join(bin).is_file()))
        .unwrap_or(false)
}

/// Reduce a free-form album name to a safe `<slug>.json` filename.
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

/// Join names with " · ", or say "(none)" so an empty list still reads cleanly.
pub fn once_or_none(items: Vec<String>) -> String {
    if items.is_empty() {
        "(none)".to_string()
    } else {
        items.join(" · ")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bundled_albums_are_discoverable() {
        // The shipped albums must be found by name so `/music play crypt` works.
        let names = available();
        assert!(names.contains(&"crypt".to_string()), "albums: {names:?}");
        assert!(names.contains(&"terminal".to_string()), "albums: {names:?}");
    }

    #[test]
    fn bundled_albums_parse_and_have_tracks() {
        for name in ["crypt", "terminal"] {
            let (pl, base) = load(name).expect("bundled album loads");
            assert_eq!(pl.name, name);
            assert!(!pl.tracks.is_empty(), "{name} has tracks");
            // Every relative src must resolve to a file that actually shipped.
            for t in &pl.tracks {
                let path = resolve(&base, &t.src);
                assert!(
                    Path::new(&path).is_file(),
                    "{name}: missing track file {path}"
                );
            }
        }
    }

    #[test]
    fn resolve_passes_urls_and_absolutes_through() {
        let base = Path::new("/tmp/albums");
        assert_eq!(resolve(base, "http://x/y.mp3"), "http://x/y.mp3");
        assert_eq!(resolve(base, "/abs/a.mp3"), "/abs/a.mp3");
        assert_eq!(resolve(base, "sub/a.mp3"), "/tmp/albums/sub/a.mp3");
    }

    #[test]
    fn slugify_makes_safe_names() {
        assert_eq!(slugify("  My Mix!! "), "my-mix");
        assert_eq!(slugify("late/night"), "late-night");
        assert_eq!(slugify("!!!"), "");
    }
}
