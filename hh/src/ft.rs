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
use std::io::{Read, Write};
use std::path::{Component, Path, PathBuf};

/// In-memory ceiling — used only by `tar_path` (sandbox injection), which builds
/// the archive in RAM. Streamed `/send` transfers use `STREAM_MAX` instead.
pub const MAX_SIZE: usize = 50 * 1024 * 1024;
/// Streamed-transfer ceiling (disk-to-disk). Far larger than `MAX_SIZE` because
/// `/send` writes chunks straight to a temp file and reads them back the same
/// way — memory stays flat regardless of size. This only guards against a sender
/// filling the receiver's disk (e.g. a multi-GB VM appliance is fine).
pub const STREAM_MAX: u64 = 16 * 1024 * 1024 * 1024; // 16 GiB
pub const CHUNK: usize = 64 * 1024;

#[derive(Clone)]
pub struct Offer {
    pub id: String,
    pub name: String,
    pub size: u64,
    pub sha256: String,
    pub dir: bool,
    pub from: String,
    /// Base64 Ed25519 persona public key of the sender (attribution). Absent on
    /// legacy/Python senders that don't sign — wire-compatible either way.
    pub persona: Option<String>,
    /// Base64 detached signature over `persona::attest_msg(sha256, name, size)`.
    pub sig: Option<String>,
    /// Optional ESA-style attribution commitment `SHA-512(passphrase || sha256)`
    /// the sender can later open by revealing the passphrase.
    pub attrib: Option<String>,
    /// Direct-send recipient: `Some(username)` means only that member should be
    /// prompted; `None` (or absent/empty on the wire) means the whole room. The
    /// relay still broadcasts to everyone, so this is an advisory app-layer
    /// filter — non-recipients drop the offer instead of prompting.
    pub to: Option<String>,
}

pub enum Ft {
    Offer(Offer),
    Accept(String),
    Reject(String),
    Chunk { id: String, data: Vec<u8> },
    Done(String),
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

/// What to stream for an outgoing `/send`: a single file on disk — the original
/// file, or a temp `.tar` we built for a directory. The sender reads `src` in
/// `CHUNK` blocks, so memory stays flat no matter how large the payload is.
pub struct SendSrc {
    pub name: String,
    pub src: PathBuf,
    /// `src` is a temp tar we created and should delete once sending finishes.
    pub temp: bool,
    pub size: u64,
    pub sha256: String,
    pub dir: bool,
}

/// Prepare a path for streamed sending: stat + stream-hash a file directly, or
/// tar a directory to a temp file first (so even large trees stream from disk).
/// Enforces `STREAM_MAX`. This walks the bytes once to hash — call it off the UI
/// thread for large payloads.
pub fn prepare_send(path: &str) -> Result<SendSrc> {
    let p = Path::new(path);
    let meta = std::fs::metadata(p).with_context(|| format!("not found: {path}"))?;
    if meta.is_dir() {
        let base = p
            .file_name()
            .and_then(|s| s.to_str())
            .unwrap_or("dir")
            .to_string();
        let tmp = std::env::temp_dir().join(format!(
            "hh-send-{}-{}.tar",
            std::process::id(),
            sanitize(&base)
        ));
        tar_to_file(p, &tmp)?;
        let (size, sha256) = hash_file(&tmp)?;
        Ok(SendSrc {
            name: format!("{base}.tar"),
            src: tmp,
            temp: true,
            size,
            sha256,
            dir: true,
        })
    } else {
        let (size, sha256) = hash_file(p)?;
        let name = p
            .file_name()
            .and_then(|s| s.to_str())
            .unwrap_or("file")
            .to_string();
        Ok(SendSrc {
            name,
            src: p.to_path_buf(),
            temp: false,
            size,
            sha256,
            dir: false,
        })
    }
}

/// Tar a directory to a file on disk (constant memory, unlike `tar_path`).
fn tar_to_file(dir: &Path, out: &Path) -> Result<()> {
    let f = std::fs::File::create(out).with_context(|| format!("create {}", out.display()))?;
    let mut tb = tar::Builder::new(std::io::BufWriter::new(f));
    let base = dir.file_name().unwrap_or_default();
    tb.append_dir_all(base, dir).context("tar directory")?;
    tb.finish()?;
    Ok(())
}

/// Stream-hash a file → `(size, sha256-hex)`, enforcing `STREAM_MAX`. Reads in
/// `CHUNK` blocks so memory stays flat.
fn hash_file(path: &Path) -> Result<(u64, String)> {
    let mut f = std::fs::File::open(path).with_context(|| format!("open {}", path.display()))?;
    let mut hasher = Sha256::new();
    let mut buf = vec![0u8; CHUNK];
    let mut total: u64 = 0;
    loop {
        let n = f.read(&mut buf)?;
        if n == 0 {
            break;
        }
        total += n as u64;
        anyhow::ensure!(
            total <= STREAM_MAX,
            "too large (max {})",
            human(STREAM_MAX as usize)
        );
        hasher.update(&buf[..n]);
    }
    Ok((total, hex::encode(hasher.finalize())))
}

/// Filesystem-safe slug for temp-file names (transfer ids, dir basenames).
fn sanitize(s: &str) -> String {
    s.chars()
        .map(|c| if c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-') { c } else { '_' })
        .collect()
}

/// Tar any local path (file or directory) to bytes for injecting into a
/// sandbox — the archive's single top-level entry is named after the path's
/// basename, so it extracts as `<dest>/<base>`. Enforces `MAX_SIZE`. Returns
/// `(base_name, tar_bytes)`.
pub fn tar_path(path: &Path) -> Result<(String, Vec<u8>)> {
    let meta = std::fs::metadata(path).with_context(|| format!("not found: {}", path.display()))?;
    let base = path
        .file_name()
        .and_then(|s| s.to_str())
        .context("path has no final component")?
        .to_string();
    let mut buf = Vec::new();
    {
        let mut tb = tar::Builder::new(&mut buf);
        if meta.is_dir() {
            tb.append_dir_all(&base, path).context("tar directory")?;
        } else {
            tb.append_path_with_name(path, &base).context("tar file")?;
        }
        tb.finish()?;
    }
    anyhow::ensure!(buf.len() <= MAX_SIZE, "too large ({})", human(buf.len()));
    Ok((base, buf))
}

/// Disk-backed receiver: incoming chunks are written straight to a temp `.part`
/// file under `downloads/` and hashed as they arrive, so a multi-GB transfer
/// never sits in RAM. `finish` returns the temp path + computed digest for
/// verification; `commit` then moves/extracts it into place.
pub struct Sink {
    file: std::fs::File,
    path: PathBuf,
    hasher: Sha256,
    written: u64,
}

impl Sink {
    /// Create the temp file under `downloads/` (same filesystem as the final
    /// destination → atomic rename on commit). Named from the transfer id.
    pub fn create(downloads: &Path, id: &str) -> Result<Self> {
        std::fs::create_dir_all(downloads)?;
        let path = downloads.join(format!(".hh-{}.part", sanitize(id)));
        let file =
            std::fs::File::create(&path).with_context(|| format!("create {}", path.display()))?;
        Ok(Self {
            file,
            path,
            hasher: Sha256::new(),
            written: 0,
        })
    }

    pub fn write(&mut self, data: &[u8]) -> Result<()> {
        self.file.write_all(data)?;
        self.hasher.update(data);
        self.written += data.len() as u64;
        Ok(())
    }

    pub fn written(&self) -> u64 {
        self.written
    }

    /// Flush and return `(temp_path, sha256-hex)`.
    pub fn finish(mut self) -> Result<(PathBuf, String)> {
        self.file.flush()?;
        let sha = hex::encode(self.hasher.finalize());
        Ok((self.path, sha))
    }

    /// Best-effort temp-file cleanup (reject / overflow / write error).
    pub fn abort(self) {
        drop(self.file);
        let _ = std::fs::remove_file(&self.path);
    }
}

/// Finalize a verified streamed transfer sitting at `tmp` into `downloads/`:
/// extract a directory tar (zip-slip guarded) or move the file to a unique name.
/// Consumes `tmp` (renamed away or removed).
pub fn commit(downloads: &Path, offer: &Offer, tmp: &Path) -> Result<PathBuf> {
    std::fs::create_dir_all(downloads)?;
    if offer.dir {
        // Extract the tar's own top-level dir directly under downloads/.
        let f = std::fs::File::open(tmp).with_context(|| format!("open {}", tmp.display()))?;
        let mut ar = tar::Archive::new(std::io::BufReader::new(f));
        let mut top: Option<std::ffi::OsString> = None;
        for entry in ar.entries()? {
            let mut e = entry?;
            let path = e.path()?.into_owned();
            // Explicit zip-slip guard (belt-and-suspenders; unpack_in also refuses).
            anyhow::ensure!(
                safe_entry(&path),
                "unsafe tar entry rejected: {}",
                path.display()
            );
            if top.is_none() {
                top = path.components().next().map(|c| c.as_os_str().to_owned());
            }
            e.unpack_in(downloads)
                .with_context(|| format!("extract {}", path.display()))?;
        }
        let _ = std::fs::remove_file(tmp);
        Ok(top
            .map(|t| downloads.join(t))
            .unwrap_or_else(|| downloads.to_path_buf()))
    } else {
        let stem = Path::new(&offer.name)
            .file_stem()
            .and_then(|s| s.to_str())
            .unwrap_or("file");
        let ext = Path::new(&offer.name)
            .extension()
            .and_then(|s| s.to_str())
            .unwrap_or("");
        let dest = unique(downloads, stem, ext);
        // Same-filesystem rename; fall back to copy for a cross-device temp dir.
        if std::fs::rename(tmp, &dest).is_err() {
            std::fs::copy(tmp, &dest)?;
            let _ = std::fs::remove_file(tmp);
        }
        Ok(dest)
    }
}

/// A tar entry path is safe to extract iff it's relative and has no `..` escape.
fn safe_entry(path: &Path) -> bool {
    !path.is_absolute() && !path.components().any(|c| matches!(c, Component::ParentDir))
}

fn unique(dir: &Path, stem: &str, ext: &str) -> PathBuf {
    let mk = |n: usize| {
        let base = if n == 0 {
            stem.to_string()
        } else {
            format!("{stem}_{n}")
        };
        if ext.is_empty() {
            dir.join(base)
        } else {
            dir.join(format!("{base}.{ext}"))
        }
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
            persona: v["persona"].as_str().map(String::from),
            sig: v["sig"].as_str().map(String::from),
            attrib: v["attrib"].as_str().map(String::from),
            to: match v["to"].as_str() {
                Some(s) if !s.is_empty() => Some(s.to_string()),
                _ => None,
            },
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

    /// Drive a full streamed transfer: prepare the source, feed `src` through a
    /// disk-backed `Sink` in `CHUNK` blocks (as the wire would), verify the hash,
    /// then commit into `downloads/`. Returns the committed path.
    fn stream_roundtrip(downloads: &Path, src: &SendSrc, id: &str) -> PathBuf {
        let mut sink = Sink::create(downloads, id).unwrap();
        let mut f = std::fs::File::open(&src.src).unwrap();
        let mut buf = vec![0u8; CHUNK];
        loop {
            let n = f.read(&mut buf).unwrap();
            if n == 0 {
                break;
            }
            sink.write(&buf[..n]).unwrap();
        }
        let offer = Offer {
            id: id.into(),
            name: src.name.clone(),
            size: src.size,
            sha256: src.sha256.clone(),
            dir: src.dir,
            from: "x".into(),
            persona: None,
            sig: None,
            attrib: None,
            to: None,
        };
        let (tmp, sha) = sink.finish().unwrap();
        assert_eq!(sha, offer.sha256, "streamed hash matches the offer");
        commit(downloads, &offer, &tmp).unwrap()
    }

    #[test]
    fn file_payload_roundtrip() {
        let dir = std::env::temp_dir().join(format!("hh-ft-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let src = dir.join("note.txt");
        std::fs::write(&src, b"offering to the clergy").unwrap();

        let prepared = prepare_send(src.to_str().unwrap()).unwrap();
        assert_eq!(prepared.name, "note.txt");
        assert!(!prepared.dir);
        let dl = dir.join("dl");
        let out = stream_roundtrip(&dl, &prepared, "1");
        assert_eq!(std::fs::read(&out).unwrap(), b"offering to the clergy");
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn dir_tar_roundtrip() {
        let dir = std::env::temp_dir().join(format!("hh-ftd-{}", std::process::id()));
        let proj = dir.join("proj");
        std::fs::create_dir_all(proj.join("sub")).unwrap();
        std::fs::write(proj.join("a.txt"), b"AAA").unwrap();
        std::fs::write(proj.join("sub/b.txt"), b"BBB").unwrap();

        let prepared = prepare_send(proj.to_str().unwrap()).unwrap();
        assert_eq!(prepared.name, "proj.tar");
        assert!(prepared.dir);
        let dl = dir.join("dl");
        let out = stream_roundtrip(&dl, &prepared, "1"); // -> dl/proj
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
