//! Host-global VM registry — the join between a saved snapshot and what it's FOR.
//!
//! `/sbx save` only ever produced an opaque engine artifact (a `hh-snap:<label>`
//! image tag, an `hh-snapshots/*.tar`, …); the rich `.hh-agent` manifest that
//! describes *why* a VM exists lives **inside** the VM and is invisible until you
//! boot it. This module records, in one host-global file, an entry per saved VM:
//! where its artifact is, plus a cached summary (purpose / status / next-todo)
//! scraped from the VM's manifest at save time. That makes saved work browsable
//! (`/sbx browse`) and selectable by an agent (`operator registry list`).
//!
//! Scope is deliberately **global** (`~/.hh/registry.json`), not per-room/per-repo:
//! the artifacts it indexes are already host-level (one docker image store, one
//! `hh-snapshots/` dir visible to every room), so a per-room split would hide the
//! same image from other rooms. Each entry is tagged with its origin (`created_by`,
//! `repo`) instead, and `list()` reconciles against what still exists so the file
//! never grows past the live snapshots.

use std::collections::BTreeMap;
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::time::{SystemTime, UNIX_EPOCH};

use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};

const REGISTRY_VERSION: u32 = 1;

/// One saved VM: its artifact location plus a cached, human/agent-readable summary
/// of the work it carries. Unknown future fields on disk are ignored on read.
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Entry {
    pub label: String,
    /// docker | podman | multipass | vbox
    pub backend: String,
    /// "image" (engine tag in `artifact_ref`) or "file" (path in `artifact_ref`)
    pub artifact_kind: String,
    pub artifact_ref: String,
    #[serde(default)]
    pub size_bytes: Option<u64>,
    #[serde(default)]
    pub created_unix: u64,
    #[serde(default)]
    pub created_by: String,
    /// Originating working dir (basename) — origin tag, not a scoping key.
    #[serde(default)]
    pub repo: String,
    // ── cached manifest summary (best-effort; empty if the VM had no .hh-agent) ──
    #[serde(default)]
    pub purpose: String,
    #[serde(default)]
    pub status: String,
    #[serde(default)]
    pub todo: Vec<String>,
    // ── trading (Phase B) ──────────────────────────────────────────────────
    /// Marked shareable by `/sbx publish`. Only shareable entries appear in the
    /// catalog a peer receives; a non-shareable entry is private to this host.
    #[serde(default)]
    pub shareable: bool,
    /// Free-form skill tags (e.g. "recon", "kali") for catalog filtering.
    #[serde(default)]
    pub tags: Vec<String>,
    /// Portable, sendable artifact path (a `.tar`/`.ova` under `hh-snapshots/`).
    /// An image-backed entry only becomes truly tradeable once it's been exported
    /// to a file a peer can receive over `/send`; `publish` fills this in.
    #[serde(default)]
    pub share_path: String,
}

impl Entry {
    /// The single most useful "what's next" line for a one-row table cell.
    pub fn next_todo(&self) -> &str {
        self.todo.first().map(String::as_str).unwrap_or("")
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
struct Registry {
    version: u32,
    entries: BTreeMap<String, Entry>,
}

impl Default for Registry {
    fn default() -> Self {
        Registry { version: REGISTRY_VERSION, entries: BTreeMap::new() }
    }
}

/// `~/.hh/registry.json`. Falls back to `./.hh/registry.json` if `$HOME` is unset.
pub fn registry_path() -> PathBuf {
    let base = std::env::var("HOME").map(PathBuf::from).unwrap_or_else(|_| PathBuf::from("."));
    base.join(".hh").join("registry.json")
}

fn load() -> Registry {
    let path = registry_path();
    let Ok(text) = std::fs::read_to_string(&path) else {
        return Registry::default();
    };
    serde_json::from_str(&text).unwrap_or_default()
}

fn store(reg: &Registry) -> Result<()> {
    let path = registry_path();
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir).with_context(|| format!("creating {}", dir.display()))?;
    }
    let text = serde_json::to_string_pretty(reg).context("serializing registry")?;
    // Write to a per-process temp then atomically rename into place, so a reader
    // (or a concurrent wave member taking the lock next) never observes a
    // half-written file even if this process is killed mid-write.
    let tmp = path.with_extension(format!("json.tmp.{}", std::process::id()));
    std::fs::write(&tmp, text).with_context(|| format!("writing {}", tmp.display()))?;
    std::fs::rename(&tmp, &path).with_context(|| format!("committing {}", path.display()))?;
    Ok(())
}

/// How long a lockfile may sit before it's presumed stale (its holder died) and
/// forcibly reclaimed. Generous enough for a slow `export_image` of a fat VM.
const LOCK_STALE_SECS: u64 = 120;

/// An advisory cross-process lock on the registry, held for the duration of one
/// read-modify-write. Dropping it releases the lock (removes the lockfile).
struct RegistryLock {
    path: PathBuf,
}

impl Drop for RegistryLock {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.path);
    }
}

fn lock_path() -> PathBuf {
    registry_path().with_extension("lock")
}

/// Take the registry lock, spinning until it's free or presumed-stale. The lock
/// is a `create_new` (O_EXCL) sentinel file — atomic across processes on a local
/// filesystem — which lets concurrent `/loop` wave members serialize their
/// registry writes instead of clobbering each other through last-writer-wins.
fn acquire_lock() -> Result<RegistryLock> {
    let path = lock_path();
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir).with_context(|| format!("creating {}", dir.display()))?;
    }
    let deadline =
        std::time::Instant::now() + std::time::Duration::from_secs(LOCK_STALE_SECS + 5);
    loop {
        match std::fs::OpenOptions::new().write(true).create_new(true).open(&path) {
            Ok(mut f) => {
                use std::io::Write;
                let _ = writeln!(f, "{}", std::process::id());
                return Ok(RegistryLock { path });
            }
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {
                // Reclaim a lock whose holder appears to have died.
                if let Ok(age) = std::fs::metadata(&path).and_then(|m| m.modified()) {
                    if age.elapsed().map(|d| d.as_secs() > LOCK_STALE_SECS).unwrap_or(false) {
                        let _ = std::fs::remove_file(&path);
                        continue;
                    }
                }
                if std::time::Instant::now() > deadline {
                    anyhow::bail!(
                        "registry locked (held >{LOCK_STALE_SECS}s) — another save/publish in progress?"
                    );
                }
                std::thread::sleep(std::time::Duration::from_millis(50));
            }
            Err(e) => return Err(e).with_context(|| format!("opening {}", path.display())),
        }
    }
}

/// Insert or replace the entry for `entry.label`.
pub fn upsert(entry: Entry) -> Result<()> {
    let _lock = acquire_lock()?;
    let mut reg = load();
    reg.entries.insert(entry.label.clone(), entry);
    store(&reg)
}

/// Read one entry by label (None if unknown). Used by `/sbx pull`/`publish` to
/// look up an artifact without dragging in the engine reconcile probe.
pub fn get(label: &str) -> Option<Entry> {
    load().entries.remove(label)
}

/// Mark `label` shareable (Phase B `/sbx publish`): records the portable
/// `share_path` a peer can receive, merges in any `tags`, and flips `shareable`.
/// Errors if the label isn't in the registry. Idempotent.
pub fn publish(label: &str, share_path: &str, tags: &[String]) -> Result<Entry> {
    let _lock = acquire_lock()?;
    let mut reg = load();
    let entry = reg
        .entries
        .get_mut(label)
        .with_context(|| format!("no saved VM labelled '{label}' — /sbx browse to list"))?;
    mark_published(entry, share_path, tags);
    let out = entry.clone();
    store(&reg)?;
    Ok(out)
}

/// Pure mutation behind `publish` — flips shareable, sets the portable artifact
/// path, and merges tags without duplicating. Split out so it's testable without
/// touching the global on-disk registry.
fn mark_published(entry: &mut Entry, share_path: &str, tags: &[String]) {
    entry.shareable = true;
    entry.share_path = share_path.to_string();
    for t in tags {
        if !entry.tags.contains(t) {
            entry.tags.push(t.clone());
        }
    }
}

/// Whether an entry *names* a sendable artifact (shareable + has a `share_path`).
/// The on-disk existence check is layered on top in `list_shareable`.
fn is_offerable(e: &Entry) -> bool {
    e.shareable && !e.share_path.is_empty()
}

/// The catalog this host offers: shareable entries whose portable artifact still
/// exists on disk (a peer can only pull what we can actually send). Newest first.
pub fn list_shareable() -> Vec<Entry> {
    let mut v: Vec<Entry> = load()
        .entries
        .into_values()
        .filter(|e| is_offerable(e) && PathBuf::from(&e.share_path).exists())
        .collect();
    v.sort_by(|a, b| b.created_unix.cmp(&a.created_unix));
    v
}

/// Drops entries whose backing artifact no longer exists
/// (image pruned, file deleted). Persists the pruned set so the file self-limits.
/// `image_exists` is injected so the engine probe stays out of this module's deps;
/// it is asked only about `artifact_kind == "image"` entries.
pub fn list_reconciled(image_exists: impl Fn(&str, &str) -> bool) -> Vec<Entry> {
    // Best-effort lock: this prunes-and-stores, so serialize it against writers
    // when we can, but never block a read-only listing if the lock is contended.
    let _lock = acquire_lock().ok();
    let mut reg = load();
    let before = reg.entries.len();
    reg.entries.retain(|_, e| match e.artifact_kind.as_str() {
        "file" => PathBuf::from(&e.artifact_ref).exists(),
        "image" => image_exists(&e.backend, &e.artifact_ref),
        _ => true,
    });
    if reg.entries.len() != before {
        let _ = store(&reg);
    }
    let mut v: Vec<Entry> = reg.entries.into_values().collect();
    v.sort_by(|a, b| b.created_unix.cmp(&a.created_unix));
    v
}

pub fn now_unix() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
}

/// Basename of the current working dir — the origin tag for a new entry.
pub fn cwd_repo() -> String {
    std::env::current_dir()
        .ok()
        .and_then(|p| p.file_name().map(|s| s.to_string_lossy().into_owned()))
        .unwrap_or_default()
}

/// Read `<root>/.hh-agent/manifest.yaml` out of a *running* OCI container via
/// `<engine> exec <name> cat …`. Returns the raw YAML text, or None if the
/// container isn't running / has no manifest. Blocking.
pub fn read_container_manifest(engine: &str, name: &str, root: &str) -> Option<String> {
    let path = format!("{}/.hh-agent/manifest.yaml", root.trim_end_matches('/'));
    let out = Command::new(engine)
        .args(["exec", name, "cat", &path])
        .stderr(Stdio::null())
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    let text = String::from_utf8_lossy(&out.stdout).into_owned();
    if text.trim().is_empty() {
        None
    } else {
        Some(text)
    }
}

/// Extract `(purpose, status, todo)` from an `.hh-agent` `manifest.yaml`.
///
/// This is a deliberately small, format-specific scraper rather than a real YAML
/// parser (the crate has no YAML dep, and the file is machine-written by *our*
/// `manifest.py` with a stable, block-style shape). It tolerates anything it
/// doesn't recognise by returning empty fields — the entry is still recorded.
pub fn scan_manifest(yaml: &str) -> (String, String, Vec<String>) {
    let mut purpose = String::new();
    let mut status = String::new();
    let mut todo: Vec<String> = Vec::new();

    let lines: Vec<&str> = yaml.lines().collect();
    let mut i = 0;
    while i < lines.len() {
        let line = lines[i];
        let trimmed = line.trim_start();

        // `purpose:` is a top-level (column-0) scalar.
        if !line.starts_with(char::is_whitespace) {
            if let Some(rest) = line.strip_prefix("purpose:") {
                purpose = unquote(rest.trim());
            }
        }
        // `status:` only appears nested under `state:`; match it wherever indented.
        if status.is_empty() {
            if let Some(rest) = trimmed.strip_prefix("status:") {
                status = unquote(rest.trim());
            }
        }
        // `todo:` opens a block of `- item` bullets (or is inline `todo: []`).
        if let Some(rest) = trimmed.strip_prefix("todo:") {
            let after = rest.trim();
            if !after.is_empty() && after != "[]" {
                // inline flow list e.g. `todo: [a, b]` — best-effort split
                todo = parse_inline_list(after);
            } else if after.is_empty() {
                let mut j = i + 1;
                while j < lines.len() {
                    let b = lines[j].trim_start();
                    if let Some(item) = b.strip_prefix("- ") {
                        todo.push(unquote(item.trim()));
                        j += 1;
                    } else {
                        break;
                    }
                }
            }
        }
        i += 1;
    }
    (purpose, status, todo)
}

fn parse_inline_list(s: &str) -> Vec<String> {
    s.trim_start_matches('[')
        .trim_end_matches(']')
        .split(',')
        .map(|p| unquote(p.trim()))
        .filter(|p| !p.is_empty())
        .collect()
}

/// Strip one layer of surrounding YAML quoting (single or double) and undo the
/// doubled-single-quote escape PyYAML emits.
fn unquote(s: &str) -> String {
    let s = s.trim();
    if s.len() >= 2 && s.starts_with('"') && s.ends_with('"') {
        return s[1..s.len() - 1].replace("\\\"", "\"");
    }
    if s.len() >= 2 && s.starts_with('\'') && s.ends_with('\'') {
        return s[1..s.len() - 1].replace("''", "'");
    }
    s.to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    const SAMPLE: &str = "\
schema: hh-agent/v1
id: hh-abc123
name: kali-recon-vm
purpose: Kali recon sandbox, summoned in-room and stamped live
created_by: operator
vm:
  engine: podman
  image: kalilinux/kali-rolling
goals:
  objective: build the manifest tooling
  user_intent: tradeable VM state
  stop_conditions: []
state:
  status: in_progress
  progress: tooling built
  done:
  - wrote manifest.py
  todo:
  - pivot to 10.0.0.5
  - dump creds
  blockers: []
provenance:
- by: operator
  action: init
";

    #[test]
    fn scans_purpose_status_todo() {
        let (purpose, status, todo) = scan_manifest(SAMPLE);
        assert_eq!(purpose, "Kali recon sandbox, summoned in-room and stamped live");
        assert_eq!(status, "in_progress");
        assert_eq!(todo, vec!["pivot to 10.0.0.5".to_string(), "dump creds".to_string()]);
    }

    #[test]
    fn empty_todo_list_is_empty() {
        let y = "purpose: x\nstate:\n  status: done\n  todo: []\n  blockers: []\n";
        let (purpose, status, todo) = scan_manifest(y);
        assert_eq!(purpose, "x");
        assert_eq!(status, "done");
        assert!(todo.is_empty());
    }

    #[test]
    fn handles_quoted_scalars() {
        let y = "purpose: 'a, b: c'\nstate:\n  status: \"in_progress\"\n";
        let (purpose, status, _) = scan_manifest(y);
        assert_eq!(purpose, "a, b: c");
        assert_eq!(status, "in_progress");
    }

    #[test]
    fn missing_fields_are_empty_not_panic() {
        let (purpose, status, todo) = scan_manifest("name: bare\n");
        assert!(purpose.is_empty());
        assert!(status.is_empty());
        assert!(todo.is_empty());
    }

    #[test]
    fn mark_published_sets_fields_and_dedups_tags() {
        let mut e = Entry { tags: vec!["recon".into()], ..Default::default() };
        mark_published(&mut e, "/snaps/x.tar", &["recon".into(), "kali".into()]);
        assert!(e.shareable);
        assert_eq!(e.share_path, "/snaps/x.tar");
        // "recon" already present must not duplicate; "kali" appended.
        assert_eq!(e.tags, vec!["recon".to_string(), "kali".to_string()]);
    }

    #[test]
    fn offerable_requires_shareable_flag_and_path() {
        let base = Entry { share_path: "/snaps/x.tar".into(), shareable: true, ..Default::default() };
        assert!(is_offerable(&base));
        assert!(!is_offerable(&Entry { shareable: false, ..base.clone() }));
        assert!(!is_offerable(&Entry { share_path: String::new(), ..base }));
    }

    #[test]
    fn next_todo_picks_first() {
        let e = Entry { todo: vec!["one".into(), "two".into()], ..Default::default() };
        assert_eq!(e.next_todo(), "one");
        assert_eq!(Entry::default().next_todo(), "");
    }
}
