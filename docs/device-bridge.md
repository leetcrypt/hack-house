# Device bridge — expose a physical device into a room as an interactive member

**Status:** P1 (pager) + P2 (flipper) shipped (2026-09-24). **Decisions locked:** first
device = WiFi Pineapple Pager; interaction model = *persona member* (curated verbs) +
raw-drive `/sbx <persona>`; home = `cmd_chat/device/` in hack-house main.

**Flipper Zero (P2) is implemented** — `adapters/flipper.py` (full multi-tool: reads +
owner-only + ARMED RF/HID) over local USB serial (`adapters/serial_conn.py` shelling to
`flipper-0/bin/flipper-cli`), and `/sbx flipper` via a dependency-free pyserial↔PTY relay
(`serial_relay.py`, no picocom/minicom needed). Registered in `bridge.py` ADAPTERS;
launches via `hh-go device flipper` / `hh-device flipper …`. USB-tethered ONLY — no
network fallback (contrast the pager's USB→WiFi→LAN auto-discovery), so "unplugged" =
genuinely offline and the presence gate reports it. `pyserial>=3.5` added to the operator
venv + `requirements-operator.txt`.

## Concept

A physical device (Pineapple Pager, Flipper Zero, …) joins a room the way the **web
publisher** does — a headless room member that bridges an *external I/O surface* in.
Members talk to it (`@pager recon scan`) and, once granted, drive it. No new frame
types: it reuses the room's `_sbx:*` / `_perm:acl` control plane end to end.

```
room  ⇄  device bridge (headless member)  ⇄  device adapter        ⇄  device
         cmd_chat/device/bridge.py            adapters/pager.py:       Pineapple Pager
         (clone of emit_sbx Broker)           shells `ssh pager` +     (USB 172.16.52.1 /
                                              pineapple-pager-c2 CLI    WiFi 172.16.42.1)
```

## The skeleton to clone — `cmd_chat/web/emit_sbx.py`

`emit_sbx.py`'s `Broker` already *is* a device bridge minus the transport: it joins as
a member, owns the sandbox surface with **no real PTY**, and enforces the ACL.

| Broker piece | Line | Reuse as-is → change |
|---|---|---|
| `send()` room-encrypts a control frame | `emit_sbx.py:53` | keep |
| `broadcast_acl()` emits `_perm:acl` | `:62` | keep |
| `_emit()` streams `_sbx:status` + `_sbx:data` | `:72` | keep the framing; **feed it real device bytes** |
| synthetic byte loop | `:84-94` | **replace** with the adapter's serial/SSH read loop |
| `_broker_recv()` gates `sender in drivers`, then echoes | `:97-130` | keep the gate; **replace echo (`:130`) with a device write** |
| `_console()` grant/grab/revoke | `:133-154` | keep (also driven by room `/grant`) |

Base member machinery it extends: `cmd_chat/client/client.py` `Client` (`:45`, SRP `:115`,
room key `:143`, `room_fernet` `:144`, `decrypt_message` `:173`, `run_async` `:514`).

## Room primitives inherited (file:line — nothing to rebuild)

- **Output → room:** `{"_sbx":"data","b64":…}` (base64 PTY bytes, Fernet-sealed). Emit
  side `app.rs:2056`; late-joiner snapshot `app.rs:1881`; parse `net.rs:239`; Python
  consumers `bridge.py:_absorb_term` `:153`, web publisher taps the same stream.
- **Input ← room, gated:** `Net::SbxInput{from,bytes}` → `if drivers.contains(from) {
  write_input }` `app.rs:1759-1764`. Non-drivers dropped silently. The bridge writes
  approved bytes to the device handle instead of a PTY.
- **Driver ACL / grant:** owner-only `/grant <name>` `app.rs:3248` → `broadcast_acl`
  `app.rs:969-976`. Kill-switch/force-grab collapses to owner + interrupts `app.rs:1420`.
  Read-only until granted. Python mirror: `bridge.py:192`, `emit_sbx.py:48,121`.
- **Two-gate external input** (for a relay/remote-fronted device): `publisher.py:1076-1114`
  — Gate A driver token + Gate B per-actor approval + replay counters.
- **Manifest / persona:** `.hh-agent` schema (`manifest.py:38`) is free-form-extensible
  (`:71-73`); a device carries a `.hh-device` variant — identity in `purpose`, wake/connect
  in `setup`, **accepted verb vocabulary in `usage`** (no schema change). The bridge reads
  it and exposes only declared verbs.

## Device prior art (do NOT reinvent — adapters shell to these)

- **`~/coding/hardware/pineapple-pager-c2/`** — staged C2 (01-connect → 02-payloads →
  03-recon → 04-wardrive-gps → 05-loot), `bin/`, `pineapple-sync.sh`, `_config/devices.env`.
  Connection via `ssh pager` (`pp-proxy.sh` auto-discovers USB/WiFi-AP/LAN, key
  `~/.ssh/pineapple-pager`). **`payloads/user/remote_access/tg_command_bot/tg_shim.sh`** is
  the interactive-prompt precedent: it replaces DuckyScript `CONFIRMATION_DIALOG`/
  `LIST_PICKER`/`NUMBER_PICKER`/… with chat-backed prompt→poll→reply. We port it to an
  `hh_shim` that targets the room instead of Telegram.
- **`~/coding/hardware/flipper-0/`** — Flipper C2 (serial `/dev/ttyACM*`, firmware/apps/
  radio, `_config/radio-legal.md`). P2.
- No serial/USB code exists in hack-house yet — transport is greenfield; everything
  downstream of "produce/accept bytes" is built.

## P1 — Pineapple Pager persona bridge (the concrete first build)

Module layout (`cmd_chat/device/`):
```
bridge.py        # DeviceBridge(Client) — clone of emit_sbx Broker; join room, own the surface
adapters/
  base.py        # DeviceAdapter: present(), verbs(), run(verb,args)->stream, arm(), stop(), health()
  pager.py       # shells `ssh pager` + pineapple-pager-c2 bin/*; verb map from the c2 command ref
shim.py          # hh_shim: room-backed CONFIRMATION_DIALOG/LIST_PICKER/… (ported tg_shim)
manifest.py      # .hh-device read/write (identity + verb vocabulary + which verbs are `armed`)
__main__.py      # `python -m cmd_chat.device up <host> <port> pager --password … --device pager`
```

Flow: bridge joins as member `pager` → posts a presence line + its verb menu (from the
manifest) → taps room chat for `@pager <verb> …` (or `/dev <verb>`) → `pager.py.run()`
shells the mapped, **allowlisted** c2 command over `ssh pager` → stdout streamed back as
`_sbx:data` (long output) + a chat summary → artifacts land in `05-loot`. Reachability
gate up front: if `ssh pager` / `pp-proxy` can't reach the device, announce OFFLINE and
refuse verbs (both devices are offline right now).

Curated verb vocabulary (P1 seed, from the c2 command reference — expand via manifest):
`status · recon scan [ssid] · clients · loot list · loot pull <id> · payload list ·
payload run <name>`. **Armed (require `@pager arm` + scope ack first):** anything that
transmits/deauths/injects.

## Safety model

Inherited: driver-token ACL, owner-only grant, kill-switch, read-only default, room-key
encryption. Added for hardware:
1. **Presence gate** — probe reachability, announce online/offline, refuse when down.
2. **Arm gate** — RF/TX/deauth/inject verbs need explicit `@pager arm` + a scope/legal ack;
   auto-disarm on timeout or `@pager stop`.
3. **Bridge confinement** — the bridge execs *only* allowlisted adapter argv (never a host
   shell); a room member cannot pivot device-verb → trillsec-host-command. Runs least-priv.
4. **Scope + audit** — owned/in-scope targets only (security CLAUDE.md); every verb logged
   (who/verb/device/result) to the c2 `logs/` and the room transcript.

## Phasing

- **P0** foundations — `cmd_chat/device/` scaffold from `emit_sbx.py`; `DeviceAdapter` base;
  presence probe; `.hh-device` manifest; `pyserial` into the venv (for P2).
- **P1** pager persona (this doc) — join, curated verbs over `ssh pager`, output→chat+loot.
- **P2** flipper serial adapter + arm gate for RF/HID (subghz/nfc/rfid/ir TX + BadUSB `hid`).
  ✅ shipped 2026-09-24 (`adapters/flipper.py` + `adapters/serial_conn.py` + `serial_relay.py`).
- **P3** interactive `hh_shim` — payload dialogs/pickers surface in the room.
- **P4** raw-drive model — device shell as an `_sbx:data` stream driven via the keystroke
  relay, gated by driver-ACL + arm (device-as-`/sbx`).
- **P5** manifest-declared vocab, multi-device rooms, `hh-device` operator skill; optional
  Rust `/sbx device` ergonomic entry (`Backend::Device`, ~10 `match` arms copying `Local`).

## Next build — SSH transport layer, dynamic payloads, `/sbx pager`

P1 shipped hardcoded verbs over one-shot `ssh pager`. Next: make the SSH infra
first-class, discover payloads dynamically, add file transfer, and expose the pager
as a driveable `/sbx`. All three reuse the existing `pineapple-pager-c2` connection +
sync infra rather than reinventing it.

### A) SSH transport layer — `cmd_chat/device/adapters/ssh_conn.py`

A reusable connection object for any SSH-fronted device; the pager adapter uses it
instead of ad-hoc `ssh` argv.

- **Connection multiplexing.** The `pager` alias re-runs `pp-proxy.sh` (USB→WiFi→LAN
  auto-discovery) on *every* connect — slow, and it flaps (seen live: "pp-proxy: Pager
  unreachable" mid-session). `~/.ssh/config` already defines `pager-usb`/`pager-wifi`
  with `ControlMaster auto` + `ControlPersist 300`. `SshConn` opens ONE master
  (`ssh -M -S <sock> -o ControlPersist=300 pager …`), then every `exec`/`scp` rides
  `-S <sock>` → sub-second, one discovery per session. Master death ⇒ presence flips
  offline and re-dials.
- **API:** `exec(argv, timeout) -> AsyncIterator[str]` (today's `stream_exec`, over the
  master); `push(local, remote)` / `pull(remote, local)` via `scp -o ControlPath=<sock>`;
  `pty() -> (reader, writer)` an interactive `ssh -tt` channel for `/sbx pager` (§C);
  `transport()` → which of USB/WiFi/LAN is live (`pp-connect status`); `health()`.
- **Auth:** key `~/.ssh/pineapple-pager` (`IdentitiesOnly`) — NOT the `sshpass`/`gopass`
  path `pineapple-sync.sh` uses; the key alias is non-interactive and CI-safe.

### B) Dynamic payload discovery + file transfer

Kill the hardcoded `payloads`/`run`. The runnable set is DISCOVERED from two sources and
refreshed on demand — a payload pushed to the device shows up automatically.

- **Device-side (installed / runnable):** `SshConn.exec` a discovery script that walks the
  device payload roots (`/root/payloads`, pineapple module dirs), and for each emits
  `name\tdesc` — desc parsed from the DuckyScript header (`# Title:` / `# Description:`)
  or a sidecar `payload.json`. (Layout confirmed device-specific; the walker probes the
  real roots at runtime rather than assuming `<dir>/payload.sh`.)
- **Host library (available to push):** enumerate `pineapple-pager-c2/{staging,payloads}/`
  — what `push` can send.
- **The discovered set IS the allowlist.** `run <name>` / `push <name>` validate `<name>`
  against the live discovered set (still ARMED for `run`); nothing outside it executes.
  Cache with a TTL; `payloads --refresh` re-scans.
- **New verbs:** `payloads` (dynamic list, device+host, tagged installed/available),
  `payload info <name>`, `push <name>` (host→device, shells `pineapple-sync push`),
  `pull [name]` (device loot→`~/loot/pineapple` + the c2 `05-loot`, shells
  `pineapple-sync pull`), and owner-only, path-validated `get <remote>` / `put <local>
  <remote>` for arbitrary transfer. Reuse `pineapple-sync.sh` where it already does the job
  (`push`/`pull`/`loot`) — the adapter shells to it; net-new is only the discovery walk +
  the room plumbing.

### C) `/sbx pager` — the pager as a driveable sandbox

Two levels; ship Level 1 first.

- **Level 1 — Python bridge PTY-over-SSH (no Rust change).** The device bridge, on an
  owner `@pager shell` / a `/sbx pager` summon, opens `SshConn.pty()` (`ssh -tt pager`)
  and runs the raw-drive loops (clone of `emit_sbx` `_emit`/`_broker_recv`): stream the
  PTY as `{"_sbx":"data"}`, accept `{"_sbx":"input"}` ONLY from `drivers`. The pager shell
  then renders in the room's sandbox pane and is driven by the keystroke relay — the P4
  raw-drive model, pager-specific. Ships entirely in Python.
- **Level 2 — native `/sbx pager` in the TUI (`Backend::Device`).** Add a `Device` variant
  to `hh/src/sbx.rs`: `command_for` returns `ssh -tt pager` (or a device-launch wrapper),
  and the existing `Sandbox::launch` PTY machinery streams `_sbx:data` + drives via the
  keystroke relay + `/grant`, unchanged. Container-only arms (`prepare`/`teardown`/egress)
  copy the `Backend::Local` no-op. `/sbx pager` then works from a bare TUI with no bridge
  running. ~10–12 `match` arms (compiler enumerates them).
- **Safety — a raw device shell bypasses the curated allowlist, so gate it harder than the
  persona:** owner-only summon; driver-ACL for input (inherited); an explicit device `arm`
  for the room before a shell opens; full keystroke audit; a `disarm`/kill that closes the
  PTY. Framing: the pager is REAL hardware, not a disposable container — treat `/sbx pager`
  like production infra (authorized operators, scoped, RF-legal), distinct from `/sbx podman`.

**Build order:** A (SshConn + multiplex) → B (dynamic discovery + push/pull/get/put) →
C-Level-1 (bridge PTY `/sbx pager`) → C-Level-2 (native `Backend::Device`). A is the
dependency for both B and C.

## Connecting from any hosting mode (local / web-relay / onion)

The bridge is a first-class room **member**, not a viewer — it joins the ordinary
`cmd_chat.py serve` **loopback** room server directly (127.0.0.1:&lt;port&gt;), exactly like
the host's own TUI. Web-relay and onion only *front* that same loopback server, so the
device connects identically regardless of exposure. **Start the room first, then add the
device** (`hh-go up | --relay | --public | onion | onion browser` → then `hh-go device pager`).

`hh-go device [pager]` **auto-discovers** the active room across all modes: it finds the
running `cmd_chat.py serve` process, reads the port from its cmdline, and the password from
either the cmdline (`-p`/`--password`, e.g. onion/explicit) or the process environment
(`CMD_CHAT_PASSWORD`, the `hh-up.sh` `--relay`/`--public` path), with the onion state file as
a fallback. Overrides: `HH_ROOM_PORT` + `HH_ROOM_PASSWORD` (target a specific room),
`HH_DEVICE_OWNER` (your room name; default `$USER`), `HH_DEVICE_ALIAS` (ssh alias). For a
room on another host / explicit coords, use `scripts/hh-device.sh <device> <host> <port> <pw>`.

## Wireless Flipper — future transport options (P3, deferred 2026-09-24)

The shipped Flipper adapter drives the device's **text serial CLI** over USB
(`/dev/ttyACM0`); it is tethered-only. To reach the Flipper WITHOUT a cable to
`trillsec` while keeping the same room UX, the transport is what changes — the verb
surface + `/sbx flipper` stay identical for options 2 & 3. Three paths, ranked:

1. **Remote tethered proxy over Tailscale/SSH — RECOMMENDED (full CLI, ~no new HW).**
   Wire the Flipper to any always-on host near it (Raspberry Pi, mini-PC, or a **phone in
   Termux via USB-OTG**) and drive `flipper-cli` on that host over SSH — the pager's exact
   pattern. Reuse `adapters/ssh_conn.py` (`SshConn`): a hybrid Flipper adapter shells verbs
   via `SshConn.exec` instead of local `stream_exec`, and `/sbx flipper` opens `SshConn.open_pty`
   (`ssh -tt … 'flipper-cli shell/serial'`) instead of the local serial relay. "Wireless" from
   the room's POV; unlimited range. Effort: ~1 hr, no new hardware if a Pi/phone is available.
2. **ESP32 WiFi UART bridge on the GPIO (full CLI, extra HW).** Flash an ESP32 (the official
   WiFi Dev Board or any ESP32) with a UART↔TCP bridge (esp-link / serial-over-TCP firmware);
   the Flipper CLI then appears as a TCP socket. Adapter change is minimal: `SerialConn` opens a
   TCP socket to `esp32-ip:port` instead of the local tty — every verb + the relay work unchanged.
   Cost: an ESP32 (~$8–30) + one flash. Caveat: that board is also the Marauder/WiFi board — may
   contend if you want both.
3. **Native Bluetooth LE (no HW, PARTIAL coverage, big rewrite).** The Flipper's built-in BLE
   (STM32WB55) speaks the **protobuf RPC** API (Storage / App-launch / Gui input+screenshot /
   System) — NOT the text CLI. You'd get file push/pull/list and "launch a saved SubGHz/NFC/
   BadUSB via the app loader" + GUI drive, but NOT the raw CLI verbs 1:1. Needs a whole new RPC
   transport (pyflipper / flipperzero-protobuf) and re-expressing verbs as RPC calls — a different
   adapter, not a config swap. ~10 m range, one controller at a time.

**Design implication:** before adding any of these, refactor `SerialConn` into a small transport
interface (`local-serial | ssh-serial | tcp-serial`) so options 1–2 slot in without forking the
adapter. Option 3 is a separate `FlipperRpcAdapter`.

## Open items
- `.hh-device` manifest field names (reuse `.hh-agent`'s `purpose/setup/usage/state` vs a
  dedicated schema) — decide at P0.
- Whether P1's verb menu is hardcoded seed vs read from the c2 project's command reference.
- Persona naming + whether the bridge user is visible (yes for persona model) vs hidden.
- Wireless Flipper transport — pick option 1/2/3 above (see the P3 section).
