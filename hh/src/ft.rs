//! File & directory transfer over the encrypted channel — wire-compatible with
//! the Python client's `_ft` protocol (offer/accept/reject/chunk/done, 64 KB
//! chunks, SHA-256 verified). Directories are streamed as a tar with `dir:true`
//! and extracted on receipt (with a path-traversal guard); a Python receiver
//! just saves the `.tar`.

use anyhow::{Context, Result};
use base64::engine::general_purpose::STANDARD;
use base64::Engine;
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::path::{Component, Path, PathBuf};

pub const MAX_SIZE: usize = 50 * 1024 * 1024;
pub const CHUNK: usize = 64 * 1024;

#[derive(Clone)]
pub struct Offer {
    pub id: String,
    pub name: String,
    pub size: u64,
    pub sha256: String,
    pub dir: bool,
    pub from: String,
}

pub enum Ft {
    Offer(Offer),
    Accept(String),
    Reject(String),
    Chunk { id: String, data: Vec<u8> },
    Done(String),
}

pub fn sha256_hex(data: &[u8]) -> String {
    let mut h = Sha256::new();
    h.update(data);
    hex::encode(h.finalize())
}

pub fn human(size: usize) -> String {
    let (mut s, units) = (size as f64, ["B", "KB", "MB", "GB"]);
    for u in units {
        if s < 1024.0 {
            return format!("{s:.1} {u}");
        }
        s /= 1024.0;
    }
    format!("{s:.1} TB")
}

/// Read the payload to offer for a path → (name, bytes, is_dir).
pub fn read_payload(path: &str) -> Result<(String, Vec<u8>, bool)> {
    let p = Path::new(path);
    let meta = std::fs::metadata(p).with_context(|| format!("not found: {path}"))?;
    if meta.is_dir() {
        let bytes = tar_dir(p)?;
        anyhow::ensure!(bytes.len() <= MAX_SIZE, "directory too large ({})", human(bytes.len()));
        let base = p.file_name().and_then(|s| s.to_str()).unwrap_or("dir");
        Ok((format!("{base}.tar"), bytes, true))
    } else {
        anyhow::ensure!(meta.len() as usize <= MAX_SIZE, "file too large (max 50 MB)");
        let bytes = std::fs::read(p)?;
        let name = p.file_name().and_then(|s| s.to_str()).unwrap_or("file").to_string();
        Ok((name, bytes, false))
    }
}

fn tar_dir(dir: &Path) -> Result<Vec<u8>> {
    let mut buf = Vec::new();
    {
        let mut tb = tar::Builder::new(&mut buf);
        let base = dir.file_name().unwrap_or_default();
        tb.append_dir_all(base, dir).context("tar directory")?;
        tb.finish()?;
    }
    Ok(buf)
}

/// Persist received bytes under `downloads`. Directories (tar) are extracted
/// with a guard rejecting absolute paths and `..` escapes (zip-slip).
pub fn save(downloads: &Path, offer: &Offer, data: &[u8]) -> Result<PathBuf> {
    std::fs::create_dir_all(downloads)?;
    if offer.dir {
        // Extract the tar's own top-level dir directly under downloads/.
        let mut ar = tar::Archive::new(data);
        let mut top: Option<std::ffi::OsString> = None;
        for entry in ar.entries()? {
            let mut e = entry?;
            let path = e.path()?.into_owned();
            // Explicit zip-slip guard (belt-and-suspenders; unpack_in also refuses).
            anyhow::ensure!(safe_entry(&path), "unsafe tar entry rejected: {}", path.display());
            if top.is_none() {
                top = path.components().next().map(|c| c.as_os_str().to_owned());
            }
            e.unpack_in(downloads)
                .with_context(|| format!("extract {}", path.display()))?;
        }
        Ok(top.map(|t| downloads.join(t)).unwrap_or_else(|| downloads.to_path_buf()))
    } else {
        let stem = Path::new(&offer.name).file_stem().and_then(|s| s.to_str()).unwrap_or("file");
        let ext = Path::new(&offer.name).extension().and_then(|s| s.to_str()).unwrap_or("");
        let dest = unique(downloads, stem, ext);
        std::fs::write(&dest, data)?;
        Ok(dest)
    }
}

/// A tar entry path is safe to extract iff it's relative and has no `..` escape.
fn safe_entry(path: &Path) -> bool {
    !path.is_absolute() && !path.components().any(|c| matches!(c, Component::ParentDir))
}

fn unique(dir: &Path, stem: &str, ext: &str) -> PathBuf {
    let mk = |n: usize| {
        let base = if n == 0 { stem.to_string() } else { format!("{stem}_{n}") };
        if ext.is_empty() { dir.join(base) } else { dir.join(format!("{base}.{ext}")) }
    };
    (0..).map(mk).find(|p| !p.exists()).unwrap()
}

/// Parse a decrypted `{"_ft":...}` frame. `sender` is the server-authenticated
/// username of the offerer (so receivers can show who's sending).
pub fn parse(text: &str, sender: &str) -> Option<Ft> {
    let v: Value = serde_json::from_str(text).ok()?;
    match v["_ft"].as_str()? {
        "offer" => Some(Ft::Offer(Offer {
            id: v["id"].as_str()?.to_string(),
            name: v["name"].as_str().unwrap_or("file").to_string(),
            size: v["size"].as_u64().unwrap_or(0),
            sha256: v["sha256"].as_str().unwrap_or("").to_string(),
            dir: v["dir"].as_bool().unwrap_or(false),
            from: sender.to_string(),
        })),
        "accept" => Some(Ft::Accept(v["id"].as_str()?.to_string())),
        "reject" => Some(Ft::Reject(v["id"].as_str()?.to_string())),
        "chunk" => Some(Ft::Chunk {
            id: v["id"].as_str()?.to_string(),
            data: STANDARD.decode(v["data"].as_str()?).ok()?,
        }),
        "done" => Some(Ft::Done(v["id"].as_str()?.to_string())),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn file_payload_roundtrip() {
        let dir = std::env::temp_dir().join(format!("hh-ft-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let src = dir.join("note.txt");
        std::fs::write(&src, b"offering to the coven").unwrap();

        let (name, bytes, is_dir) = read_payload(src.to_str().unwrap()).unwrap();
        assert_eq!(name, "note.txt");
        assert!(!is_dir);
        let offer = Offer { id: "1".into(), name, size: bytes.len() as u64,
            sha256: sha256_hex(&bytes), dir: false, from: "x".into() };
        let dl = dir.join("dl");
        let out = save(&dl, &offer, &bytes).unwrap();
        assert_eq!(std::fs::read(&out).unwrap(), b"offering to the coven");
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn dir_tar_roundtrip() {
        let dir = std::env::temp_dir().join(format!("hh-ftd-{}", std::process::id()));
        let proj = dir.join("proj");
        std::fs::create_dir_all(proj.join("sub")).unwrap();
        std::fs::write(proj.join("a.txt"), b"AAA").unwrap();
        std::fs::write(proj.join("sub/b.txt"), b"BBB").unwrap();

        let (name, bytes, is_dir) = read_payload(proj.to_str().unwrap()).unwrap();
        assert_eq!(name, "proj.tar");
        assert!(is_dir);
        let offer = Offer { id: "1".into(), name, size: bytes.len() as u64,
            sha256: sha256_hex(&bytes), dir: true, from: "x".into() };
        let dl = dir.join("dl");
        let out = save(&dl, &offer, &bytes).unwrap(); // -> dl/proj
        assert!(out.ends_with("proj"));
        assert_eq!(std::fs::read(out.join("a.txt")).unwrap(), b"AAA");
        assert_eq!(std::fs::read(out.join("sub/b.txt")).unwrap(), b"BBB");
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn traversal_guard_blocks_escapes() {
        // The zip-slip guard: relative-only, no `..` escape, no absolute paths.
        assert!(!safe_entry(Path::new("../escape.txt")));
        assert!(!safe_entry(Path::new("a/../../etc/passwd")));
        assert!(!safe_entry(Path::new("/etc/passwd")));
        assert!(safe_entry(Path::new("proj/sub/ok.txt")));
        assert!(safe_entry(Path::new("file.txt")));
    }
}
