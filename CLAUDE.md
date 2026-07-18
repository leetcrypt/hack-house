# hack-house — Claude operating guide

Encrypted collaborative terminal sessions with a summoned, shareable sandbox.
**Rust TUI client** (`hh/`) + **Python server & operator bridge** (`cmd_chat/`).

A Claude Code session can join a room as a **first-class member**: read/answer
humans and other AIs, and — once granted — drive the shared sandbox with full
shell + keystroke control. VMs carry a self-describing `.hh-agent` manifest so
work + memory hand off between agents (and humans), and saved VM states are
tradeable via a host-global registry.

## Operating a room — use the `hh-operator` skill

To enter/run/watch a room, bridge a Claude session into one, or autonomously
monitor and respond, **use the `hh-operator` skill** (`~/.claude/skills/hh-operator/`).
It documents the full doctrine: the daemon + `hh-bridge` CLI, the read→think→act
loop, sandbox drive (exec/write/get), the keystroke relay + stop-vocabulary, the
`watch` stop-condition engine, and the `.hh-agent` manifest for handoffs.

```bash
# Run from this repo root; pin the interpreter to the venv.
HH=".venv/bin/python -m cmd_chat.operator"     # = the hh-bridge CLI
$HH up <host> <port> <name> --password <pw> --no-tls   # join (spawns the daemon)
$HH read --wait --timeout 30                            # long-poll for activity
$HH say "…"                                             # chat into the room
$HH keys "make build" enter ; $HH watch --for 'PASS|FAIL' --in screen   # drive
$HH manifest push --root /root --name <vm> --purpose … --objective …    # handoff
$HH spawn "<objective incl. the full join cmd>" --room-host … --go --skip-permissions
```

## Mobile operator on Android / Termux — the phone is REACHABLE, use it

Spec: `docs/termux-operator.md`. The operator side is pure Python and runs on
the **Fairphone-6** in Termux — it can both **join** and **host** rooms (the SRP
shim now covers the server side too, so no Rust / no `srp` C-ext). You can drive
the device directly over SSH — so **verify reachability before ever telling the
user "this can only run on-device, paste it back."** That framing is wrong when
the phone answers SSH.

One canonical, hardened invocation (use this prefix everywhere — it sidesteps
the wrong-alias trap and never hangs):
```bash
PHONE='ssh -i ~/.ssh/phone-deploy -p 8022 -o ConnectTimeout=8 -o BatchMode=yes -o StrictHostKeyChecking=accept-new u0_a203@100.95.202.68'
$PHONE 'uname -m; python --version'    # aarch64 / Python 3.13.x
```

- **Reachability gate — probe the PORT, not `tailscale status`:**
  `timeout 8 bash -c 'cat </dev/null >/dev/tcp/100.95.202.68/8022' && echo REACHABLE || echo OFFLINE`.
  Tailscale reports `fairphone-6` **`active` even when sshd is dead** — Android
  kills backgrounded Termux, taking `sshd` with it. The tailnet line is NOT
  proof; the TCP probe is. If OFFLINE, the fix is **on-device** (wake the phone,
  relaunch Termux, `termux-wake-lock` + start `sshd`) — say so, don't retry a
  dead port.
- **Trap:** the `~/.ssh/config` aliases `phone` / `phone-deploy` point at
  `100.95.180.14` (`trilluminati`), a *different, usually-offline* phone —
  `ssh phone` will time out. Use the Fairphone IP `100.95.202.68` (the `$PHONE`
  prefix), not the alias.
- Termux facts: Python 3.13 (≥3.11); **no `/tmp`** — `$TMPDIR` is
  `$HOME/.claude-temp`; `$PREFIX=/data/data/com.termux/files/usr`; `rustc`/
  `openssl` not installed by default.
- Deps that work: `pkg install python-cryptography` (avoid a rust build),
  `pip install requests rich websockets`; **skip `srp`** — both client and
  server fall back to `cmd_chat/client/_srp_pure` (pure-Python SRP-6a shim).
- Push-restricted work: copy this tree to the phone with tar-over-ssh rather
  than a stale gitea clone —
  `tar czf - cmd_chat scripts requirements-operator.txt | $PHONE 'tar xzf - -C ~/hh-mobile'`.
- Phase-0 gate: `python scripts/termux-preflight.py` on the phone → expect
  `RESULT: PASS`. Operator files staged at `~/hh-mobile` on-device.
- **Proven live (2026-07-07):** phone JOINS a dev-box room; the phone HOSTS a
  room the laptop joins (cross-impl SRP: laptop C-ext ↔ phone pure Verifier);
  and the stdlib mobile web console (`python -m cmd_chat.operator web`) mirrors a
  real room (terminal + keystroke drive + chat) in a mobile browser.

## Key components

| What | Where |
|------|-------|
| Operator bridge (daemon + CLI) | `cmd_chat/operator/__main__.py`, `bridge.py` |
| Agent manifest (`hh-agent/v1`) | `cmd_chat/operator/manifest.py` — `.hh-agent/` bundle library + CLI |
| Recursive spawn (Claude-spawns-Claude) | `cmd_chat/operator/bootstrap.py` (budgeted: `--depth`/`--fanout`) |
| Saved-VM registry / discovery | `hh/src/registry.rs` → `~/.hh/registry.json`; reader: `$HH registry list\|show` |
| Sandbox grammar (TUI) | `/sbx podman\|launch`, `/grant <name>`, `/sbx save`, `/sbx browse`, `/sbx publish`, `/sbx catalog @user`, `/sbx pull @user <label>` |
| Session music (TUI) | `hh/src/music.rs` + `/music play <album>\|stop\|next\|random\|import <path>`. Bundled CC-BY albums `crypt`/`terminal` under `hh/music/`; plays via external ffplay/mpv/cvlc. Licensing: `docs/music-licensing.md` |
| Server | `cmd_chat.py serve <host> <port> --password <pw> [--no-tls]` |
| Rust client | `hh/target/debug/hack-house connect <host> <port> <name> --password <pw>` |

## Demo videos — use the `video-toolkit`

hack-house feature reels are produced with `~/coding/video-toolkit` (headless
asciinema-in-tmux capture via `bin/tmux-demo.py`, then a `video-forge` cut).

- **Flagship reel — three Claudes, one shared VM:** `scenarios/hh-vmhub.json`
  (capture) + `scenarios/hh-vmhub-cut.json` (forge cut). A planner, builder, and
  tester (each a real nested `claude -p`) collaborate to build **VMHub** — a
  GitHub-for-VMs web app — live inside one shared sandbox, then save it to the VM
  library. Role task-scripts: `scenarios/vmhub-{builder,tester}-task.sh`; staged
  app under `scenarios/assets/vmhub/`.
- A reel must **show the product**: the Rust TUI room (roster, encrypted chat, the
  shared sandbox F2 view) with Claude visibly operating as a member and
  Claude-spawning-Claude — not just a console of CLI calls.

> Provisioning note: the demo summons a pre-baked `localhost/hh-dev:slim` (python
> 3.11) podman sandbox so VMHub (stdlib, zero pip deps) runs with no installs.
