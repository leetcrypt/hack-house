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

## 4. Security considerations

- **Ephemeral key hygiene.** `ADD_ONION` with `DiscardPK` — the service's private
  key is never written to disk and never leaves the Tor process's memory. No
  long-term onion identity exists to leak, log, or correlate across sessions.
- **The chat message is a bearer credential**, same posture as the existing `/pw`
  command's local-only warning — the address + one-time password together grant
  entry. Treat the `/tor share` message like any other password share (it already
  inherits the room's existing E2E protection, so this is a reminder, not a new
  control).
- **ControlPort exposure.** Bind the ControlPort to localhost (or a Unix socket)
  with cookie authentication only (`CookieAuthentication 1` in torrc); never
  expose it beyond loopback. This is the one new local attack surface introduced
  — anyone who can reach the ControlPort can mint/kill onion services.
- **No new server-side trust.** Because there is no relay, there is nothing to
  audit for "does it log plaintext" the way web-relay's relay needs — the data
  path is Tor's own network, which hack-house does not operate.
- **Guard-node / traffic-correlation risk is Tor's, not new.** Not re-litigated
  here; same acceptance Tor users already make.
- **Chat server untouched.** Exactly like web-relay's decision, the core
  SRP/Fernet/ZK guarantees of `cmd_chat/server/` are unmodified — the invite
  message is just payload text in an unmodified protocol.

---

## 5. Cost / deployment (decision D)

- **$0 recurring cost.** No relay to run, no VPS, no domain, no TLS certificate to
  manage (Tor doesn't need one), no third-party account (Matrix or otherwise) to
  register.
- `tor` is FOSS, already packaged for this host (`dnf install tor` on Fedora).
- **Optional hardening, still free:** run the `tor` process inside a rootless
  podman container, reusing the `/sbx podman` sandbox convention already in this
  repo, to isolate the ControlPort/onion process from the host. Purely a posture
  choice — the feature works identically without it.
- **Known gap:** mobile (Termux) typically lacks a packaged `tor` binary — flag
  for the mobile-operator worktree; Orbot or a static Tor build would be the
  fallback there. Not a v1 blocker since the desktop/laptop hosts already have it.

---

## 6. Milestones

| Phase | Deliverable | Gate |
|---|---|---|
| **P0** | `cmd_chat/tor/onion.py` — stem wrapper; `cmd_chat.py serve --tor` prints a working `xxxxx.onion:<port>`; manual guest connect via external `torsocks` proves round-trip. | A second machine reaches the room over Tor with no inbound ports open on the host. |
| **P1** | In-process SOCKS connect (`connect.py`) — `hh connect --tor` / `cmd_chat.py connect --tor`, no external `torsocks` wrapper needed. | Guest connects with one flag, no separate tool install beyond `tor`/`PySocks`. |
| **P2** | `/tor share [label]` — posts the invite into the *current* room's existing encrypted chat; `/tor stop` tears down cleanly. | Invite round-trips entirely inside hack-house chat; no external channel touched. |
| **P3** | Teardown discipline — `DEL_ONION` reliably fires on process exit/`/tor stop`/crash; test for orphaned services. | Repeated share/stop cycles leave zero live onion services behind. |
| **P4** | Optional podman-hardened `tor` process + docs. | Feature works identically with the process isolated; documented as opt-in. |

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
