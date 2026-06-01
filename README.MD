<div align="center">

# hack-house

### encrypted collaborative terminal sessions with a summoned sandbox

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Rust](https://img.shields.io/badge/client-rust-orange.svg)](https://www.rust-lang.org/)
[![Python 3.10+](https://img.shields.io/badge/server-python%203.10+-blue.svg)](https://www.python.org/downloads/)

</div>

---

**hack-house** is a multi-user, end-to-end-encrypted terminal session. A small
crew shares an encrypted chat room and, when summoned, a disposable sandboxed
Linux box they drive together — with real Linux users, an owner who delegates
the keys, and file transfer in between.

The server never sees plaintext. Messages, files, and terminal output are all
relayed as opaque ciphertext. Close the window and the house empties — nothing
is written to disk on the server.

> hack-house is the evolution of **cmd-chat**. The Python (Sanic) server is the
> same proven zero-knowledge relay; the flagship client is now a Rust
> [`ratatui`](https://ratatui.rs) TUI. SRP + HKDF→Fernet are byte-for-byte
> compatible across both, so the original Python client still interoperates.

## Features

- **End-to-end encrypted** — Fernet (AES-128-CBC + HMAC), encrypted client-side before anything leaves your machine
- **SRP authentication** — the password is never sent over the network (zero-knowledge proof)
- **Zero-knowledge server** — relays only ciphertext; cannot read messages, files, or terminal output
- **RAM only** — nothing persisted on the server; close it and history is gone
- **Shared sandbox** — summon a disposable `local` / `docker` / `multipass` box the whole room can watch and drive
- **Real permissions** — the sandbox owner grants/revokes *drive* (keyboard) and *sudo* (VM superuser) per user
- **Encrypted file transfer** — `/send` → `/accept` with SHA-256 verification
- **TLS** — self-signed by default, or bring your own cert; `--no-tls` for local/Tailscale use
- **Themes** — switchable "vestments" (`church` · `neon` · `crypt`)

## Layout

| Path | What |
|------|------|
| `hh/` | The Rust `ratatui` client (the flagship) |
| `cmd_chat/`, `cmd_chat.py` | The Python (Sanic) server + legacy Python client |
| `hh/lets-hack.sh` | Spin up a local test "clergy" in tmux (server + N client panes) |
| `hh/direnv-autostart/` | `cd` into a directory to auto-launch a session (direnv) |

## Quick start

### 1. Build the client + start a server (one command)

The fastest way to see it working is the tmux harness, which builds the client,
boots a fresh `--no-tls` server on `127.0.0.1:4173`, and opens a pane per user:

```bash
cd hh
./lets-hack.sh                  # alice + bob, tiled in tmux
./lets-hack.sh neo trinity      # custom users
./lets-hack.sh --theme neon     # pick vestments
./lets-hack.sh --kill           # tear it all down
```

### 2. Manual setup

**Server** (Python):

```bash
pip install -r requirements.txt
python3 cmd_chat.py serve 0.0.0.0 3000 --password <room-password>
```

**Client** (Rust):

```bash
cd hh
cargo build --release
./target/release/hack-house connect <server_ip> 3000 <yourname> \
    --password <room-password> --insecure
```

| Flag | Purpose |
|------|---------|
| `--password` | Room password (required) |
| `--no-tls` | Connect without TLS (local / trusted tunnel) |
| `--insecure` | Skip TLS cert verification (self-signed certs) |
| `--theme <path>` | Load a vestments TOML (see `hh/themes/`) |

### 3. Autostart with direnv (optional)

```bash
cd hh/direnv-autostart
./setup.sh           # installs direnv, hooks your shell, allows this dir
```

After that, `cd` into the directory launches a single session for the logged-in
user with a freshly minted in-memory room password (revealed in-app with `/pw`).
Nothing is written to disk.

## Using it

Type to chat. Slash commands and keys:

| Command / key | Action |
|---|---|
| `<text>` ↵ | Send an encrypted chat message |
| `/help` · `F1` | Help overlay |
| `/pw` | Show this room's password (local only — never broadcast) |
| `/theme [name]` | Switch vestments, or list them |
| `/send <path>` | Offer a file (or directory) to the room |
| `/accept` · `/reject` | Respond to a pending file offer |
| `/sbx launch [local\|docker\|multipass] [image]` | Summon the shared sandbox |
| `/sbx stop` | Tear down the sandbox you host |
| `/drive` · `F2` | Take the shared shell (`Esc` releases) |
| `/grant <user>` · `/revoke <user>` | Owner: delegate/withdraw drive |
| `/sudo <user>` · `/unsudo <user>` | Owner: delegate/withdraw VM superuser |
| `Ctrl+C` · `Ctrl+Q` | Quit gracefully |
| `Ctrl+C` (while driving) | Interrupt the running command |
| `Ctrl+R` | Reconnect after a drop |
| `↑/↓` · `PgUp/PgDn` · mouse wheel | Scroll chat / sandbox scrollback |

File transfers are chunked (64 KB), encrypted with the room key, relayed as
ciphertext, and SHA-256 verified on arrival. Files land in `./downloads/`.

## Securing your connection

- **Tailscale (recommended)** — both parties join a tailnet; traffic rides an encrypted WireGuard tunnel, no port forwarding. Connect with `--no-tls` over the trusted tunnel, or keep TLS on.
- **LAN** — use your local IP; both devices on the same network.
- **Public internet** — forward the port and use a real cert (`--cert`/`--key` on the server).

Share the room password out-of-band (in person, a disappearing Signal message,
or a one-time-secret link) — never over an unencrypted channel.

## How it works

```
CLIENT                              SERVER                         CLIENT
  │── POST /srp/init {A} ──────────►│                               │
  │◄── {B, salt, room_salt} ────────│                               │
  │  derive room_key = HKDF(password, room_salt)                    │
  │── POST /srp/verify {M} ────────►│                               │
  │◄── {H_AMK, ws_token} ───────────│                               │
  │══ WSS /ws/chat?ws_token ═══════►│◄══════════════════════════════│
  │  encrypt(msg, room_key) ───────►│──── ciphertext ──────────────►│
  │                                  │        decrypt(ct, room_key)  │
  │  server stores ONLY ciphertext — it cannot read messages        │
```

- **SRP** — both sides prove they know the password without transmitting it.
- **Room key** — each client derives `HKDF(password, room_salt)` independently; the server never holds it.
- **Sandbox** — the host runs a PTY locally and relays its output as encrypted `_sbx` frames; drivers' keystrokes flow back the same way. Permissions are enforced both at the app layer (drive ACL) and in the VM (real unix users / sudo).

### Crypto parity

```bash
cd hh
cargo run -- selftest                              # offline: Rust SRP ≡ Python golden vectors
cargo run -- handshake <ip> <port> <name> --password <pw> --no-tls
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Security reports: see [SECURITY.md](SECURITY.md).

## License

MIT · *hack the planet*
