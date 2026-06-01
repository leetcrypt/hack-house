//! SRP-6a + room-key crypto, byte-for-byte compatible with the Python
//! `srp` library (NG_2048, SHA-256, `rfc5054_enable()`) and `cryptography`
//! HKDF→Fernet as used by the Sanic server / reference client.
//!
//! Reference (pysrp `_pysrp.py`):
//!   x   = SHA256( salt || SHA256(I || ":" || P) )
//!   k   = SHA256( PAD(N) || PAD(g) )                 (rfc5054: PAD to len(N))
//!   A   = g^a mod N
//!   u   = SHA256( PAD(A) || PAD(B) )
//!   S   = (B - k*g^x)^(a + u*x) mod N
//!   K   = SHA256( S )
//!   M   = SHA256( (H(N) xor H(PAD(g))) || SHA256(I) || salt || A || B || K )
//!   HAMK= SHA256( A || M || K )
//! Note: A and B inside M / HAMK use *minimal* big-endian bytes (no padding);
//! only k and u pad to len(N) (= 256 bytes for NG_2048).

use num_bigint::BigUint;
use num_traits::Zero;
use sha2::{Digest, Sha256};

/// RFC 5054 / pysrp NG_2048 safe prime.
const N_HEX: &str = "\
AC6BDB41324A9A9BF166DE5E1389582FAF72B6651987EE07FC3192943DB56050A37329CBB4\
A099ED8193E0757767A13DD52312AB4B03310DCD7F48A9DA04FD50E8083969EDB767B0CF60\
95179A163AB3661A05FBD5FAAAE82918A9962F0B93B855F97993EC975EEAA80D740ADBF4FF\
747359D041D5C33EA71D281E446B14773BCA97B43A23FB801676BD207A436C6481F1D2B907\
8717461A5B9D32E688F87748544523B524B0D57D5EA77A2775D2ECFA032CFBDBF52FB37861\
60279004E57AE6AF874E7303CE53299CCC041C7BC308D82A5698F3A8D0C38271AE35F8E9DB\
FBB694B5C803D89F7AE435DE236D525F54759B65E372FCD68EF20FA7111F9E4AFF73";

/// The SRP identity used by every cmd-chat / clergy room (server hardcodes this).
/// The user's chosen display name is independent of this value.
pub const SRP_IDENTITY: &[u8] = b"chat";

fn n() -> BigUint {
    BigUint::parse_bytes(N_HEX.as_bytes(), 16).expect("valid N")
}
fn g() -> BigUint {
    BigUint::from(2u32)
}

fn sha256(parts: &[&[u8]]) -> Vec<u8> {
    let mut h = Sha256::new();
    for p in parts {
        h.update(p);
    }
    h.finalize().to_vec()
}

/// Left-pad `b` with zero bytes to exactly `width` bytes.
fn pad(b: &[u8], width: usize) -> Vec<u8> {
    if b.len() >= width {
        return b.to_vec();
    }
    let mut out = vec![0u8; width - b.len()];
    out.extend_from_slice(b);
    out
}

fn bytes_to_long(b: &[u8]) -> BigUint {
    BigUint::from_bytes_be(b)
}

/// pysrp `long_to_bytes`: minimal big-endian, empty for zero.
fn long_to_bytes(x: &BigUint) -> Vec<u8> {
    if x.is_zero() {
        return Vec::new();
    }
    x.to_bytes_be()
}

/// Multiplier k = SHA256(PAD(N) || PAD(g)), padded to len(N).
fn compute_k(n: &BigUint) -> BigUint {
    let width = long_to_bytes(n).len();
    let nb = pad(&long_to_bytes(n), width);
    let gb = pad(&long_to_bytes(&g()), width);
    bytes_to_long(&sha256(&[&nb, &gb]))
}

/// x = SHA256(salt || SHA256(I || ":" || P)).
fn gen_x(salt: &[u8], username: &[u8], password: &[u8]) -> BigUint {
    let inner = sha256(&[username, b":", password]);
    bytes_to_long(&sha256(&[salt, &inner]))
}

/// (H(N) xor H(PAD(g))) used inside M.
fn hn_xor_g(n: &BigUint) -> Vec<u8> {
    let width = long_to_bytes(n).len();
    let h_n = sha256(&[&long_to_bytes(n)]);
    let h_g = sha256(&[&pad(&long_to_bytes(&g()), width)]);
    h_n.iter().zip(h_g.iter()).map(|(a, b)| a ^ b).collect()
}

/// Client-side SRP-6a state.
pub struct SrpClient {
    username: Vec<u8>,
    password: Vec<u8>,
    n: BigUint,
    k: BigUint,
    a: BigUint,
    pub a_pub: BigUint, // A
}

impl SrpClient {
    /// New client with a random 256-byte ephemeral `a` (high bit set, per pysrp).
    pub fn new(username: &[u8], password: &[u8]) -> Self {
        let mut buf = [0u8; 256];
        rand::RngCore::fill_bytes(&mut rand::thread_rng(), &mut buf);
        buf[0] |= 0x80;
        Self::with_a(username, password, &buf)
    }

    /// Deterministic constructor for test vectors.
    pub fn with_a(username: &[u8], password: &[u8], a_bytes: &[u8]) -> Self {
        let n = n();
        let k = compute_k(&n);
        let a = bytes_to_long(a_bytes);
        let a_pub = g().modpow(&a, &n);
        Self {
            username: username.to_vec(),
            password: password.to_vec(),
            n,
            k,
            a,
            a_pub,
        }
    }

    /// Wire bytes for A (minimal big-endian).
    pub fn a_bytes(&self) -> Vec<u8> {
        long_to_bytes(&self.a_pub)
    }

    /// Process the server challenge (salt, B). Returns (M, K, H_AMK_expected).
    /// `M` is sent to the server; `h_amk` is compared to the server's reply.
    pub fn process_challenge(&self, salt: &[u8], b_bytes: &[u8]) -> anyhow::Result<Challenge> {
        let n = &self.n;
        let width = long_to_bytes(n).len();
        let big_b = bytes_to_long(b_bytes);
        if (&big_b % n).is_zero() {
            anyhow::bail!("SRP safety check failed: B mod N == 0");
        }

        let a_min = long_to_bytes(&self.a_pub);
        let b_min = long_to_bytes(&big_b);
        let u = bytes_to_long(&sha256(&[&pad(&a_min, width), &pad(&b_min, width)]));
        if u.is_zero() {
            anyhow::bail!("SRP safety check failed: u == 0");
        }

        let x = gen_x(salt, &self.username, &self.password);
        let v = g().modpow(&x, n);

        // base = (B - k*v) mod N, kept non-negative.
        let kv = (&self.k * &v) % n;
        let base = ((&big_b % n) + n - kv) % n;
        let exp = &self.a + &u * &x;
        let s = base.modpow(&exp, n);

        let k_key = sha256(&[&long_to_bytes(&s)]);

        let m = sha256(&[
            &hn_xor_g(n),
            &sha256(&[&self.username]),
            salt,
            &a_min,
            &b_min,
            &k_key,
        ]);
        let h_amk = sha256(&[&a_min, &m, &k_key]);

        Ok(Challenge {
            m,
            session_key: k_key,
            h_amk,
        })
    }
}

pub struct Challenge {
    pub m: Vec<u8>,
    pub session_key: Vec<u8>,
    pub h_amk: Vec<u8>,
}

// ── Room key: HKDF-SHA256(password, salt=room_salt, info) → Fernet ──────────

/// Derive the shared room Fernet key exactly as the reference client:
/// `Fernet(urlsafe_b64( HKDF(SHA256, 32, room_salt, "cmd-chat-room-key")(pw) ))`.
pub fn room_fernet(password: &[u8], room_salt: &[u8]) -> anyhow::Result<fernet::Fernet> {
    use base64::Engine;
    let hk = hkdf::Hkdf::<Sha256>::new(Some(room_salt), password);
    let mut okm = [0u8; 32];
    hk.expand(b"cmd-chat-room-key", &mut okm)
        .map_err(|_| anyhow::anyhow!("hkdf expand failed"))?;
    let key_b64 = base64::engine::general_purpose::URL_SAFE.encode(okm);
    fernet::Fernet::new(&key_b64).ok_or_else(|| anyhow::anyhow!("invalid fernet key"))
}

#[cfg(test)]
mod tests {
    use super::*;

    // Golden vectors generated from the live Python `srp` (_ctsrp backend),
    // rfc5054 enabled, NG_2048, SHA-256. See tools/gen_vectors.py.
    const PW: &[u8] = b"labtest";
    const USER: &[u8] = b"chat";
    const SALT_HEX: &str = "0a1b2c3d";
    const A_HEX: &str = "8613d4e3da583215e770e4de20622d664374d237a96aabdebe1e38ae34b2d0bc45da3251d9f76337f918bbfa49a52aaf4a6d5f141aadc82f73f7559a3c0859c733d4cb258e9fdd797a3c1be8f71a0f5db0a9d15e19b5af82c408513d512c1824c3f61f3099b93bc9cf8c8bcdbd8f87ec6a347bb81bf5027a30b9ce6eb6beb110efc734164f65d4fc08ff7da2ef19732f559c07197c5a166b52c27a9806f9776b6b88c79739f6a1e024b2d3856f4fc7e69b39548f02a599e178fcb9b6a574a13964ab0331a40b839810e27d5a9bd71f9bacdf1ed26bdc4baaaa0088ecfa1d2daae7f47b6d67e5480d57e97770bbb623177f92080b0e963097fa72ef9f6ded07f0";
    const B_HEX: &str = "047426a55963c70bc385c6a51f6e9dc0bfe5e16b0d1fee4f566fb54b60fa77144f15ed1ee6ade007bd92f2b90846e1ee083ab4290239420606f48a1d861f759543d7856cbce21fd7fec98c9961a66610b412fea2efc5be78f35b18fd48176ac80c3a1cbefacac81e25e7da8079fac4012d01c47d85b783c2ea7340819bfe73d29cd0953d47c8fade77caa5459fb77d88fb918c073a77c495fa884859142a270cb0b1668de06131b150df4dbc931953a381710b7fdb98a953d6f77a4bba847c4c62c15cca8e514dc13f531427966a553c461aa4ab0caec9665612861fef03d48676e5f6551fc8ca4317f3118e0294c949bd2f5821e5900e7f695225dafa0ba2d2";
    const M_HEX: &str = "6e733ba88eb86c52e3be89207d2815a65b4dea8116f668af5de1b66ce1f047dd";
    const HAMK_HEX: &str = "649a7d46bb9210483e0489b7f9e6fb300a6cddd6381b018fa81770076169a837";
    const K_HEX: &str = "a12218af3fda651aa3c094a4db474a5eee919496c3ae8d38a4f6be1104ed4928";

    fn a_bytes() -> Vec<u8> {
        let mut v = vec![0x80u8];
        v.extend(std::iter::repeat_n(0x22u8, 31));
        v
    }

    #[test]
    fn srp_matches_pysrp_vectors() {
        let c = SrpClient::with_a(USER, PW, &a_bytes());
        assert_eq!(hex::encode(c.a_bytes()), A_HEX, "A mismatch");

        let salt = hex::decode(SALT_HEX).unwrap();
        let b = hex::decode(B_HEX).unwrap();
        let ch = c.process_challenge(&salt, &b).unwrap();
        assert_eq!(hex::encode(&ch.session_key), K_HEX, "K mismatch");
        assert_eq!(hex::encode(&ch.m), M_HEX, "M mismatch");
        assert_eq!(hex::encode(&ch.h_amk), HAMK_HEX, "H_AMK mismatch");
    }
}

#[cfg(test)]
mod fernet_interop {
    // Token produced by Python `cryptography` Fernet with key = urlsafe_b64(0x42*32).
    const KEY: &str = "QkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkI=";
    const TOK: &str = "gAAAAABqG0p-31PhpUCwVaYKIXTq2NIf5N8nNRsIzvaO4BZL9xUEBgBfeiKb2hY-lQdP4nxSpNrhs2RmLpMVNfPozMNrxjomGFSbgrIipevHdOtFelEQNE4=";

    #[test]
    fn rust_decrypts_python_fernet() {
        let f = fernet::Fernet::new(KEY).unwrap();
        let pt = f
            .decrypt(TOK)
            .expect("rust must decrypt python fernet token");
        assert_eq!(pt, b"hello from python fernet");
    }
}
