# Device bridge — expose a physical device into a room as an interactive member

**Status:** design (2026-09-17). **Decisions locked:** first device = WiFi Pineapple
Pager; first interaction model = *persona member* (curated verbs); home =
`cmd_chat/device/` in hack-house main. Raw-drive + Flipper come later.

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
- **P2** flipper serial adapter + `radio-legal` arm gate.
- **P3** interactive `hh_shim` — payload dialogs/pickers surface in the room.
- **P4** raw-drive model — device shell as an `_sbx:data` stream driven via the keystroke
  relay, gated by driver-ACL + arm (device-as-`/sbx`).
- **P5** manifest-declared vocab, multi-device rooms, `hh-device` operator skill; optional
  Rust `/sbx device` ergonomic entry (`Backend::Device`, ~10 `match` arms copying `Local`).

## Open items
- `.hh-device` manifest field names (reuse `.hh-agent`'s `purpose/setup/usage/state` vs a
  dedicated schema) — decide at P0.
- Whether P1's verb menu is hardcoded seed vs read from the c2 project's command reference.
- Persona naming + whether the bridge user is visible (yes for persona model) vs hidden.
