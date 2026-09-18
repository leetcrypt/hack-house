# GOAL — pager integration, fully implemented + tested in real hack-house sessions

**North star:** anyone hosting a hack-house room can bring their WiFi Pineapple Pager
into the room, **send/run payloads from the room**, and **`/sbx pager`** to drive it —
easily, safely, and proven by real end-to-end tests. Keep iterating until DONE.

Branch: `feat/device-bridge` (in `main/`). Design: `docs/device-bridge.md`.

## DONE when ALL of these are true (checkbox ledger — update each tick)

- [x] **P1 persona bridge** — `@pager status/scan/clients/loot` in a real room (done, `f4fe6e1`).
- [x] **A · SshConn** — `ssh_conn.py`: ControlMaster multiplexing (sub-second execs, one
  discovery/session), `exec/push/pull/open_pty/health/close`; pager adapter rewired. Verified
  live: health, multiplexed exec, scp push round-trip. (done)
- [x] **B · Dynamic payloads + transfer** — discovery from device (51 installed) + host
  library (headers parsed); discovered set == allowlist; verbs `payloads[ refresh]`, `info`,
  `push`(owner), `pull`(owner), `run`(armed). Verified live in a room: owner sent
  `hh_test_marker` host→device and ran it (marker written, exit 0); non-owner push/arm/run
  all refused. (done)
- [x] **C1 · `/sbx pager` (Python bridge PTY)** — `@pager shell` opens `ssh -tt pager` as an
  `_sbx:status`+`_sbx:data` stream in the sandbox pane; `_sbx:input` routed to the PTY only
  from `drivers`. Verified live: shell prompt streamed, a driver ran `HH_DRIVE_OK Linux mips`,
  a non-driver's keystrokes were dropped, `shell stop` ended cleanly. (done)
- [ ] **C2 · `/sbx pager` (native `Backend::Device`)** — `hh/src/sbx.rs` variant; `/sbx pager`
  from a bare TUI streams+drives the pager shell. Verified: `cargo build` clean + live drive.
- [ ] **D · Ergonomics** — a launcher (`hh-go device pager` or a documented one-liner) +
  `hh-device` notes so a fresh host runs it in one step.
- [ ] **E · Full integration test** — a real hack-house session: bridge joins, a member
  **sends a payload from the room** and runs it (benign), and `/sbx pager` drives it. Write
  a short PASS report at `docs/device-bridge-TESTREPORT.md`. Commit everything.

## Safety envelope (autonomous run)
- Fire ONLY benign/no-op **test** payloads (write a marker file / echo) — NEVER a real RF
  attack (deauth/handshake/inject). Create the test payload in the c2 staging.
- Honor the ARMED gate; owner-only for arm / `/sbx pager` / `put`.
- The pager is REAL hardware — if it's offline (pp-proxy flaps), build/test everything that
  doesn't need it, and retry the live legs when it's back. Do not fake a live PASS.
- Commit as each phase goes green; never push without a human gate.

## Progress log (newest first)
- 2026-09-17 — GOAL set. P1 done. Starting A (SshConn).
