# PoC: persistent sandbox — fast-qwen build → save image → close → reload

**Goal of the video beat:** prove that a hack-house Docker sandbox is *durable
on demand*. A local, CPU-only **fast qwen coder** writes & runs code inside an
ephemeral Docker sandbox; we snapshot it to an image with `/sbx save`; we **fully
close the session** (container is purged on teardown); we relaunch the client and
`/sbx load` the snapshot — the code the model wrote is **still there**.

This is the headline pitch: *sandboxes are RAM-only/ephemeral by default, but you
can freeze a moment of work into an image and thaw it later — nothing leaks to the
server, the image lives only on the owner's box.*

## Why this is non-obvious / worth showing

- `/sbx stop` and client-quit both run `sbx::teardown` → `docker rm -f hack-house`.
  The container is **gone**. Normally the work would be gone too.
- `/sbx save <label>` runs `docker commit hack-house hh-snap:<label>` *while the
  container is alive*. The image is independent of the container, so it survives
  the purge.
- `/sbx load <label>` runs a **fresh** container from `hh-snap:<label>` — same
  filesystem state, new ephemeral instance.

## Models (CPU-only box: i5-8350U, no GPU)

| Path | Model | Why |
|------|-------|-----|
| chat (`/ai <q>`) | `qwen2.5:3b` | general, the locally-pulled default |
| sandbox `!task` | `qwen2.5-coder:1.5b` | auto-selected coder; fast TTFT on CPU, better shell/code |

The agent auto-selects the coder build for the `!task` (sandbox-driving) path when
the chat provider is Ollama and a `qwen2.5-coder` is present (it is — pulled).

## Storyboard (the cut)

1. **Title card** — "Ephemeral by default. Persistent on demand."
2. **Summon** — alice: `/sbx launch docker` → "summoned" sandbox bubble.
3. **Spawn the coder** — alice: `/ai start` → `qwen2.5:3b (ai) online — ollama/qwen2.5:3b`
   (the coder model rides along for `!task`).
4. **Build, by the fast model** — alice:
   `/ai qwen2.5:3b !write /root/fib.py that prints the first 10 Fibonacci numbers, then run it`
   → agent drives the shared shell; `fib.py` is written and executed; the
   sandbox pane shows the Fibonacci output.
5. **Freeze it** — alice: `/sbx save buildbox` →
   `† saved sandbox → image hh-snap:buildbox · reload with /sbx load buildbox`.
6. **Walk away** — alice: `/sbx stop` (or quits the client entirely). Container is
   purged; prove it: `docker ps -a` shows no `hack-house`, but
   `docker images hh-snap` still lists `buildbox`.
7. **Come back** — a *fresh* client session; alice: `/sbx load buildbox`.
8. **The reveal** — F2 to drive, `cat /root/fib.py && python3 /root/fib.py` →
   the model's code and output are exactly as left. **Persistence proven.**
9. **Result card** — "OPERATIONS CONDUCTED": built by local qwen-coder · saved to
   image · session closed · reloaded intact.

## Acceptance (what the PoC script asserts)

- After step 4: `docker exec hack-house cat /root/fib.py` is non-empty AND running
  it prints 10 Fibonacci numbers (`0 1 1 2 3 5 8 13 21 34`).
- After step 5: `docker images hh-snap --format '{{.Tag}}'` contains `buildbox`.
- After step 6 (stop): `docker ps -a --format '{{.Names}}'` has **no** `hack-house`;
  the `hh-snap:buildbox` image still exists.
- After step 7-8 (load): the **new** `hack-house` container's `/root/fib.py`
  matches the original byte-for-byte.

## Execution

`hh/scripts/demo-save-load.sh` drives the whole thing headlessly over tmux (per the
TUI-tmux test recipe): boots the server, runs client **session A**, injects the
beats with `send-keys`, verifies via `capture-pane` + `docker exec`, then quits
session A and opens client **session B** to load and confirm. It is a PoC /
correctness harness first; once green it feeds the polished `video-toolkit`
render.

### Gotchas baked into the script

- TUI doesn't bind Ctrl-U (it inserts a literal `u`); clear input with `BSpace`.
  Send text with `send-keys -l "<text>"` then a separate `Enter`; don't race renders.
- Agent joins under its model tag (e.g. `qwen2.5:3b`); only one `/ai start` per client.
- Keep `!task` phrasing single-line; the agent's drive output lands in the sandbox
  pane, not chat.
- `/sbx load` refuses if a sandbox is already running — stop first.
- Docker daemon must be up (`docker info`); `/sbx launch docker --start` can boot
  it (sudo) but we pre-check instead.
- Snapshot label charset: alphanumerics, `.`, `_`, `-` (≤64).

### Teardown / cleanup

The script removes the `hack-house` container and (optionally) the `hh-snap:*`
demo images it created, and kills the server + tmux sessions, so reruns are clean.
