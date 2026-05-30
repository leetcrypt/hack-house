# cmd-chat → Collaborative Sandbox Sessions — Spec

> **Status:** Draft v1 · **Date:** 2026-05-30
> **Scope:** Evolves `cmd-chat` from an E2E-encrypted terminal chat into a
> multi-user collaborative session with a shared, sandboxed Linux environment.
> **Baseline reviewed:** `cmd_chat/` @ `main` (commit `dc1b5e5`).

---

## 0. Decisions locked (from product owner)

| # | Decision | Choice |
|---|----------|--------|
| A | Client language / TUI | **Rust + ratatui client**, **Python Sanic server unchanged**. Stable JSON-over-WebSocket wire protocol between them. |
| B | Sandbox backend | **Pluggable backend interface. Multipass default, Docker secondary.** |
| C | Shared-terminal model | **Single shared PTY** (collaborative, tmux-share style). Permissions = who may type. |
| D | Permission model | **Two layers:** app-level RBAC (owner/admin/member) **+** real VM unix users & `sudo` delegation. |

---

## 1. Vision & goals

Turn a cmd-chat "room" into a **shared workspace**: up to 4 people (infra for more)
join one encrypted session, chat, drop files/dirs into a shared space, and
collaboratively drive **one sandboxed Linux box** they can type commands into and
run scripts in — with real Linux permissions and a clear owner who can delegate
superuser rights.

### Goals
- Preserve the existing security guarantees: **zero-knowledge server**, **E2E
  Fernet encryption**, **SRP auth**, **RAM-only server**, **no IP leaks**.
- 4 concurrent users per session today; capacity is a single config constant, not
  an architectural limit.
- A genuinely nice **ratatui** TUI: panes, themes, custom colour/layout config.
- One-command launch of a **disposable sandbox** (Multipass VM or Docker
  container) that the whole room shares.
- File **and directory** upload into the shared session.
- Linux-grade permissions inside the sandbox + app-level roles governing the
  session itself.

### Non-goals (v1)
- Persistence / chat history survival across server restart (server stays RAM-only).
- Federation / multiple rooms per server process (one room per `serve`, as today).
- Running the sandbox *on the server* (see §4 — it runs on the initiator's client).
- Mobile / GUI clients. Terminal only.
- Multi-VM topologies. One shared sandbox per session.

---

## 2. Baseline architecture (what exists today)

```
CLIENT (python+rich)              SERVER (sanic, RAM-only)            CLIENT
  SRP handshake  ───────────────► /srp/init, /srp/verify ──────────►  (relay only)
  HKDF(pw,salt) → room_key                                            server NEVER
  Fernet(room_key) encrypt        WSS /ws/chat  (broadcast)           sees plaintext
  ──── ciphertext ──────────────► ConnectionManager.broadcast ─────►  decrypt(room_key)
  file xfer = _ft JSON over the same encrypted message channel (64KB chunks, SHA-256)
```

- **Server modules:** `factory.py` (DI/wiring), `server.py` (TLS + run),
  `routes.py`, `views.py` (SRP + ws + admin), `managers.py`
  (`ConnectionManager`), `stores.py` (`MessageStore`, `UserSessionStore`),
  `srp_auth.py`, `models.py`, `helpers.py` (`RateLimiter`, `get_client_ip`).
- **Client:** single `client.py` — `Client` class, asyncio `receive_loop` +
  `input_loop`, rich console clear-and-reprint, `_ft` file-transfer protocol.
- **Wire types today:** `init`, `message`, `user_left`. File transfer rides inside
  `message.text` as JSON beginning `{"_ft": …}` (offer/accept/reject/chunk/done).

### What we keep vs. change
| Component | v1 plan |
|---|---|
| Sanic server, SRP, TLS, RateLimiter | **Keep**, extend with new relay message types + capacity check. |
| `ConnectionManager` broadcast | **Keep**; add `broadcast(exclude_user)` usage and roster events. |
| RAM-only / zero-knowledge | **Keep** — non-negotiable; everything new also rides as ciphertext. |
| Python rich client | **Replace** with Rust ratatui client (protocol-compatible). |
| File transfer `_ft` protocol | **Keep & extend** for directories (tar stream). |

---

## 3. Target architecture (high level)

```
┌─────────────────────────── SESSION (one room) ───────────────────────────┐
│                                                                           │
│  OWNER CLIENT (Rust/ratatui)            SERVER (Sanic, dumb relay)         │
│  ┌───────────────────────────┐          ┌─────────────────────────┐       │
│  │ ratatui UI                │          │ SRP / TLS / rate-limit  │       │
│  │ chat | roster | sandbox   │◄──WSS───►│ ConnectionManager       │◄──┐   │
│  │ E2E Fernet(room_key)      │  cipher  │ broadcast(opaque bytes) │   │   │
│  │ ┌───────────────────────┐ │  text    │ Stores (RAM only)       │   │   │
│  │ │ SANDBOX BROKER (local)│ │          └─────────────────────────┘   │   │
│  │ │  Multipass | Docker   │ │                                        │   │
│  │ │  PTY ⇄ encrypted frames│ │     MEMBER CLIENTS (Rust/ratatui) ─────┘   │
│  │ │  RBAC + unix-user map  │ │     decrypt → render shared PTY pane       │
│  │ └───────────────────────┘ │     send keystrokes (if permitted)         │
│  └───────────────────────────┘                                            │
└───────────────────────────────────────────────────────────────────────────┘
```

**Key principle — the server stays zero-knowledge.** The sandbox does **not** run
on the server (the server has no room key and must never see plaintext). Instead:

- The **client that launches the sandbox** (normally the owner) hosts a local
  **Sandbox Broker** that spawns the Multipass VM / Docker container and owns its
  PTY.
- PTY output is Fernet-encrypted with the room key and relayed through the server
  as opaque ciphertext — identical trust model to file transfer.
- Keystrokes from other clients travel encrypted to the broker, which is the
  **single policy-enforcement point** (it decides who may type / `sudo` / upload).

This is the only design that satisfies *both* "shared sandbox" *and* "server can't
read anything." It is documented as a constraint, not an accident.

---

## 4. Wire protocol (v2)

### 4.1 Versioning & envelope
- Add a top-level relay type **`hello`** exchanged right after `init` carrying
  `protocol_version` (`2`). Server relays it untouched. Clients negotiate down /
  warn on mismatch.
- All new collaborative payloads ride **inside the encrypted channel** the same way
  `_ft` does: the cleartext-to-server is a `message` frame whose decrypted `text`
  is JSON with a discriminator key. We generalise `_ft` to a namespaced envelope:

```jsonc
// decrypted application payload (server only ever sees the ciphertext of this)
{
  "v": 2,
  "kind": "chat" | "file" | "dir" | "sbx" | "perm" | "presence",
  "id":  "uuid",            // correlation id
  "from":"username",        // asserted by sender, validated by broker for sbx/perm
  "ts":  "iso8601",
  "body": { /* kind-specific */ }
}
```

> Rationale: keeps the server's relay role unchanged (still just `message`
> broadcast of opaque bytes) while giving the app a clean, extensible schema.
> Existing `_ft` messages map onto `kind:"file"`.

### 4.2 New server-visible relay types (cleartext metadata only)
The server learns nothing it shouldn't; these carry no plaintext content.
- `roster` — authoritative presence list + capacity (server-generated; see §5).
- `capacity_full` — sent on connect rejection (HTTP 409 / ws close code).
- Everything else stays `message` (opaque ciphertext).

### 4.3 Sandbox sub-protocol (`kind:"sbx"`, encrypted, broker-authoritative)
| `body.op` | Direction | Meaning |
|---|---|---|
| `launch` | client→broker | request to start sandbox (owner/admin only) |
| `status` | broker→all | `starting`/`ready`/`stopped`/`error`, backend, image, specs |
| `pty_data` | broker→all | base64 PTY output chunk (the shared terminal stream) |
| `pty_input` | client→broker | keystrokes; broker enforces "may type" ACL |
| `resize` | client→broker | cols/rows; broker applies if sender is the active driver |
| `run_script` | client→broker | upload+exec a script in the VM (perm-gated) |
| `stop` | owner→broker | tear down sandbox |

### 4.4 Permission sub-protocol (`kind:"perm"`, broker-authoritative)
| `body.op` | Meaning |
|---|---|
| `grant` / `revoke` | change app role (admin/member) — owner/admin only |
| `sudo_grant` / `sudo_revoke` | add/remove a user from VM sudoers — superuser only |
| `acl` | broker broadcasts the current authoritative ACL snapshot |

---

## 5. Feature 1 — Multi-user sessions (4 now, N later)

**Current state:** `ConnectionManager` already supports arbitrarily many websocket
connections; `username_exists` blocks dup names. No capacity cap, no real roster
broadcast (only `init` snapshot + `user_left`).

**Changes**
1. **Capacity constant** `SESSION_MAX_USERS = 4` in `factory.py` (env override
   `CMD_CHAT_MAX_USERS`). Enforced in `srp_verify` *and* on ws connect:
   reject with `409 Username/Room full` / ws close code `4004` when
   `session_store.count() >= max`.
2. **Authoritative roster.** Server emits a `roster` event (join, leave,
   role-change) so all clients converge on one presence list with roles. Replaces
   the ad-hoc `user_left` patching (kept for back-comp, superseded by `roster`).
3. **`user_joined` event** added (today only `user_left` exists) for live roster.
4. **Infra-for-more:** capacity is data, not code. Document that >4 needs (a) UI
   roster scroll, (b) broadcast fan-out is O(N) per message — fine to low double
   digits; note ceiling in README. No protocol change required to raise the cap.

**Acceptance**
- 5th join attempt to a 4-cap room is cleanly refused with a user-visible reason.
- Roster in every client matches server truth within one broadcast round-trip.
- Raising `CMD_CHAT_MAX_USERS=8` works with zero code changes.

---

## 6. Feature 2 — Rust ratatui client (enhanced + themeable)

**New crate:** `cmd-chat-tui/` (Rust 2021). Talks the v2 protocol to the existing
Sanic server. The Python client remains in-tree as a reference/fallback until the
Rust client reaches parity, then is deprecated.

### 6.1 Crate layout
```
cmd-chat-tui/
  Cargo.toml
  src/
    main.rs            # CLI (clap): connect/serve-shim, flags mirror python
    app.rs             # App state, event loop (tokio + crossterm)
    net/
      srp.rs           # SRP-6a client (crate: srp + sha2) — matches python params
      ws.rs            # tokio-tungstenite WS client
      crypto.rs        # HKDF-SHA256 → Fernet (crate: fernet) room key
      proto.rs         # v2 envelope (serde) types
    ui/
      layout.rs        # ratatui layout regions
      chat.rs          # chat pane (scrollback, msg styling)
      roster.rs        # users + roles + sandbox status
      sandbox.rs       # shared PTY pane (vt100 parse: crate `vt100`)
      input.rs         # input box, command palette (/send /run /grant …)
      theme.rs         # theme model + loader
    sandbox/
      broker.rs        # local PTY broker (owner side) — see §8
      backend.rs       # trait SandboxBackend
      multipass.rs     # default backend
      docker.rs        # secondary backend
    files.rs           # upload (file + dir/tar), SHA-256, chunking
    perms.rs           # client-side ACL view + enforcement hints
  themes/
    default.toml  nord.toml  gruvbox.toml  mono.toml
```

### 6.2 Crypto parity (must match Python exactly)
- SRP-6a, group **RFC5054 / SHA-256**, identity `b"chat"` (matches
  `srp_auth.py`). Verify interop against the live Sanic server early — this is the
  #1 integration risk.
- Room key: `HKDF(SHA256, len=32, salt=room_salt, info=b"cmd-chat-room-key")`
  over the password, then `Fernet(urlsafe_b64(key))` — byte-for-byte as
  `client.py::srp_authenticate`.
- WS auth: `ws_token` echoed from `/srp/verify`; HMAC token already server-side.

### 6.3 UI / layout
Default layout (resizable, ratatui `Layout` constraints):
```
┌ cmd-chat ── room: <name> ── 🔒 E2E ── users 3/4 ──────────────┐
│ ┌─ chat ───────────────────────────┐ ┌─ roster ─────────────┐ │
│ │ 12:01 alice: hey                 │ │ ● alice  (owner,root)│ │
│ │ 12:01 bob:   yo                  │ │ ● bob    (admin,sudo)│ │
│ │ …scrollback…                     │ │ ○ carol  (member)    │ │
│ └──────────────────────────────────┘ │ sandbox: ● ready     │ │
│ ┌─ sandbox  (shared PTY · driver: alice) ─────────────────────┐│
│ │ ubuntu@sbx:~$ ./build.sh                                    ││
│ │ …live vt100 output for everyone…                           ││
│ └────────────────────────────────────────────────────────────┘│
│ > type message  ·  /send /run /sbx /grant  (F2 toggle PTY focus)│
└────────────────────────────────────────────────────────────────┘
```
- **Panes:** chat, roster (with roles + sandbox status), shared PTY, input.
- **Focus model:** Tab cycles panes; `F2` toggles "drive the PTY" (keystrokes go to
  sandbox) vs "chat input". Visible indicator of who currently holds the PTY driver
  token (see §8.3).
- **Command palette** (`/`): `/send`, `/sendd <dir>`, `/run <script>`, `/sbx
  launch|stop`, `/grant <user> <role>`, `/sudo <user>`, `/theme <name>`, `/quit`.
- **vt100 rendering** via the `vt100` crate so real terminal apps (vim, htop) render.

### 6.4 Themes & custom design
- TOML themes in `themes/`, loaded from `$XDG_CONFIG_HOME/cmd-chat/themes/` first.
- Schema: named colours (16 + hex), per-element styles (chat.self, chat.other,
  timestamp, roster.owner, sandbox.border, …), border style, layout ratios
  (chat:roster split, PTY height), and key-bindings overrides.
- `--theme <name>` flag + `/theme` live switch. Ships: default, nord, gruvbox, mono.

**Acceptance**
- Authenticates against the unchanged Sanic server and exchanges chat with a
  Python client (proves crypto + protocol parity).
- vim/htop usable inside the shared PTY pane.
- Switching theme at runtime restyles without reconnect.

---

## 7. Feature 3 — Launchable sandboxed environment (pluggable backend)

### 7.1 Backend abstraction
```rust
trait SandboxBackend {
    async fn launch(&self, spec: &SandboxSpec) -> Result<Handle>;   // create + boot
    async fn attach_pty(&self, h: &Handle) -> Result<PtyPair>;      // master pty
    async fn exec(&self, h: &Handle, argv: &[String], as_user: &str) -> Result<Output>;
    async fn put_file(&self, h: &Handle, dst: &str, bytes: &[u8], mode: u32) -> Result<()>;
    async fn add_user(&self, h: &Handle, name: &str, sudo: bool) -> Result<()>;
    async fn set_sudo(&self, h: &Handle, name: &str, enabled: bool) -> Result<()>;
    async fn stop(&self, h: &Handle) -> Result<()>;                 // destroy
}
```
- **Multipass (default):** `multipass launch`, `multipass exec`, `multipass
  transfer`, `multipass shell` (PTY via `multipass exec -- bash -i` over a pty),
  `multipass delete --purge`. Real VM ⇒ strong isolation, genuine users/sudo.
- **Docker (secondary):** `docker run -d`, `docker exec -it`, `docker cp`,
  `useradd`/`gpasswd`. Faster, weaker isolation; flag the root-in-container caveat.
- Backend chosen via `--sbx-backend multipass|docker` (default multipass), with
  capability probe (which binary exists) and graceful fallback message.

### 7.2 `SandboxSpec`
- image/release (e.g. `24.04` for multipass, `ubuntu:24.04` for docker),
  cpus, mem, disk, name (`sbx-<room>-<short>`), `disposable: true` (purge on stop /
  session end). Defaults sized small (1 cpu / 1G / 5G).

### 7.3 Launch flow
1. Owner/admin runs `/sbx launch` → broker validates RBAC (§9) → `sbx.status:
   starting` broadcast.
2. Broker boots backend, opens PTY, creates VM users for each *current* member
   (owner = sudoer/root, others = standard) — see §9.4.
3. `sbx.status: ready` broadcast with backend/image/specs.
4. Broker streams `pty_data` (encrypted) to all; accepts `pty_input` from the
   permitted driver; everyone watches live.
5. `/sbx stop` (owner) → `stop()` → purge → `sbx.status: stopped`.

### 7.4 Lifecycle & safety
- Sandbox is **disposable**: destroyed on `/sbx stop`, on owner disconnect (grace
  timer), and on server/session end. Nothing persists by default (matches RAM-only
  ethos). A `--keep` flag can opt out for the owner.
- Resource caps enforced via backend spec; reject launch if host lacks resources.
- The broker runs on the **owner's machine**, so the owner is implicitly trusting
  their own host to run the VM — documented clearly.

**Acceptance**
- `/sbx launch` boots a Multipass VM in the owner's client; all members see
  `ready` and live output. Switching `--sbx-backend docker` works unchanged at the
  protocol level.
- `/sbx stop` fully purges the VM/container (verified: `multipass list` clean).

---

## 8. Feature 4 — Shared collaborative PTY

### 8.1 Model
Single shared PTY (decision C). The broker owns one master PTY connected to a
shell in the sandbox. Output fans out to everyone (`pty_data`); input is funneled
from whoever currently holds the **driver token**.

### 8.2 Output path (E2E)
PTY master output → broker reads → base64 → Fernet(room_key) → `message` relay →
all clients decrypt → feed bytes to local `vt100` parser → render sandbox pane.
Server sees only ciphertext. Output is **broadcast to all** including the driver
(so everyone has identical screen state).

### 8.3 Input path & driver token
- "Who may type" is a permission (see §9). Among permitted users, a **single driver
  token** prevents keystroke interleaving.
- Default: token follows last `request-drive` (F2) and auto-releases after N
  seconds idle, or explicit release. Owner can force-grab.
- Non-driver permitted users see a "request drive" affordance; the current driver
  / owner approves (or owner config = open-grab among permitted users).
- Broker is authoritative: it drops `pty_input` from anyone not holding the token.

### 8.4 Resize
- Driver's terminal size drives `resize`; others letterbox/scroll to fit. Broker
  applies `TIOCSWINSZ`.

**Acceptance**
- Two members alternately take the driver token and type; no interleaved garbage;
  all panes show identical output.
- A member without "may type" permission is silently prevented from driving and
  told why.

---

## 9. Features 5 & 6 — File/dir upload + permission model

### 9.1 File & directory upload (feature 5)
Extends the existing `_ft` flow (now `kind:"file"` / `kind:"dir"`):
- **File:** unchanged semantics (offer/accept/chunk/done, 64KB, SHA-256), but a
  shared-session target: accepted files land in the **sandbox shared dir**
  (`/srv/shared`, default) *and/or* local `./downloads/` (user choice on accept).
- **Directory:** `/sendd <dir>` → broker/sender streams a **tar** of the tree as
  `kind:"dir"` chunks (same chunking + a single SHA-256 over the tar). On accept,
  extracted into `/srv/shared/<name>/` in the sandbox (path-traversal-guarded:
  reject entries with `..`, absolute paths, or symlinks escaping root).
- If no sandbox is running, dir/file upload still works peer-to-peer into
  `./downloads/` (parity with today).
- Max sizes: keep 50 MB/file; add `CMD_CHAT_MAX_UPLOAD` and a per-session
  aggregate cap. Files written into the VM get owner = uploader's VM user, mode
  `0644` (dirs `0755`).

**Acceptance**
- `/sendd ./project` puts the tree under `/srv/shared/project` in the VM with
  correct ownership; SHA-256 verified; traversal attack entries rejected.

### 9.2 Permission model — two layers (feature 6)

**Layer 1 — App-level RBAC (session control).** Enforced by the broker (the only
component that can read plaintext and owns the sandbox). Roles:

| Role | Capabilities |
|---|---|
| **owner** | Everything. The initiator. Can launch/stop sandbox, grant/revoke admin, delegate VM superuser, force driver token, kick. Exactly one. |
| **admin** | Launch sandbox, manage members (grant/revoke member-level), approve drive requests, manage uploads. Cannot remove owner. |
| **member** | Chat, request drive, upload (if allowed), use sandbox per VM perms. |

- Owner is whoever ran `serve` / first authenticated as owner (config:
  `CMD_CHAT_OWNER=<username>`; default = first user to connect). Ownership transfer
  via `perm.grant owner <user>` (owner only).
- RBAC changes broadcast as `perm.acl` snapshots so all clients show current roles.

**Layer 2 — Real VM unix users & sudo (filesystem/command control).** Inside the
sandbox, each session member maps to a real Linux account:
- On member join *while sandbox is up*, broker `add_user(name, sudo=role∈{owner})`.
- **Superuser = real root/sudoer.** The initiator's VM user is in `sudo` group.
- **Delegation:** owner runs `/sudo <user>` → `perm.sudo_grant` → broker
  `set_sudo(user, true)` (adds to `sudo` group in VM). `/sudo -r <user>` revokes.
- The shared PTY shell runs **as the current driver's VM user**, so real Linux
  permissions (file ownership, `sudo` prompts) apply naturally — a member without
  sudo who types `sudo apt install` gets denied by the *VM*, not just the app.

> The two layers are complementary: app RBAC decides *who can touch the session and
> the driver token*; unix perms decide *what a command can actually do once typed*.
> Defense in depth — an RBAC bug can't grant root, and a VM-perms bug can't bypass
> session control.

**Acceptance**
- Owner delegates sudo to bob; bob's `sudo` commands now succeed in the VM; revoke
  works (`gpasswd -d`).
- A member demoted from admin immediately loses launch/grant abilities (next
  broker-validated action rejected; `perm.acl` updated in all clients).
- Driver shell runs as the driver's own unix user (verify with `whoami`/`id`).

---

## 10. Security considerations

- **Server remains zero-knowledge.** PTY frames, uploads, perms — all ride as
  Fernet ciphertext inside `message`. The server's job is unchanged (broadcast
  opaque bytes). Re-audit `views.py::chat_ws` to confirm no new plaintext leaks.
- **Broker = trust anchor.** It holds the room key and runs the VM on the owner's
  host. It is the single enforcement point for sbx/perm ops. Document that members
  are trusting the owner's host (where the VM runs).
- **Sender spoofing:** `from` in the envelope is asserted by the sender. For
  chat that's acceptable (today's model). For `sbx`/`perm` ops the broker **must
  bind identity to the ws session** (the `user_id`/`ws_token` already authenticated
  by the server), not to the self-asserted `from`. Add a server-stamped, integrity-
  protected sender id to relayed frames, or have the broker correlate via the
  authenticated channel. **Open item — see §12.**
- **Sandbox escape:** Multipass (VM) >> Docker (container). Default to Multipass;
  warn loudly when Docker is selected. Never run the broker's host shell — only the
  VM shell is shared.
- **Path traversal / zip-slip** on dir upload: hard-reject `..`, absolute, escaping
  symlinks; extract under a fixed root with a safe tar extractor.
- **Resource exhaustion:** cap VM cpu/mem/disk, upload sizes, PTY output rate
  (coalesce + backpressure so a `yes` flood can't DoS the room).
- **Rate limiting:** keep on `/srp/*`; add light rate-limit on launch/upload ops at
  the broker.
- Keep TLS-by-default, SRP, getpass, no-IP-broadcast guarantees intact.

---

## 11. Milestones

| Phase | Deliverable | Gates |
|---|---|---|
| **P0** | Spec sign-off + crypto interop spike: Rust SRP+HKDF+Fernet talks to live Sanic server. | Rust client exchanges one chat msg with Python client. |
| **P1** | Rust ratatui client at parity (chat, roster, file xfer, themes). Deprecate rich client. | Feature-2 acceptance. |
| **P2** | Multi-user hardening: capacity cap, authoritative roster, `user_joined`. | Feature-1 acceptance. |
| **P3** | Sandbox broker + backend trait + Multipass; shared PTY (output+driver token). | Features 3 & 4 acceptance. |
| **P4** | Permissions: app RBAC + VM unix users/sudo delegation; sender-identity binding. | Feature-6 acceptance + §10 sender fix. |
| **P5** | Dir upload (tar) into shared dir; Docker backend; resource/rate caps; docs. | Feature-5 acceptance; Docker smoke test. |

---

## 12. Open questions / risks

1. **Sender-identity binding (security-critical).** `sbx`/`perm` ops must be tied to
   the server-authenticated `user_id`, not the self-asserted `from`. Options: (a)
   server stamps an authenticated sender id onto every relayed `message`
   (small server change, breaks "pure relay" purity slightly but stays
   zero-knowledge re: *content*); (b) broker-side challenge per privileged op.
   **Recommend (a).** Needs owner decision.
2. **Where does the broker live if the owner uses the Rust client on a phone/thin
   host?** v1 assumes owner host can run Multipass. Fallback: allow a designated
   "sandbox-host" member. Out of scope v1?
3. **Multipass PTY fidelity** via `multipass exec` — confirm full pty (job control,
   resize). If lacking, SSH into the VM instead (`multipass` injects a key).
4. **vt100 crate completeness** for heavy TUIs (vim/tmux-in-VM). Spike in P3.
5. **Cross-platform** broker (owner on macOS/Windows)? Multipass supports both;
   Docker paths differ. v1 target = Linux owner (matches current usage).
6. **Capacity >4 fan-out** — O(N) broadcast + N PTY decryptions per frame. Fine to
   ~12; note ceiling. Coalesce PTY output to bound it.

---

## 13. Testing strategy

- **Crypto interop** (P0): Rust↔Python chat round-trip; golden vectors for
  HKDF/Fernet/SRP.
- **Protocol:** schema tests for v2 envelope; back-compat with `_ft`.
- **Server:** extend existing pytest suite (`tests/`) for capacity cap, roster
  events, sender-id stamping.
- **Broker (Rust):** unit tests for RBAC matrix, ACL transitions, driver-token
  state machine, tar-extract traversal guard.
- **Integration:** scripted Multipass launch in CI-capable runner (or gated
  manual); verify user/sudo mapping, file ownership, purge-on-stop.
- **Manual lab:** extend `lab/setup-lab.sh` to a 3-pane Rust-client lab + a
  `/sbx launch` smoke walkthrough.

---

## Appendix A — Mapping requested features → spec sections

| Requested | Section |
|---|---|
| 1. Up to 4 users, infra for more | §5 |
| 2. ratatui TUI, enhanced + custom colours/layout | §6 |
| 3. Launch sandboxed env (Multipass), run commands/scripts | §7, §8 |
| 4. Shared collaborative terminal | §8 |
| 5. Upload files/directories to shared session | §9.1 |
| 6. Linux perms in VM, superuser by initiator + delegation | §9.2 |
