<div align="center">

# hack-house

### encrypted collaborative terminal sessions with a summoned sandbox

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Rust](https://img.shields.io/badge/client-rust-orange.svg)](https://www.rust-lang.org/)
[![Python 3.10+](https://img.shields.io/badge/server-python%203.10+-blue.svg)](https://www.python.org/downloads/)

</div>

---

Fork from https://github.com/diorwave/cmd-chat

This tool a privacy and security oriented chatroom that we added file sharing as well as shared terminal sessions. 

For sharing. For learnng. For hacking. For building. For demos. For teaching. For mentorhsip for the people who dont want to trust corporations to manage their data and communications.


Encrypted chat that runs in your terminal. You host the server, you control the room. Close the window — everything's gone. Messages and files are encrypted client-side before the server ever sees them.


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

```bash
git clone https://github.com/leetcrypt/hack-house.git
cd hack-house
```

### 1. One-shot setup (`bootstrap.sh`)

Checks prerequisites, creates the Python venv, installs the server's
dependencies, and builds the Rust client:

```bash
./bootstrap.sh              # venv + deps + debug build
./bootstrap.sh --release    # release build
./bootstrap.sh --check      # report tooling only, change nothing
```

> `bootstrap.sh` does **not** touch direnv — the autostart in step 4 is a
> separate, opt-in convenience.

### 2. Try it in tmux (`lets-hack.sh`)

The fastest way to see it working: builds the client, boots a fresh `--no-tls`
server on `127.0.0.1:4173`, and opens a pane per user.

```bash
cd hh
./lets-hack.sh                  # alice + bob, tiled in tmux
./lets-hack.sh neo trinity      # custom users
./lets-hack.sh --theme neon     # pick vestments
./lets-hack.sh --reuse          # keep a live server (reconnect tests)
./lets-hack.sh --kill           # tear it all down
```

### 3. Manual setup

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

### 4. Autostart with direnv (optional, separate)

A convenience for daily use, independent of `bootstrap.sh`. Run the one-time
setup once:

```bash
cd hh/direnv-autostart
./setup.sh           # installs direnv, hooks bash/zsh, `direnv allow`s this dir
```

After that, simply `cd`-ing into the directory launches a single session for the
logged-in user with a freshly minted **in-memory** room password (reveal it
in-app with `/pw`, share it out-of-band to invite others). The password is
generated at launch and never written to disk — matching the project's RAM-only
model. If a session is already live, it just points you at it.

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

### The shared sandbox

Anyone in the room can summon a disposable Linux box with `/sbx launch`. The
person who summons it is the **owner/host**: their client runs the real PTY
locally and relays its output to everyone else as encrypted frames, so the
server only ever sees ciphertext (same trust model as chat).

| Backend | Isolation | Notes |
|---|---|---|
| `local` | none | a `bash` shell on the host — fast, for dev/testing only |
| `docker` | container | `ubuntu:24.04` by default; `/sbx launch docker --start` boots the daemon (or run `./ensure-docker.sh`) |
| `multipass` | full VM | `24.04` by default; strongest isolation, ~30 s to boot, the choice for real use |

Tear it down with `/sbx stop` (purges the VM/container).

### Driving the shell

The shared terminal is **watch-by-default**: everyone sees the live output, but
only granted drivers can type into it.

- `/drive` (or `F2`) takes the keyboard; `Esc` releases it. `/drive` exists so
  the whole flow works on mobile/SSH clients with no function keys.
- While driving, your keystrokes go to the PTY; `Ctrl+C` interrupts the running
  command (it does **not** quit the app).
- `PgUp`/`PgDn` and the mouse wheel scroll the terminal's scrollback even while
  driving; `End` jumps back to live.

### Unix permission control

Permissions are enforced at **two layers**:

1. **App-level drive ACL** — who is allowed to type into the shared shell.
   The owner runs `/grant <user>` / `/revoke <user>`.
2. **Real VM identities** — on `multipass`/`docker`, each member is provisioned
   an actual unix account, with the owner as superuser. On `multipass`,
   `/sudo <user>` / `/unsudo <user>` toggle real `sudo` rights inside the VM, so
   "may type" and "may run privileged commands" are independent and enforced by
   the OS itself.

The roster shows each member's status: owner, sudoer (⚡), driver (◆), or
member (•).

### Sharing files & directories

`/send <path>` proposes a transfer; recipients `/accept` or `/reject`. A whole
directory works too (it's packed before sending). Files are chunked (64 KB),
encrypted with the room key, relayed as opaque ciphertext, and **SHA-256
verified** on arrival before landing in `./downloads/`. Max size is 50 MB.

### Themes (vestments)

Three bundled themes — `crypt` (default, neutral monochrome), `church` (neon),
and `neon`. Switch live with `/theme <name>`, list them with bare `/theme`, or
load your own TOML at launch with `--theme <path>` (see `hh/themes/`). Each
theme defines its own sigil, colours, and roster width.

### Staying connected

If the connection drops (network blip, laptop sleep), press `Ctrl+R` to re-run
the SRP handshake and re-attach — no restart needed. If you were hosting the
sandbox, it's re-announced so the room re-syncs the shared shell. Chat keeps up
to ~4000 lines of scrollback; the sandbox terminal keeps 2000.

## Configuration

| Variable | Where | Effect |
|---|---|---|
| `CMD_CHAT_MAX_USERS` | server | Room capacity (default `4`) |
| `PORT` · `PW` · `HOST` | `lets-hack.sh` | Override the test server's port / password / bind host |
| `THEME` | `lets-hack.sh` | Vestments for every pane (`church` · `neon` · `crypt`) |
| `HH_SESSION` · `HH_USER` | direnv autostart | tmux session name / your in-session name |

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
