---
name: hh-loop
description: Autonomously grow the hack-house VM library. Each loop execution provisions a room + sandbox, spawns 1–3 Claude operators (planner/builder/tester) to build one high-value, self-describing VM from a brief, verifies it, then saves + publishes it with canonical .hh-agent metadata. Use when asked to run /loop, mass-produce VM library contributions, or batch-build/publish hack-house VMs.
---

# hh-loop — autonomously build & publish hack-house VM-library contributions

One **loop execution** = one isolated room where a small team of Claude operators
builds **one valuable, reusable VM**, proves it works, and publishes it to the
host VM library. Many executions run in **waves** to reach a target count (e.g.
50). Each VM is a capability another agent can `/sbx pull` and use immediately.

Built on the `hh-operator` skill (join/read/act/drive a room) plus the headless
`hack-house sbx save|publish` library surface. **Read `hh-operator` first** — this
skill orchestrates several of those sessions and adds save/publish + batching.

```bash
# Run from the hack-house repo root; pin the venv interpreter.
cd ~/coding/learning/hacker-house
HH=".venv/bin/python -m cmd_chat.operator"        # the hh-bridge CLI
HHBIN="hh/target/debug/hack-house"                # the Rust client + sbx CLI
```

## What "high value" means — the brief framework

Every run is guided by the **same value rubric** so output is consistently
useful, not busywork. A VM library contribution is high-value when it is:

1. **Reusable** — a ready-to-run capability, not a one-off script. Another
   agent/human pulls it and it *just works*.
2. **Self-describing** — carries a complete `.hh-agent` manifest: `purpose`,
   `objective`, `usage[]` (how to run it), `setup[]`, and `status: done`.
3. **Verified** — the capability is demonstrated working (tests / PoC green)
   *before* save. Unverified work is not published.
4. **Distinct** — fills a real gap. **Always `registry list` first**; never
   duplicate an existing label/capability.
5. **Bounded** — prefer what's in the base image (`localhost/hh-dev:slim`,
   Kali/Parrot) so it builds fast and the artifact stays portable.

A **brief** is the per-run spec the operators receive:

```
label:       kali-recon-kit            # registry key / snapshot tag (unique!)
title:       Recon kit — one-command host+service map
mission:     A sandbox that maps a target to a tidy report in one command.
deliverable: /root/recon.sh + README; runs nmap+host enumeration, writes report.md
verify:      ./recon.sh 127.0.0.1 produces report.md with open-port section
tags:        recon, kali, security
topology:    planner+builder+tester    # see "adaptive topology" below
```

### Adaptive topology (1–3 operators, chosen by the brief)

- **solo** (1) — small, fully-specified tool. The operator plans+builds+verifies.
- **planner+builder** (2) — needs design then build; planner reviews before save.
- **planner+builder+tester** (3) — needs *independent* verification; tester must
  pass the `verify` criterion before the planner approves save+publish.

The planner is the operator you join as; it spawns the others with `$HH spawn …
--go --skip-permissions` (budgeted via `--depth/--fanout`). Review loops are
event-gated: planner `watch`es for the tester's PASS before saving.

## Run ONE brief (visible tmux is the default)

Operations run in a **visible tmux session by default** so you can watch the TUI
and the operators work; headless is an option, not the default. Use a **dedicated
tmux socket** (`-L hh-loop`) and a `hh-loop-` session prefix so teardown can never
touch your main tmux work.

```bash
RUN="hh-loop-kali-recon-kit"        # session name == run id (label-derived)
PORT=8790                            # unique per concurrent run (see isolation)
PW="loop-$(openssl rand -hex 4)"
LABEL="kali-recon-kit"

# 0. one-time per run: a private room server + the visible tmux session
tmux -L hh-loop new-session -d -s "$RUN" -x 220 -y 50
tmux -L hh-loop send-keys -t "$RUN" \
  "cd ~/coding/learning/hacker-house && .venv/bin/python cmd_chat.py serve 127.0.0.1 $PORT --password $PW --no-tls" Enter

# 1. join as the planner (spawns the bridge daemon), grant the sandbox, summon it
$HH up 127.0.0.1 $PORT planner --password "$PW" --no-tls --session "$RUN"
$HH sbx launch --image localhost/hh-dev:slim --engine podman
# (in a real room the owner /grants; for a self-owned loop room you already drive it)

# 2. stamp the VM's manifest up front (canonical metadata travels with the work)
$HH manifest push --root /root --name "$LABEL" \
  --purpose "Recon kit — one-command host+service map" \
  --objective "build recon.sh that maps a target to report.md, verified" \
  --intent "reusable recon VM for the library" --stop "verify criterion green"

# 3. spawn the team per topology (objective = brief + value rubric + save contract)
$HH spawn "Join 127.0.0.1 $PORT as builder (--password $PW --no-tls). Use the \
hh-operator skill. Build the deliverable for brief '$LABEL': <mission/deliverable>. \
Load /root/.hh-agent first. When built, say 'BUILD READY'." \
  --room-host 127.0.0.1 --room-port $PORT --room-name builder --go --skip-permissions
# (3-operator briefs also spawn a tester that must emit PASS against `verify`)

# 4. planner drives the review loop, then records done-state
$HH watch --for "PASS|verify (ok|green)" --in events --timeout 1800
$HH manifest update --root /root --status done --progress "built + verified" \
  --done "recon.sh works" --note "ready to publish"
```

### Save + publish — canonical, headless, lock-safe

This is the payoff. The new **headless** `hack-house sbx` subcommand commits the
sandbox and publishes it through the *same* registry path the TUI uses (atomic +
file-locked, so concurrent wave members never clobber each other):

```bash
# commit the running sandbox → indexed VM with its manifest summary scraped in
$HHBIN sbx save "$LABEL" --name "$LABEL" --backend podman --created-by hh-loop --local

# mark shareable + export a portable tar so peers can pull it
$HHBIN sbx publish "$LABEL" --tag recon --tag kali --tag security

# verify it landed shareable, with a real artifact
$HH registry show "$LABEL"        # purpose/status/todo scraped from the manifest
ls -la hh/hh-snapshots/hh-snap-"$LABEL".tar
```

A run is **done** only when: `verify` is green, `manifest status: done`, the
registry entry is `shareable: true`, and the `.tar` exists.

## Recording a run (`--record`)

Capture the operation for review/demos. Two channels, combinable:

- **logs** (default-cheap): tee the bridge + server output and dump the session.
  ```bash
  LOGDIR=~/coding/learning/hacker-house/.loop-runs/$RUN; mkdir -p "$LOGDIR"
  tmux -L hh-loop pipe-pane -t "$RUN" -o "cat >> $LOGDIR/tmux.log"   # live tap
  $HH read --since 0 > "$LOGDIR/events.jsonl"                        # full event log
  $HH screen > "$LOGDIR/screen.txt"                                  # final TUI frame
  ```
- **film**: record the visible TUI with the video-toolkit tmux capture
  (`~/coding/video-toolkit/bin/tmux-demo.py`) pointed at socket `hh-loop`,
  session `$RUN` → an asciinema cast / mp4. Heavier; use for showcase runs.

## Scale to a target (waves of 4–6)

The contention points are the **registry.json** (now locked) and the **podman
namespace** — *not* git (runs build inside throwaway VMs). Isolate every run so a
wave can't collide:

| Per-run knob | Make unique |
|---|---|
| room port | `8790 + i` |
| tmux session / run id | `hh-loop-<label>` |
| container + snapshot label | the brief's `label` (must be unique) |
| child config dir | `$HH spawn --config-dir /tmp/cc-$RUN` |

Drive the queue in **waves of 4–6 concurrent runs** (moderate — keeps the
CPU-bound box responsive; recall: this host is CPU-only and saturation drops
agent sockets). Pull briefs from the curated **security-focused queue** (the
`briefs/` set; one brief → one run), adaptive topology per brief, until the
target count of published, shareable VMs is reached. After each wave, reconcile:

```bash
$HH registry list        # how many shareable VMs exist now → progress toward 50
```

## Safe teardown (never touch the user's main tmux)

Everything lives on the `hh-loop` socket, so cleanup is scoped and safe:

```bash
$HH down                                   # leave the room, drop the daemon
tmux -L hh-loop kill-session -t "$RUN"     # one run
tmux -L hh-loop kill-server                # whole wave (only the hh-loop socket!)
# NEVER `tmux kill-server` on the default socket — that's the user's session.
```

Snapshots/tars persist (that's the product); prune throwaway *containers* with
`podman rm -f <label>` if a run aborted before save.

## Safety

- Blast radius of any build is its container; never run destructive host commands.
- Publish only **verified** VMs — an unverified or duplicate VM is not a
  contribution. `registry list` before every brief.
- Respect the wave cap; oversubscribing the CPU drops agent websockets (mass
  FAIL/TIMEOUT is saturation, not model failure).
- Always `$HH down` + scoped `tmux -L hh-loop kill-*` when done — no orphaned
  daemons or servers.
