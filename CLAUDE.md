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

## Key components

| What | Where |
|------|-------|
| Operator bridge (daemon + CLI) | `cmd_chat/operator/__main__.py`, `bridge.py` |
| Agent manifest (`hh-agent/v1`) | `cmd_chat/operator/manifest.py` — `.hh-agent/` bundle library + CLI |
| Recursive spawn (Claude-spawns-Claude) | `cmd_chat/operator/bootstrap.py` (budgeted: `--depth`/`--fanout`) |
| Saved-VM registry / discovery | `hh/src/registry.rs` → `~/.hh/registry.json`; reader: `$HH registry list\|show` |
| Sandbox grammar (TUI) | `/sbx podman\|launch`, `/grant <name>`, `/sbx save`, `/sbx browse`, `/sbx publish`, `/sbx catalog @user`, `/sbx pull @user <label>` |
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
