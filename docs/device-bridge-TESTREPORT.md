# Device bridge — test report (Pineapple Pager)

**Date:** 2026-09-17 · **Branch:** `feat/device-bridge` · **Device:** WiFi Pineapple Pager
(`mips`, OpenWrt 6.6.86), reached over the `pager` ssh alias (`pp-proxy` USB/WiFi/LAN).
All tests run against the **real device** through **real hack-house room sessions**
(loopback SRP rooms, multiple members), not mocks.

## Result: ALL PASS ✅

Consolidated live integration run (`op` = owner, `bob` = non-owner), one room:

| Leg | Verified | Evidence |
|---|---|---|
| Dynamic payload discovery | ✅ | `@pager payloads` → 51 installed (device) + host library, headers parsed |
| **Send a payload from the room** | ✅ | `@pager push hh_test_marker` → `✓ pushed` (host→device scp over the ssh master) |
| **Run a payload from the room** | ✅ | `@pager arm` + `@pager run hh_test_marker` → `hh-test-marker OK … on pager` (marker written to `/root/loot`, exit 0) |
| Authorization (non-owner) | ✅ | `bob`'s `push`/`arm`/`run` all refused (`✋ … not authorized`) |
| `/sbx pager` — shell stream | ✅ | `@pager shell` → live `root@pager:/mmc/root#` streamed as `_sbx:data` into the sandbox pane |
| `/sbx pager` — drive | ✅ | a driver's `_sbx:input` ran `echo INTEG_DRIVE_OK` on the device; output returned |
| `/sbx pager` — ACL gate | ✅ | a non-driver's keystrokes were dropped (never reached the PTY) |
| Launcher | ✅ | `scripts/hh-device.sh pager <host> <port> <pw> --owner op` → `pager` joins the roster |

## What was proven end-to-end
1. **Anyone hosting can add their pager to a room** with one command
   (`hh-device.sh` or `hh-go device pager`); it appears as `@pager`.
2. **Members send payloads from the room** — `@pager push <name>` pushes a host-library
   payload to the device; discovery keeps the pushable/runnable sets live (no hardcoding).
3. **Members run payloads from the room** — `@pager run <name>` executes an installed
   payload (ARMED + owner-gated). Proven with a **benign no-op marker payload** — zero RF.
4. **`/sbx pager`** — the raw device shell renders in the sandbox pane and is driven by the
   keystroke relay, gated by the same driver-token ACL as a podman sandbox.

## Safety posture exercised
- Read-only verbs (status/scan/clients/loot/payloads/info) open to any member.
- Device-mutating verbs (push/pull) owner-only; execution (run) + shell ARMED/owner-only,
  auto-disarm after 120s. Non-owner attempts refused (verified).
- No shell injection: fixed remote scripts; chat args token-validated; discovered set is the
  allowlist. Only a benign marker payload was fired — no deauth/handshake/inject.
- Presence gate: the bridge announces online/offline and drops commands when the device is
  unreachable (the pager flapped mid-session and the gate held).

## Transport
`ssh_conn.SshConn` opens one OpenSSH ControlMaster per session: sub-second execs, a single
`pp-proxy` discovery per session (not per command), self-healing on a flap. `push`/`pull`
ride the same master (scp); `/sbx pager` uses an `ssh -tt` PTY over it.

## Not yet done (follow-up)
- **C2 — native `/sbx pager` TUI command** (`Backend::Device` in `hh/src/sbx.rs`). C1
  delivers the full `/sbx pager` *logic* via `@pager shell` today; C2 is the ergonomic
  `/sbx pager` typed directly in a bare TUI. Tracked in `cmd_chat/device/GOAL.md`.
- Flipper adapter (serial), interactive `hh_shim` for payload dialogs.

## Reproduce
```bash
cd ~/coding/hack-house/main
.venv/bin/python cmd_chat.py serve 127.0.0.1 3200 -p test --no-tls &   # a room
./scripts/hh-device.sh pager 127.0.0.1 3200 test --owner <your-room-name> &   # the pager
# join as <your-room-name>, then:  @pager help · @pager scan · @pager push hh_test_marker
#   @pager arm · @pager run hh_test_marker · @pager shell  (then /drive)
```
