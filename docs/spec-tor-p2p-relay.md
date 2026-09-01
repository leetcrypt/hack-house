# hack-house → Tor P2P Relay — Spec

> **Status:** Draft v1 · **Date:** 2026-08-30
> **Scope:** Let a host expose a hack-house room over an **ephemeral, per-session
> Tor v3 onion service** instead of (or alongside) a raw `host:port`, and hand the
> resulting address off to people who are already hack-house members **through
> hack-house's own existing encrypted chat** — no relay, no third-party rendezvous
> service, no cost.
> **Baseline reviewed:** `cmd_chat/server/` + `cmd_chat/agent/` (headless-member
> pattern) + `hh/` client @ `main`.
> **Sibling spec:** `spec-lobby-web-relay.md` (`feat/web-lobby-relay`, unmerged) —
> the browser-facing, relay-brokered path. This spec is the opposite trade-off:
> fully P2P and Tor-anonymous, zero-install-for-browsers is explicitly **not** a
> goal here, hack-house itself is the only client.

---

## 0. Decisions locked

| # | Decision | Choice |
|---|----------|--------|
| A | Transport | **Ephemeral v3 onion service per session**, created via `stem`'s `ADD_ONION` against a local `tor` ControlPort. In-memory only (`DiscardPK`) — no descriptor or key ever written to disk, destroyed (`DEL_ONION`) on session end. |
| B | Rendezvous / address handoff | **hack-house's own existing zero-knowledge chat**, not Matrix, not any external service. `/tor share` posts the onion address + a fresh one-time room password as an ordinary chat message in a room the recipient is already a member of — already Fernet-encrypted, already SRP-authenticated. No new crypto, no new trust boundary. |
| C | Guest transport | Dial through the local Tor SOCKS5 proxy (`127.0.0.1:9050`) instead of a direct socket — `hh connect <x>.onion <port> <name> --password <pw> --tor`. |
| D | Cost / infra posture | **$0.** No relay, no VPS, no domain, no homeserver of any kind. `tor` is free/FOSS (`dnf install tor`), runs locally. Optional hardening: run it in a rootless podman container, mirroring the existing `/sbx podman` sandbox pattern — still free, still local. |
| E | Prerequisite | **Both parties already have hack-house installed and already share a room.** This is not a discovery mechanism — it makes one *specific session* private/anonymous/NAT-free between people who already found each other via hack-house. |
| F | Where the code lives | **Python sidecar**, `cmd_chat/tor/`, mirroring the `cmd_chat/agent/` headless-member precedent. **No Rust/TUI protocol changes** — `/tor` is a thin command that shells to the sidecar, same shape as `/ai`. |
| G | Host process sandboxing | **`bwrap` (bubblewrap) by default** for the `tor` process itself — zero new dependency (already on `trillsec`/`laptop`, ships with Fedora for flatpak), no daemon, no image to pull. A rootless-podman variant is offered as an alternative for operators who'd rather match the existing `/sbx podman` convention; both are opt-in launch wrappers around the same unmodified `tor` binary, not a fork of it. |
| H | Bind-address guardrail | `cmd_chat.py serve --tor` **refuses a non-loopback bind address by default** (`--tor-allow-public-bind` to override). Prevents the single worst footgun in this feature: thinking you're only reachable via the onion address while a plain public/LAN listener is *also* open. Mirrors web-relay's "loud warning, explicit override" posture rather than a silent auto-correct. |

**Why B, spelled out:** the alternative rendezvous options considered were (1) a
public relay/lobby (rejected — reintroduces a server in the data path, which is
exactly what per-connection onion services exist to avoid), (2) a self-hosted
Matrix homeserver (rejected — a stateful federated service is a heavier ops/trust
burden than anything else this project runs; nothing else in hack-house persists
server-side state), (3) matrix.org / an external messenger (rejected — free, but
not self-hosted, and adds a dependency the recipient may not already have). Reusing
hack-house's own chat costs nothing, adds nothing new to harden, and the prereq
(E) makes it the honest bootstrap: you don't need this feature to meet someone for
the first time, you use it once you already have a channel.

---

## 1. Vision & goals

Two people already run hack-house together (LAN, Tailscale, whatever). One of
them wants to open a **new, throwaway, maximally private** session — no relay in
the middle, no persistent address, resistant to NAT/CGNAT, traffic-anonymized by
Tor — and hand the invite to the other **without leaving hack-house or paying for
anything**.

### Goals
- **Per-connection ephemerality.** A fresh onion address every `/tor share`; torn
  down on session end; nothing links one session's address to the next.
- **Zero marginal cost, zero new infra to operate.** No relay, no domain, no
  homeserver, no ongoing bill.
- **Reuse, don't rebuild, the rendezvous.** hack-house's chat already solves
  "how do I send you a short authenticated string" — use it.
- **NAT/CGNAT-friendly, like web-relay, but P2P.** Host doesn't need inbound ports
  or Tailscale — Tor's rendezvous handles that; no dial-out relay needed either.

### Non-goals (v1)
- **Not a discovery/lobby mechanism.** Onion addresses are deliberately
  unguessable; there is no browsable "who's live" list here (that's web-relay's
  job, a different trade-off).
- **Not a Matrix integration.** Considered and rejected per §0 — see the cost
  comparison in the companion discussion; not revisited unless the prereq (E)
  changes (e.g. a future want to reach people *without* hack-house installed).
- **Not a browser client.** hack-house-to-hack-house only; if browser access is
  the goal, that's `spec-lobby-web-relay.md`.
- **Not solving Tor's own guard-node/traffic-correlation properties.** Inherited
  as-is, same posture Tor Browser users already accept.

---

## 2. Components

| Component | Language / home | Role |
|---|---|---|
| **Onion sidecar** | Python, new `cmd_chat/tor/onion.py` | Authenticates to the local `tor` ControlPort (cookie auth), calls `create_ephemeral_hidden_service` (stem), forwards the onion port to the local `cmd_chat` server socket, returns `{address, port}`. Calls `remove_ephemeral_hidden_service` on teardown. |
| **Connector helper** | Python, new `cmd_chat/tor/connect.py` | Wraps the client's socket construction so `--tor` routes the SRP/WS handshake through local SOCKS5 instead of a direct TCP dial (`PySocks`, same pattern as `torsocks` but in-process — no external wrapper binary required). |
| **`/tor` command** | TUI command palette (thin; shells to the sidecar, same shape as `/ai`) | `/tor share [label]`, `/tor stop`. |
| **Chat message** | existing room's E2E channel — **no changes** | Carries the invite (address, port, one-time password) as a normal message. |

Nothing new runs as a server. There is no relay process, no listener exposed to
the public internet beyond the onion service itself (which is Tor's own
rendezvous infrastructure, not anything hack-house operates).

---

## 3. Flow

```
 HOST (already in a shared hack-house room)                    GUEST (same room, already a member)
 ───────────────────────────────────────────                   ─────────────────────────────────────
 cmd_chat.py serve <port> --password <pw> --tor
   └─ onion.py: ControlPort (cookie auth)
        ADD_ONION → xxxxx.onion:<port>          (in-memory key,
                                                   DiscardPK)
 /tor share "quick sync"
   └─ posts into the CURRENT room's existing
      E2E chat:
      "🧅 tor room 'quick sync' —
       xxxxx.onion:<port>  pw:<one-time>"   ───►  reads the chat message
                                                   (already a room member,
                                                    already decrypting it)

                                                   hh connect xxxxx.onion <port> <name> \
                                                     --password <pw> --tor
                                                     └─ connect.py: dial via
                                                        127.0.0.1:9050 (SOCKS5)
                                                        instead of direct TCP

 ◄──────────────────── SRP + Fernet room handshake, exactly as today, over Tor ────────────────────►

 session ends / /tor stop
   └─ DEL_ONION — address gone, no residual footprint
```

Tor is a **transport swap only**. The actual authentication and encryption is
still hack-house's existing SRP + Fernet room-key handshake — nothing about that
protocol changes. This is simpler than the web-relay case: there's no browser, so
there's no fragment-key trick and no ciphertext-relay problem to solve at all.

---

## 4. Security & OPSEC considerations

Two attack surfaces exist here: **the `tor` process itself** (a network-facing
daemon we don't control the code of) and **the hack-house server behind it**
(code we do control). Everything below is scoped, cheap, and doesn't fight
Tor's own design — no re-inventing anonymity primitives Tor already gets right.

### 4.1 Already load-bearing (P0, shipped)

- **Ephemeral key hygiene.** `ADD_ONION` with `DiscardPK` — the service's private
  key is never written to disk and never leaves the Tor process's memory. No
  long-term onion identity exists to leak, log, or correlate across sessions.
- **The chat message is a bearer credential**, same posture as the existing `/pw`
  command's local-only warning — the address + one-time password together grant
  entry. Treat the `/tor share` message like any other password share (it already
  inherits the room's existing E2E protection, so this is a reminder, not a new
  control).
- **No new server-side trust.** Because there is no relay, there is nothing to
  audit for "does it log plaintext" the way web-relay's relay needs — the data
  path is Tor's own network, which hack-house does not operate.
- **Chat server untouched.** Exactly like web-relay's decision, the core
  SRP/Fernet/ZK guarantees of `cmd_chat/server/` are unmodified — the invite
  message is just payload text in an unmodified protocol.

### 4.2 Bind-address guardrail (decision H) — the #1 footgun

`--tor` makes the onion address reachable; it says nothing about what the
`ip_address`/`port` arguments to `serve` are bound to. A host who runs
`cmd_chat.py serve 0.0.0.0 9500 --tor` gets **both** the onion *and* a plain
public listener — the exact outcome this feature exists to avoid, and easy to
do by accident since nothing about the command shouts "you're now on the public
internet." `serve --tor` refuses to start unless the bind host is loopback
(`127.0.0.1`/`::1`/`localhost`), with `--tor-allow-public-bind` as an explicit,
loud opt-out for a deliberate dual-exposure setup. Cheapest possible control —
one argparse check — for the highest-consequence mistake in this feature.

### 4.3 Sandboxing the `tor` process (decision G)

`tor` is a large, network-facing C daemon parsing untrusted input from the
Tor network by design — it deserves the same "don't run it bare on the host"
instinct the project already applies to interactive sandboxes (`/sbx podman`).
Two backends, same unmodified `tor` binary, launcher picks one:

- **`bwrap` (default, `scripts/tor-hardened-launch.sh`).** No daemon, no image,
  nothing to pull — already present wherever flatpak is. Namespace isolation
  (`--unshare-pid --unshare-uts --unshare-ipc --unshare-cgroup`), filesystem
  locked to read-only system paths plus a private, freshly-created
  `DataDirectory` (`--bind` only that, not `$HOME`), `--die-with-parent` so a
  killed launcher can't leave an orphaned `tor`, `--new-session` to drop
  controlling-terminal access. **Network is deliberately left unshared-from-host**
  (`tor` needs full outbound reachability to relays — this is filesystem/PID/IPC
  isolation, not network sandboxing; unlike the interactive `/sbx` case,
  `--network=none` is not an option here).
- **Rootless podman (alternative)**, for operators who'd rather match the
  existing container convention: `--network=host` (loopback compatibility
  with a co-located hack-house server is otherwise lost — a separate netns
  can't reach the host's `127.0.0.1:<port>`), `--cap-drop=ALL`,
  `--security-opt=no-new-privileges`, `--read-only` root with a writable
  `DataDirectory` volume only. Weaker network isolation than `bwrap` in
  exchange for full filesystem/capability isolation and a familiar teardown
  (`podman rm -f`).

Either way, the blast radius of a `tor` vulnerability is capped at "can see the
DataDirectory and talk to the network," never "can read `$HOME` or see other
host processes."

### 4.4 ControlPort / SocksPort surface reduction

- **Cookie auth only, never a stored control password** (`CookieAuthentication 1`
  in torrc) — nothing secret to leak from a config file.
- **Prefer Unix domain sockets over TCP** for both ports where the client
  supports it: `ControlSocket <path>` instead of `ControlPort`, `SocksPort
  unix:<path>` instead of a TCP port. Removes even *loopback* TCP ports from the
  local attack surface — a Unix socket under the sandboxed `DataDirectory` is
  reachable only by processes with filesystem access to it. `EphemeralOnion`
  already supports this (`control_socket=` constructor arg); `cmd_chat.py serve`
  exposes it as `--tor-control-socket PATH`.
- If a TCP ControlPort is used instead (e.g. talking to an already-running
  system `tor`), it must stay bound to loopback — never LAN/0.0.0.0. Anyone who
  can reach it can mint or kill onion services.

### 4.5 Full onion service, not single-hop

Tor supports `HiddenServiceSingleHopMode`/`HiddenServiceNonAnonymousMode` for
lower-latency "reachable but not anonymous" services. **Do not use it here** —
the whole premise of this feature is host anonymity *and* NAT-traversal, not
just NAT-traversal. `create_ephemeral_hidden_service` never sets those flags;
call this out explicitly so a future "let's speed this up" change doesn't
quietly trade away the host's anonymity to shave round-trip latency.

### 4.6 Don't fight Tor's guard design

A tempting-sounding "opsec" idea is to rotate Tor entry guards per session for
"freshness." **Don't** — guards are deliberately long-lived by Tor's own design
specifically to resist guard-discovery attacks; rotating them on our own
schedule would weaken anonymity, not improve it. Nothing to implement here;
noted so it doesn't get "fixed" later by someone unfamiliar with why guards are
sticky.

### 4.7 DoS / abuse posture (inherited, not new)

The onion address is meant to stay private, but if it leaks, the server behind
it is still just `cmd_chat/server/` — it already inherits whatever connection
caps exist there (e.g. `CMD_CHAT_MAX_USERS`) with zero new code. No additional
rate-limiting is being built for v1; flag as a P1.5/P2 follow-up only if the
default caps prove insufficient in practice — don't build DoS defenses for a
threat that hasn't been observed yet.

### 4.8 DataDirectory hygiene

The sandboxed `DataDirectory` (guard list, cached descriptors — not the
ephemeral onion key, which is never written) should live under the scratch
launch dir the hardened script creates fresh, **never** inside a git repo, a
cloud-synced folder, or anything that ends up in a VM snapshot/backup shared
with someone else. This is about not accidentally fingerprinting/correlating
guard state across contexts, not about the ephemeral key (which never touches
disk regardless).

### 4.9 Traffic correlation — Tor's limitation, not re-solved here

Guard-node compromise / global traffic-correlation risk is inherent to Tor
itself, out of scope to re-solve at this layer — same acceptance any Tor user
already makes. Not re-litigated in every section; noted once, here.

---

## 5. Cost / deployment (decision D)

- **$0 recurring cost.** No relay to run, no VPS, no domain, no TLS certificate to
  manage (Tor doesn't need one), no third-party account (Matrix or otherwise) to
  register.
- `tor` is FOSS, already packaged for this host (`dnf install tor` on Fedora).
- **Hardening is also free.** `bwrap` (default) and rootless podman (alternative)
  are both already-installed/no-new-cost — see §4.3. Sandboxing is the
  recommended default launch path, not a someday-upgrade.
- **Known gap:** mobile (Termux) typically lacks a packaged `tor` binary — flag
  for the mobile-operator worktree; Orbot or a static Tor build would be the
  fallback there. Not a v1 blocker since the desktop/laptop hosts already have it.

---

## 6. Milestones

| Phase | Deliverable | Gate |
|---|---|---|
| **P0** | `cmd_chat/tor/onion.py` — stem wrapper; `cmd_chat.py serve --tor` prints a working `xxxxx.onion:<port>`; manual guest connect via external `torsocks` proves round-trip. **DONE**, live-verified 2026-08-31 (real Tor, real onion, SRP-authenticated join, clean `DEL_ONION` teardown, zero orphaned services). | A second machine reaches the room over Tor with no inbound ports open on the host. |
| **P0.5** | Bind-address guardrail (decision H, §4.2) + `scripts/tor-hardened-launch.sh` (bwrap default, podman alt, §4.3) + `--tor-control-socket` unix-socket wiring (§4.4). **Code + script done, TDD green (177/177).** Live-verified 2026-08-31: bwrap sandbox bootstraps cleanly, `serve --tor --tor-control-socket <sock>` mints a real onion over the sandboxed Unix-socket ControlSocket, and raw transport reachability through it was confirmed end-to-end (`curl --socks5-hostname` → `HTTP 200` from the actual server, through the sandbox, over Tor, to the ephemeral onion). | `serve --tor` on a non-loopback host refuses without `--tor-allow-public-bind` — **done**. Hardened launch reaches the same "onion minted + transport reachable" state as the ad-hoc P0 test — **done**. |
| _(finding)_ | Re-running the **full async protocol round-trip** (`cmd_chat/operator` + external `torsocks`) against the sandboxed instance was flaky — SRP (sync `requests`) consistently succeeded, but the async `websockets` phase sometimes hung past 90s where the original P0 test (same client code, non-sandboxed tor, statically-configured SocksPort) completed in ~13s. Root cause not chased down (schedule-boxed); most likely `torsocks`' LD_PRELOAD interception of asyncio's non-blocking `connect()`, a known-fragile combination — not a sandboxing defect (raw transport + sync SRP both worked reliably every time). This **reinforces P1's priority**: replace external `torsocks` with in-process `PySocks` for the async client so this class of interaction goes away entirely. | — |
| _(interim)_ | `scripts/tor-onion-connect.sh` — guest-side helper: locates a checkout regardless of `$PWD` (the raw `torsocks hh/target/debug/hack-house connect …` form only works if you happen to already be sitting in a checkout root — a real footgun a laptop guest hit live), picks Rust-if-built else Python, wraps `torsocks`. Not a substitute for P1 (still needs external `torsocks`), just makes today's guest command actually robust. Live-verified 2026-09-01: run from an arbitrary directory on a second machine, found `~/coding/hack-house/main` on its own, connected successfully. | — |
| **P1** | In-process SOCKS connect (`connect.py`) — `hh connect --tor` / `cmd_chat.py connect --tor`, no external `torsocks` wrapper needed. | Guest connects with one flag, no separate tool install beyond `tor`/`PySocks`. |
| **P2** | `/tor share [label]` — posts the invite into the *current* room's existing encrypted chat; `/tor stop` tears down cleanly. | Invite round-trips entirely inside hack-house chat; no external channel touched. |
| **P3** | Teardown discipline — `DEL_ONION` reliably fires on process exit/`/tor stop`/crash; test for orphaned services. | Repeated share/stop cycles leave zero live onion services behind. |

---

## 7. Open questions / risks

1. **New room vs. exposing the current room.** v1 default: `/tor share` spins a
   **brand-new** ephemeral room (matches the "per-connection" framing and keeps
   the existing room's transport unchanged). Exposing the *current* room
   additionally via Tor is a possible stretch goal, not v1.
2. **ControlPort auth mode.** Cookie auth (recommended — nothing to store) vs. a
   configured control password. Recommend cookie-only; refuse to start if cookie
   auth isn't available rather than falling back to a stored secret.
3. **Bundled vs. system `tor`.** v1 assumes a system-installed `tor` binary
   (already true on `trillsec`/`laptop`). Bundling a static Tor binary is a later
   portability item, not required for the primary desktop/laptop use case.
4. **SOCKS library choice.** `PySocks` (pure Python, matches the project's
   pure-Python SRP fallback precedent for Termux) vs. shelling out to system
   `torsocks`. Recommend `PySocks` for the same portability reason the mobile
   operator already avoids native-extension dependencies.

---

## 8. Testing strategy

- **Onion lifecycle:** create → publish → reachable → `DEL_ONION` → unreachable;
  no leftover key material on disk at any point.
- **Transport:** guest SOCKS-routed connect succeeds only with `tor` reachable;
  clear failure (not a silent hang) when Tor isn't running.
- **Handoff:** `/tor share` message decrypts correctly for existing room members
  and is indistinguishable from any other chat message to the (unmodified) server.
- **Teardown:** repeated share/stop cycles across a session leave the ControlPort
  with zero orphaned ephemeral services (`GETINFO onions/current`).
- **Negative:** ControlPort refuses non-cookie auth attempts; sidecar refuses to
  start if it can't authenticate rather than degrading silently.
