//! Pseudonymous attribution — a persistent Ed25519 "persona" key that signs the
//! files you share, plus an optional revealable attribution commitment.
//!
//! Modeled on Princess_Pi's *Encrypt-Share-Attribution* (Church of Malware codex):
//! prove authorship two independent ways without ever binding to a real identity —
//!   1. an **Ed25519 signature** over the file's content hash (automatic), and
//!   2. a later **passphrase reveal** matching a `SHA-512(passphrase || sha256)`
//!      commitment (opt-in via `--attest`).
//! The private key persists at `~/.config/hack-house/persona_ed25519`, so the same
//! pseudonym signs across sessions: peers can link "the same author" and verify
//! integrity, while the server (and even peers) never learn who that author is.

use base64::engine::general_purpose::STANDARD;
use base64::Engine;
use ed25519_dalek::{Signature, Signer, SigningKey, Verifier, VerifyingKey};
use sha2::{Digest, Sha256, Sha512};
use std::path::{Path, PathBuf};

/// A long-lived signing identity (the seed is 32 bytes on disk, 0600).
pub struct Persona {
    signing: SigningKey,
}

impl Persona {
    /// Load the persisted key, or mint + persist a new one. Never fails: if the
    /// config dir is unreadable/unwritable we fall back to an ephemeral in-memory
    /// key so signing still works for this session.
    pub fn load_or_create() -> Self {
        if let Some(path) = key_path() {
            if let Ok(bytes) = std::fs::read(&path) {
                if let Ok(seed) = <[u8; 32]>::try_from(bytes.as_slice()) {
                    return Self {
                        signing: SigningKey::from_bytes(&seed),
                    };
                }
            }
            let signing = gen();
            if let Some(dir) = path.parent() {
                let _ = std::fs::create_dir_all(dir);
            }
            if std::fs::write(&path, signing.to_bytes()).is_ok() {
                harden(&path);
            }
            return Self { signing };
        }
        Self { signing: gen() }
    }

    /// Base64 of the 32-byte Ed25519 public key — shipped in each offer frame.
    pub fn pub_b64(&self) -> String {
        STANDARD.encode(self.signing.verifying_key().to_bytes())
    }

    /// Base64 detached signature over `msg`.
    pub fn sign_b64(&self, msg: &[u8]) -> String {
        STANDARD.encode(self.signing.sign(msg).to_bytes())
    }

    /// Short human tag for this persona (sha256 of the pubkey, first 4 bytes hex).
    /// Handy for a future roster badge; peers currently render `fingerprint_of`
    /// the incoming pubkey directly.
    #[allow(dead_code)]
    pub fn fingerprint(&self) -> String {
        fingerprint_of(&self.pub_b64()).unwrap_or_else(|| "unknown".into())
    }
}

fn gen() -> SigningKey {
    let mut seed = [0u8; 32];
    rand::RngCore::fill_bytes(&mut rand::thread_rng(), &mut seed);
    SigningKey::from_bytes(&seed)
}

#[cfg(unix)]
fn harden(path: &Path) {
    use std::os::unix::fs::PermissionsExt;
    let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600));
}
#[cfg(not(unix))]
fn harden(_path: &Path) {}

fn key_path() -> Option<PathBuf> {
    let home = std::env::var_os("HOME")?;
    Some(
        PathBuf::from(home)
            .join(".config")
            .join("hack-house")
            .join("persona_ed25519"),
    )
}

/// Canonical bytes signed for a file offer — binds the content hash, name, and
/// size so a signature can't be lifted onto a different file.
pub fn attest_msg(sha256_hex: &str, name: &str, size: u64) -> Vec<u8> {
    format!("hh-attest-v1\n{sha256_hex}\n{name}\n{size}").into_bytes()
}

/// Short fingerprint tag from a base64 pubkey (sha256 → first 4 bytes hex).
pub fn fingerprint_of(pub_b64: &str) -> Option<String> {
    let raw = STANDARD.decode(pub_b64).ok()?;
    let d = Sha256::digest(&raw);
    Some(hex::encode(&d[..4]))
}

/// Verify an offer signature. Returns false on any malformed input.
pub fn verify(pub_b64: &str, sig_b64: &str, msg: &[u8]) -> bool {
    let inner = || -> Option<bool> {
        let pk_raw = STANDARD.decode(pub_b64).ok()?;
        let pk = VerifyingKey::from_bytes(&<[u8; 32]>::try_from(pk_raw.as_slice()).ok()?).ok()?;
        let sig_raw = STANDARD.decode(sig_b64).ok()?;
        let sig = Signature::from_bytes(&<[u8; 64]>::try_from(sig_raw.as_slice()).ok()?);
        Some(pk.verify(msg, &sig).is_ok())
    };
    inner().unwrap_or(false)
}

/// Attribution commitment, ESA-style: `SHA-512(passphrase || sha256_hex)`. The
/// author can later reveal the passphrase; anyone recomputes this against the
/// (signed) content hash to confirm authorship.
pub fn commitment(passphrase: &str, sha256_hex: &str) -> String {
    let mut h = Sha512::new();
    h.update(passphrase.as_bytes());
    h.update(sha256_hex.as_bytes());
    hex::encode(h.finalize())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sign_verify_roundtrip() {
        let p = Persona { signing: gen() };
        let msg = attest_msg("deadbeef", "note.txt", 42);
        let sig = p.sign_b64(&msg);
        assert!(verify(&p.pub_b64(), &sig, &msg), "valid signature verifies");
        // Tampering with any bound field breaks verification.
        let bad = attest_msg("deadbeef", "note.txt", 43);
        assert!(!verify(&p.pub_b64(), &sig, &bad), "size tamper rejected");
        assert!(!verify(&p.pub_b64(), "AAAA", &msg), "garbage sig rejected");
    }

    #[test]
    fn commitment_reveal() {
        let c = commitment("correct horse battery staple pony", "abc123");
        assert_eq!(c, commitment("correct horse battery staple pony", "abc123"));
        assert_ne!(c, commitment("wrong passphrase", "abc123"));
        assert_eq!(c.len(), 128, "sha512 hex");
    }

    #[test]
    fn fingerprint_is_stable_and_short() {
        let p = Persona { signing: gen() };
        let fp = p.fingerprint();
        assert_eq!(fp.len(), 8, "4 bytes → 8 hex chars");
        assert_eq!(Some(fp), fingerprint_of(&p.pub_b64()));
    }
}
