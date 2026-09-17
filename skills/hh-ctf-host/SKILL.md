---
name: hh-ctf-host
description: Host a CTF tournament in a live hack-house room — generate/bank challenges with ctfgen, raise the room's capacity with the ctfhost tournament profile, distribute one sandbox per challenge, run flag submission + a live scoreboard through room chat, and ingest telemetry into ctfscore's leaderboards. Use when asked to run/host a CTF, stand up a challenge room, or turn the ctf-bench research modules into an operated event.
---

# hh-ctf-host — operate a CTF tournament through a hack-house room

The `research/ctf-bench` worktree already has a working CTF harness — challenge
generation (`ctfgen`), a room-capacity profile for many players (`ctfhost`), a
match orchestrator (`ctfarena`), and a SQLite scoring/leaderboard store
(`ctfscore`) — proven on a real P0 dataset. It's a **science harness** (kept
deliberately out of the shipping client, same posture as `vm-escape-study`), built
for instrumented measurement, not for a human/AI tournament night. This skill is
the missing operating layer: how to point that harness's outputs at a **live room**
so players join, pull a challenge sandbox, submit flags in chat, and watch a
scoreboard update — without touching the harness's measurement code. Read
**`hh-operator`** first for the room mechanics.

```bash
BENCH="${CTF_BENCH:-$HOME/coding/hack-house/work-trees/ctf-bench/research/ctf-bench}"
LAB="${CTF_LAB:-$HOME/coding/hack-house/work-trees/ctf-bench/research/ctf-lab}"
HHREPO="${HH_REPO:-$HOME/coding/hack-house/main}"
HH="$HHREPO/.venv/bin/python -m cmd_chat.operator"     # or: hh-op
cd "$BENCH"
```

## 1. Generate or draw challenges (`ctfgen`)

Reuse the bank rather than hand-authoring — seed-deterministic, self-solve-gated,
novelty-checked against what's already banked:

```bash
python3 -m ctfgen bank-status --bank "$LAB/bank/bank-v1.json"     # what's already available
# need a fresh one? admit runs gen -> self-solve -> novelty -> bank in one step:
python3 -m ctfgen admit --seed 41 --ctf-vm "$LAB/ctf-vm" \
    --bank "$LAB/bank/bank-v1.json" --split scored
```
Only ever host **`scored`**-split challenges in a real tournament; `practice`
entries (like `hh-easy-01`) are for warm-up, not the leaderboard.

## 2. Raise the room's capacity (`ctfhost`)

The shipping `cmd_chat` server caps a room at 4 users by config
(`CMD_CHAT_MAX_USERS`), not architecture — a tournament for more players is a
config overlay, not a code change. Verify the raised cap actually admits before
you invite anyone:

```bash
python3 -m ctfhost describe --max-users 16      # print the profile + fan-out trade-off
python3 -m ctfhost env --max-users 16            # -> CMD_CHAT_MAX_USERS=16 (eval-able)
eval "$(python3 -m ctfhost env --max-users 16)"
python3 -m ctfhost verify --max-users 16         # drives the REAL server gate: N-th ADMITs, (N+1)-th "Clergy full"
```
Only launch the tournament server after `verify` prints `VERIFY: PASS` — an
unverified cap fails silently as "room full" mid-event, which is a worse failure
than catching it before doors open.

## 3. Stand up the room + one sandbox per challenge

Each challenge is its own disposable box — don't let players share a container,
that's a cheating vector and an availability risk if one player wedges it:

```bash
cd "$HHREPO"
$HH up 127.0.0.1 <port> ctf-host --password <pw> --no-tls   # CMD_CHAT_MAX_USERS from §2 in env
for CH in gen-enum-0041 gen-enum-0137 gen-enum-0288; do
  # per hh-loop's isolation pattern: unique label/container per challenge
  podman run -d --name "ctf-$CH" --network=none --pids-limit=256 --memory=512m \
    "$(cat "$LAB/ctf-vm/$CH/image.txt" 2>/dev/null || echo hh-dev:slim)" sleep infinity
done
$HH say "Tournament open: 3 challenges (gen-enum-0041/0137/0288). /sbx pull or ask the \
host to grant a per-challenge box. Submit flags in chat as: FLAG <challenge> <flag>."
```
`--network=none` per box unless a challenge's `CONTRACT.md` specifically calls for
network — same default-deny posture `hh-redteam` uses for its own boxes.

## 4. Score submissions live, through the room

Flags are graded by the challenge's own `grade` step (per `ctf-lab/CONTRACT.md`'s
plant/grade/solve contract) — **never** by a player's self-report, exactly like
`ctfarena.match`'s rule that `solved` comes from the in-box grader, not the
runner's claim:

```bash
$HH read --wait --timeout 30      # watch for "FLAG <challenge> <guess>" lines
# on a submission: run the challenge's own grader inside its box, not a string-eq in chat
podman exec "ctf-$CH" /ctf/grade.sh "<guess>"    # per CONTRACT.md's grade interface
$HH say "CORRECT: <player> solved gen-enum-0041 (depth 2)"   # or: "no — try again"
```

## 5. Ingest telemetry, publish the leaderboard (`ctfscore`)

Each match/submission should land a `run.json` (§7.5 schema) — if a player is
being scored by the instrumented harness (not just a casual chat submission),
route them through `ctfarena` proper so the telemetry format matches what
`ctfscore` expects:

```bash
python3 -m ctfarena match --challenge "$LAB/ctf-vm/gen-enum-0041" \
    --runner reference --player <name> --model <n/a-for-human> --out telemetry/
python3 -m ctfscore ingest telemetry/ --db "$BENCH/ctf-score.db"
python3 -m ctfscore leaderboard --db "$BENCH/ctf-score.db"
$HH say "Leaderboard: $(python3 -m ctfscore leaderboard --db "$BENCH/ctf-score.db" | head -5)"
```
For a casual/human tournament without full instrumentation, a simpler tally kept
in the room (or a plain dict scored off §4's grade results) is fine — reach for
`ctfscore`'s IRT/Elo boards when you actually want a difficulty-adjusted or
head-to-head ranking, not for a one-off scoreboard.

## 6. Teardown

```bash
for CH in gen-enum-0041 gen-enum-0137 gen-enum-0288; do podman rm -f "ctf-$CH"; done
$HH down
```

## Safety

- Every challenge box is disposable, `--network=none` by default, and torn down
  after the event — same posture as `hh-redteam`'s empty boxes.
- Grade with the challenge's own grader, never a chat self-report — a false claim
  should read as a wrong submission, not a win.
- Only host `scored`-split bank entries in a real event; keep `practice` entries
  out of the leaderboard.
- Verify the raised room capacity (`ctfhost verify`) before inviting players —
  don't discover the cap is wrong once people are already trying to join.
