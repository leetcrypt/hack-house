<div align="center">

# hack-house

### encrypted collaborative terminal sessions with a summoned sandbox

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Rust](https://img.shields.io/badge/client-rust-orange.svg)](https://www.rust-lang.org/)
[![Python 3.10+](https://img.shields.io/badge/server-python%203.10+-blue.svg)](https://www.python.org/downloads/)

![hack-house demo](docs/hack-house-demo.gif)

*Two clients sharing a multipass sandbox — summon, drive the shell, real per-user sudo.*

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
- **Snapshot save/load** — freeze a sandbox to a named snapshot and restore it later (`/sbx save` · `/sbx load` · `/sbx snaps`)
- **Local VirtualBox VMs** — `/sbx vms` detects VirtualBox and lists your VMs; `/sbx gui <vm>` opens a desktop VM locally for the room to gather around — per-user consent gate, with automatic resolution of VT-x conflicts (Docker Desktop / multipass)
- **Real permissions** — the host grants/revokes *drive* (keyboard) and *sudo* (VM superuser) per user; **stacking roster badges** show exactly who holds what, both in the clergy panel and inline on every chat message
- **Local-first AI agent** — `/ai start` summons an in-room AI that runs against *your own* [Ollama](https://ollama.com) (no API key, nothing leaves your machine); replies **stream token-by-token** with **in-RAM semantic recall** of the conversation for context; model-agnostic, addressed-only, end-to-end encrypted like every other client
- **Encrypted file transfer** — `/send` → `/accept` with SHA-256 verification
- **TLS** — self-signed by default, or bring your own cert; `--no-tls` for local/Tailscale use
- **Themes** — seven switchable "vestments" (`crypt` default · `church` · `neon` · `blush` · `matrix` · `wraith` · `goldcrypt`), plus a live randomizer

## Layout

| Path | What |
|------|------|
| `hh/` | The Rust `ratatui` client (the flagship) |
| `cmd_chat/`, `cmd_chat.py` | The Python (Sanic) server + legacy Python client |
| `cmd_chat/agent/` | The model-agnostic AI agent bridge (joins a room as an encrypted client) |
| `models.toml` | Named provider profiles for `/ai start <profile>` (see `docs/providers.md`) |
| `docs/providers.md` | Connect any model — profiles, flags, discovery, bring-your-own-provider |
| `hh/scripts/lets-hack.sh` | Spin up a local test "clergy" in tmux (server + N client panes) |
| `hh/scripts/bootstrap-ai.sh` | Optional: install Ollama + pull a model for the local `/ai` agent |
| `hh/direnv-autostart/` | `cd` into a directory to auto-launch a session (direnv) |

## Quick start

```bash
git clone https://github.com/leetcrypt/hack-house.git
cd hack-house
```

### 1. One-shot setup (`hh/scripts/bootstrap.sh`)

Checks prerequisites, creates the Python venv, installs the server's
dependencies, and builds the Rust client:

```bash
hh/scripts/bootstrap.sh              # venv + deps + debug build
hh/scripts/bootstrap.sh --release    # release build
hh/scripts/bootstrap.sh --check      # report tooling only, change nothing
```

> `bootstrap.sh` does **not** touch direnv — the autostart in step 4 is a
> separate, opt-in convenience.

**Optional AI layer (`hh/scripts/bootstrap-ai.sh`).** Want the local `/ai` agent? This runs
the baseline setup first, then installs Ollama (if missing) and pulls a default
model — nothing here changes the AI-free baseline above:

```bash
hh/scripts/bootstrap-ai.sh                      # baseline + Ollama + qwen2.5:3b
hh/scripts/bootstrap-ai.sh --check              # report only, change nothing
HH_AI_MODEL=llama3 hh/scripts/bootstrap-ai.sh   # pull a different model
```

### 2. Try it in tmux (`scripts/lets-hack.sh`)

The fastest way to see it working: builds the client, boots a fresh `--no-tls`
server on `127.0.0.1:4173`, and opens a pane per user.

```bash
cd hh
./scripts/lets-hack.sh                  # alice + bob, tiled in tmux
./scripts/lets-hack.sh neo trinity      # custom users
./scripts/lets-hack.sh --theme neon     # pick vestments
./scripts/lets-hack.sh --reuse          # keep a live server (reconnect tests)
./scripts/lets-hack.sh --kill           # tear it all down
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
| `/ai start [model\|profile]` | Summon a local AI agent (default `ollama/qwen2.5:3b`; a bare name is a `models.toml` profile) |
| `/ai stop` | Dismiss the agent you summoned |
| `/ai <question>` | Ask the agent (`/ai <name> <question>` if several present) |
| `/ai list` | List the agents present (or hint to `/ai start` if none) |
| `/ai models` | Models the active agent can serve — or, with no agent, your local Ollama tags |
| `/sbx launch [local\|docker\|multipass] [image]` | Summon the shared sandbox |
| `/sbx stop` | Tear down the sandbox you host |
| `/sbx save [label]` · `/sbx load <label>` · `/sbx snaps` | Snapshot the sandbox, restore one, or list snapshots |
| `/sbx vms` | Detect VirtualBox and list local VMs |
| `/sbx gui <vm> [--install]` | Open a local VirtualBox desktop VM for the room (consent-gated) |
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
| `docker` | container | `ubuntu:24.04` by default; `/sbx launch docker --start` boots the daemon (or run `hh/scripts/ensure-docker.sh`) |
| `multipass` | full VM | `24.04` by default; strongest isolation, ~30 s to boot, the choice for real use |

Tear it down with `/sbx stop` (purges the VM/container).

**Snapshots.** Freeze the current sandbox to a named checkpoint with `/sbx save
[label]`, list what you've stored with `/sbx snaps`, and restore one later with
`/sbx load <label>` — handy for resuming a half-built environment or replaying a
demo from a known-good state.

**Local VirtualBox VMs.** Separate from the relayed sandbox, `/sbx vms` detects a
VirtualBox install and lists your VMs, and `/sbx gui <vm>` boots one as a full
desktop VM **on your own machine** (`--install` offers to install VirtualBox
first if it's missing). It's not owner-gated — the per-user confirmation gate
*is* the permission, so everyone opens their own copy. If a hardware hypervisor
(Docker Desktop, multipass) is holding VT-x, hack-house detects the conflict and
offers to stop it so the VM can boot.

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

The roster shows each member's status with **stacking badges**: host (the
theme's sigil, e.g. `✝`), sudoer (⚡), driver (◆), and member (•). They're
additive — a host who summoned a sandbox and can drive reads `✝⚡◆` — and the
host badge appears the moment someone is first in the room, before any sandbox
exists. The same badge is rendered inline next to the author on every chat
message, so a message's authority is legible right in the transcript, not only
in the side panel. Because the badges read the exact ACL the sandbox enforces,
they can never advertise a power the room won't honour.

### Sharing files & directories

`/send <path>` proposes a transfer; recipients `/accept` or `/reject`. A whole
directory works too (it's packed before sending). Files are chunked (64 KB),
encrypted with the room key, relayed as opaque ciphertext, and **SHA-256
verified** on arrival before landing in `./downloads/`. Max size is 50 MB.

### The AI agent (local-first)

Summon an AI participant with `/ai start` — it joins the room as a normal
encrypted client (same SRP + room key as everyone else) and answers when you
address it with `/ai <question>`. `/ai stop` dismisses it (it's also cleaned up
when you quit). Pick a model at summon time with `/ai start <model>`.

- **Runs on *your* machine.** The default provider is [Ollama](https://ollama.com)
  — a local model (default `qwen2.5:3b`), no API key, nothing leaves your host.
  Run `hh/scripts/bootstrap-ai.sh` once to install it and pull the model.
- **Addressed-only.** The agent reads room traffic like any client but forwards
  to the model *only* the messages that trigger it (`/ai …`) — no passive
  surveillance, no cost or noise when idle.
- **Model-agnostic.** Swap the backend without touching the client: bundled
  adapters for `ollama` (default), `anthropic`, and any OpenAI-compatible
  endpoint (OpenAI, Groq, Together, local vLLM…), plus a `module:Class` hook for
  your own. Cloud providers are opt-in and read their API key from the agent's
  environment — never the room.
- **Named profiles.** Register a backend once in `models.toml` and summon it by
  name: `/ai start groq-llama`. Profiles store `api_key_env` (the *name* of an
  env var, never the key), so the file is safe to commit. See the full
  [provider guide](docs/providers.md) — profiles, explicit flags, discovery, and
  bring-your-own-provider.
- **Discoverable.** `/ai list` shows who's present and `/ai models` shows what
  the active agent can serve (active model bracketed). With no agent running,
  `/ai models` still probes your local Ollama so you can see what's pullable
  before summoning. By hand, `--list-models` enumerates a backend and `--check`
  preflights it (exit 0/1) without joining a room.
- **End-to-end like everything else.** Replies are encrypted client-side; the
  server still only ever relays ciphertext.

Each agent uses one room seat — raise `CMD_CHAT_MAX_USERS` if the room is full.
To run an agent by hand (a cloud provider, or on another host), drive the bridge
directly:

```bash
.venv/bin/python -m cmd_chat.agent <server_ip> <port> \
    --password <room-pw> --provider ollama --model qwen2.5:3b
# joins as its model tag ("qwen2.5:3b") unless you override with --name
# cloud (opt-in): --provider anthropic --model claude-opus-4-6   (needs ANTHROPIC_API_KEY)
```

### Themes (vestments)

Seven bundled themes — `crypt` (default, neutral monochrome, `✝` sigil),
`church`, `neon`, `blush`, `matrix`, `wraith`, and `goldcrypt`. Switch live with
`/theme <name>`, list them with bare `/theme`, roll a fresh randomized vestment
with `Ctrl+Alt+P` (and save it to disk), or load your own TOML at launch with
`--theme <path>` (see `hh/themes/`). Each theme defines its own sigil, colours,
and roster width.

### Staying connected

If the connection drops (network blip, laptop sleep), press `Ctrl+R` to re-run
the SRP handshake and re-attach — no restart needed. If you were hosting the
sandbox, it's re-announced so the room re-syncs the shared shell. Chat keeps up
to ~4000 lines of scrollback; the sandbox terminal keeps 2000.

## Configuration

| Variable | Where | Effect |
|---|---|---|
| `CMD_CHAT_MAX_USERS` | server | Room capacity (default `4`) |
| `PORT` · `PW` · `HOST` | `scripts/lets-hack.sh` | Override the test server's port / password / bind host |
| `THEME` | `scripts/lets-hack.sh` | Vestments for every pane (`church` · `neon` · `crypt`) |
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
