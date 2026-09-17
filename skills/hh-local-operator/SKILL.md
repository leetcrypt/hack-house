---
name: hh-local-operator
description: Bridge a local Ollama model (via local-ai's `ai` harness×model dispatcher) into a hack-house room as a first-class operator — not the one-shot `/ai <name> !<task>` delegate — carrying a persistent brain (trillsec-memory / brain-bootstrap) that survives across room joins, separate from the per-VM `.hh-agent` manifest. Use when a local model should join/read/say/drive a room like a Claude operator, when a recurring local operator identity needs to remember past rooms, or when asked to give a room participant real memory instead of a cold start each session.
---

# hh-local-operator — a local model as a room member with a memory, not a one-shot reply

A room's `/ai <name>` is a **stateless one-shot delegate**: `say "/ai bot !task"`,
read one reply, done. It never joins the roster, never drives the sandbox, never
remembers the last time it was asked something. This skill is the other end of
that spectrum: a local Ollama model, run through `local-ai`'s harness×model
dispatcher, joins a room the same way a Claude operator does — `up`, `read --wait`,
`say`, and (when granted) `exec`/`keys` — and carries a **persistent brain** across
sessions via `trillsec-memory` / `brain-bootstrap`, so a recurring identity like
"hh-recon-bot" remembers past rooms instead of starting cold every join. Read
**`hh-operator`** first for the room doctrine this wraps; this skill only changes
*who's driving the CLI*, not the CLI itself.

```bash
HHREPO="${HH_REPO:-$HOME/coding/hack-house/main}"
HH="$HHREPO/.venv/bin/python -m cmd_chat.operator"     # or: hh-op
LOCALAI="${LOCAL_AI:-$HOME/coding/local-ai}"
```

## 0. Two handoff layers — don't conflate them

`hh-operator`'s `.hh-agent` manifest already handles **work handoff**: what was
done *in this sandbox*, so the next agent (human or AI) can resume. That's
VM-scoped and stays that way — don't route identity memory through it.

This skill adds a second, orthogonal layer: **agent handoff** — what *this
operator identity* has learned across *many* rooms and VMs, carried in its own
brain via `ai brain export/import`. A local operator should pull both when it
joins: the room's `.hh-agent` for "what's this box for," and its own brain for
"what have I learned about hack-house rooms/targets/people in general."

## 1. Preflight — confirm the harness can actually drive `$HH`

`ai` sandboxes by default (filesystem confinement; see `local-ai/CLAUDE.md`).
Operating a room needs outbound websocket to the relay and, if granted,
`podman exec` into a sandbox — **verify these reach through the sandbox before
trusting a run**, don't assume:

```bash
cd "$HHREPO"
"$LOCALAI/scripts/ai" run goose qwen2.5-coder:7b -t \
  'run: python3 -m cmd_chat.operator status' 2>&1 | tail -5
```
If it can't reach the relay or exec podman, drop to `--no-sandbox` for the
operator run specifically — the blast radius is still the room's own sandbox
(same "one bad action's blast radius is the container" posture as `hh-operator`),
so this is a scoping call, not a safety regression, but confirm it rather than
guessing.

## 2. Give the local model the operator doctrine as its task

The harness is the agent loop; the model is the weights; `hh-operator`'s SKILL.md
is the system prompt neither one has by default. Paste the doctrine (or point at
the file) into the task:

```bash
"$LOCALAI/scripts/ai" run goose qwen2.5-coder:7b --no-sandbox -t "
You are joining a hack-house room as operator 'hh-recon-bot'. Use the
cmd_chat.operator CLI (run from $HHREPO, venv-pinned) exactly per its doctrine:
up once, then loop read --wait -> think -> say/exec -> read --wait. Join:
  cd $HHREPO && .venv/bin/python -m cmd_chat.operator up <host> <port> hh-recon-bot --password <pw> --no-tls
Objective: <task>. Stop when told 'stop'/'that's all', or your objective is met,
then: .venv/bin/python -m cmd_chat.operator down
"
```
A weaker/cheaper local model is well suited to **narrow, well-specified** room
roles (a recon delegate, a scoreboard bot for `hh-ctf-host`, a watcher for
`hh-ir-watch`'s flag patterns) — per `brain-bootstrap`'s own honest-gaps note,
local-model judgment quality on open-ended synthesis is unmeasured against
Claude's. Scope the objective tightly; don't hand a 3B model an open-ended
planner role `hh-loop` would give a Claude operator.

## 3. Give the identity a brain that survives across rooms

**First time this identity operates:** bootstrap it a real brain instead of
starting from nothing —
```bash
cd "$LOCALAI"
scripts/ai bootstrap --topic "hack-house room operations and recon triage" \
  --brain hh-recon-bot --model qwen2.5-coder:7b
# → identity.json + ICM workspace + a research plan; --hybrid pauses for web research
# (Claude-only per brain-bootstrap's own contract) — hand that phase to a Claude
# session, or run --checkpoint and hand-seed early memories if this needs to stay local-only.
```

**Every session after:** pull the brain in before joining, push what it learned
out after leaving — the same export/import surface `ai brain` already provides:
```bash
scripts/ai brain export hh-recon-bot --out /tmp/hh-recon-bot-brain.tar   # before the room, if running elsewhere
scripts/ai brain import hh-recon-bot --in  /tmp/hh-recon-bot-brain.tar   # onto this box
# ... run the room session per §2 ...
scripts/ai brain verify hh-recon-bot        # sanity-check the store isn't corrupted before trusting it
```

**Consolidate after the session**, don't just append raw transcript — run the
same offline review the rest of the memory system uses so the brain stays
retrievable instead of growing into noise:
```bash
# from trillsec-memory, pointed at this identity's brain dir:
python3 skills/memory-dream/scripts/dream.py --dir ~/agent-workspaces/hh-recon-bot/memory
python3 skills/kg-build/scripts/kgbuild.py --dir ~/agent-workspaces/hh-recon-bot/memory --apply harvest
```
`memory-dream` stages consolidation/dedup as **drafts** — a human promotes, it
never silently rewrites the brain — and `kg-build` adds the recall layer so a
later join can ask "what do I already know about this room/target" instead of
re-deriving it. This is the concrete payoff of a brain over a one-shot `/ai`
call: a recon bot that's operated a target's room twice recalls the first pass.

## 4. Where this composes with the other hh-* skills

- **`hh-ctf-host`** — a local operator is a natural scoreboard/announcer bot: cheap,
  narrow, doesn't need Claude-grade judgment to relay `ctfscore` output into chat.
- **`hh-ir-watch`** — a local model can run the passive flag-pattern watch
  continuously at near-zero cost, escalating to a Claude operator only when
  something actually trips (RQ4 patterns are mostly pattern-matching, not
  judgment calls, so this is a good fit for a cheap always-on watcher).
- **`hh-hunt-bridge`**'s multi-operator topology — a local model can staff the
  narrow "recon" role while Claude operators handle synthesis/validation, the
  same cost-aware split `hh-loop`'s topology table already implies for humans
  choosing solo vs. planner+builder+tester.

## Safety

- Sandbox posture is a deliberate, verified choice (§1), not a default you
  didn't check — confirm what a `--no-sandbox` run can actually reach before
  trusting it with a live room's grant.
- Same containment posture as every other hh-* skill: blast radius is the
  sandbox, never local/host exec, keystrokes/exec still require an owner grant.
- Brain consolidation stays human-gated per `memory-dream`'s own contract —
  don't add `--apply` to a destructive proposal without reading it first.
- A local operator's judgment is unmeasured against Claude's (per
  `brain-bootstrap`) — scope its room role narrowly rather than trusting it with
  open-ended planning until that's actually benchmarked.
