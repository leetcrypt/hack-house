# Pseudonymous attribution

hack-house lets you share files **pseudonymously but provably** — a recipient can
verify *who* authored a file (and that it's intact) without anyone, including the
relay server, learning your real identity. It adapts Princess_Pi's
**Encrypt-Share-Attribution** scheme from the Church of Malware codex
(<https://git.thecoven.info/PrincessPi/Encrypt-Share-Attribution>).

There are two independent ways to prove authorship, mirroring ESA:

1. **Persona signature (automatic).** On first run each client mints a long-lived
   Ed25519 "persona" key at `~/.config/hack-house/persona_ed25519` (0600). Every
   `/send` / `/sendroom` offer carries the persona public key and a detached
   signature over `attest-v1 || sha256 || name || size`. Receivers verify it and
   see the persona **fingerprint** (`⛧<8 hex>`), so the same author is recognizable
   across offers. The fields are additive JSON — a Python peer that doesn't sign
   still interoperates (its offers just show as *unsigned*).

2. **Attribution passphrase (opt-in).** Add `--attest <passphrase>` to a send:
   the offer then carries a commitment `SHA-512(passphrase || sha256)`. You can
   *later* reveal the passphrase to prove authorship to anyone, even people who
   weren't in the room.

## Commands

```
/send <user> <path> [--attest <passphrase>]
/sendroom <path>     [--attest <passphrase>]
/export-signed <dir> [--attest <passphrase>]
```

`/export-signed` packages a directory into a **portable, self-verifying ESA 7z
archive** (`verifiable_archive_<ts>.7z`) using Princess_Pi's exact format: a fresh
per-round Ed25519 key signs an inner `contents.7z`, SHA-512 checksums cover the
outer layer, and bundled `verify-everything.sh` / `test_validate_passphrase.sh`
let anyone with `bash` + `7z` + `ssh-keygen` verify it — no hack-house needed. The
builder is `hh/tools/esa/esa_build.sh` (embedded in the binary). Requires `7z`,
`ssh-keygen`, `sha512sum`, `shred`, `openssl` on the host.
