"""Channel sub-keys for the web relay (B6).

`K_web` — the 32 bytes that ride in the share link's `#k` fragment — used to be
handed straight to AES-GCM for every direction at once: terminal output, browser
input, chat, file transfer. That is one key doing four jobs, which means a nonce
that repeats across two of those channels is a catastrophic GCM failure rather
than a merely embarrassing one, and it means a bug in any one channel's framing
is a bug in all four.

So `K_web` is now a *master* secret and nothing encrypts under it directly. Each
channel gets its own AES-256 key via HKDF-SHA256 with a distinct `info` label,
which is exactly what WebCrypto's `deriveKey` gives the page for free — the
browser side of this file is the `hkdf()` helper in `room.html`, and the two must
agree byte-for-byte or nothing decrypts.

Salt is empty (HKDF then behaves as if salted with `hash_len` zero bytes), which
is what `crypto.subtle.deriveKey` does with a zero-length salt. There is nothing
for a salt to buy here: `K_web` is already 32 uniformly random bytes, and both
parties would have to be told the salt anyway.
"""

from __future__ import annotations

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# One label per direction/purpose. Adding a channel means adding a label here and
# in `room.html` — never reusing a neighbour's.
CHANNELS = ("out", "in", "chat", "file")

# `#k` shorter than this is rejected outright. The docs promise AES-256, and a
# short fragment used to be silently accepted and stretched by nobody — the page
# would happily run AES-128 while claiming otherwise.
K_WEB_LEN = 32


def derive_channel_key(k_web: bytes, channel: str) -> bytes:
    if len(k_web) < K_WEB_LEN:
        raise ValueError(
            f"K_web must be {K_WEB_LEN} bytes, got {len(k_web)}")
    if channel not in CHANNELS:
        raise ValueError(f"unknown channel {channel!r}")
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=b"",
                info=f"hh/{channel}".encode()).derive(bytes(k_web))


def derive_web_keys(k_web: bytes) -> dict[str, AESGCM]:
    """`K_web` → one ready-to-use AESGCM per channel."""
    return {c: AESGCM(derive_channel_key(k_web, c)) for c in CHANNELS}
