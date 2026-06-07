#!/usr/bin/env bash
# film-save-load.sh — RECORD the "persistent sandbox" beat to an asciinema cast,
# then render an MP4. Sibling of demo-save-load.sh (the correctness harness):
# this one is for the camera, so it paces the beats and records a single,
# continuous take of the real flow:
#
#   launch docker sandbox  →  /ai start (fast qwen)  →  agent builds code in it
#   →  /sbx save <label>    →  Ctrl-Q quit (container purged, image survives)
#   →  fresh client         →  /sbx load <label>  →  reveal: the work is intact
#
# Recording trick (per the TUI-tmux recipe): the demo runs in an inner tmux
# session; `asciinema rec` runs in its own detached session that `tmux attach`es
# to the inner one, so it mirrors exactly what we drive with send-keys.
#
# Usage:  hh/scripts/film-save-load.sh [--keep] [--no-render]
#   --keep        leave server/sessions/container/image up afterwards
#   --no-render   stop after writing the .cast (skip the mp4 render)
set -uo pipefail

# ---- config -----------------------------------------------------------------
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
pick_port() { local p; for p in $(seq 4200 4280); do ss -ltn 2>/dev/null | grep -q ":$p " || { echo "$p"; return; }; done; echo 4173; }
PORT="${PORT:-$(pick_port)}"
PW="${PW:-malware-bless}"
LABEL="${LABEL:-buildbox}"
IMG="${IMG:-python:3.12-slim}"
CTR="hack-house"
SNAP="hh-snap:${LABEL}"
PY="$REPO/.venv/bin/python"
BIN="$REPO/hh/target/debug/hack-house"
COLS=110; ROWS=32
SRV_SESS="hhfilm-srv"     # server (not recorded)
RUN_SESS="hhfilm"         # the demo pane we drive
REC_SESS="hhfilm-rec"     # asciinema attaches here and records
OUTDIR="$REPO/docs/demo"
CAST="$OUTDIR/save-load.cast"
MP4="$OUTDIR/save-load.mp4"
CODER="qwen2.5-coder:1.5b"
NEED='0 1 1 2 3 5 8 13 21 34'

KEEP=0; RENDER=1
for a in "$@"; do
  case "$a" in
    --keep) KEEP=1 ;;
    --no-render) RENDER=0 ;;
  esac
done

GREEN=$'\e[32m'; RED=$'\e[31m'; YEL=$'\e[33m'; DIM=$'\e[2m'; RST=$'\e[0m'
step() { printf '\n%s== %s ==%s\n' "$YEL" "$*" "$RST"; }
ok()   { printf '%s  ok %s%s\n' "$GREEN" "$*" "$RST"; }
bad()  { printf '%s  XX %s%s\n' "$RED" "$*" "$RST"; }
note() { printf '%s    %s%s\n' "$DIM" "$*" "$RST"; }
FAIL=0; fail() { bad "$*"; FAIL=1; }

cleanup() {
  if [[ $KEEP -eq 1 ]]; then
    note "--keep: leaving server/sessions/container/image up."
    return
  fi
  step "cleanup"
  tmux kill-session -t "$REC_SESS" 2>/dev/null
  tmux kill-session -t "$RUN_SESS" 2>/dev/null
  tmux kill-session -t "$SRV_SESS" 2>/dev/null
  docker rm -f "$CTR"   >/dev/null 2>&1
  docker rmi -f "$SNAP" >/dev/null 2>&1
  note "removed container + $SNAP; sessions killed."
}
trap cleanup EXIT

# ---- helpers ----------------------------------------------------------------
# type into the recorded pane: literal text, a beat, then Enter (no Ctrl-U)
say() { tmux send-keys -t "$RUN_SESS" -l "$*"; sleep 0.5; tmux send-keys -t "$RUN_SESS" Enter; sleep 0.8; }
key() { tmux send-keys -t "$RUN_SESS" "$@"; }
cap() { tmux capture-pane -t "$RUN_SESS" -p 2>/dev/null; }
wait_for() { local re="$1" t="${2:-30}" i=0; while (( i < t*2 )); do cap | grep -qE "$re" && return 0; sleep 0.5; ((i++)); done; return 1; }
wait_cmd() { local t="${WT:-30}" i=0; while (( i < t )); do "$@" >/dev/null 2>&1 && return 0; sleep 1; ((i++)); done; return 1; }
runout() { docker exec "$CTR" sh -c 'cd /root && python3 fib.py' 2>&1; }

# ---- 0. preflight -----------------------------------------------------------
step "preflight"
command -v tmux >/dev/null || { echo "tmux required"; exit 2; }
command -v "$HOME/anaconda3/bin/asciinema" >/dev/null || command -v asciinema >/dev/null || { echo "asciinema required"; exit 2; }
ASCIINEMA="$( [[ -x "$HOME/anaconda3/bin/asciinema" ]] && echo "$HOME/anaconda3/bin/asciinema" || command -v asciinema )"
[[ -x "$PY" ]] || { echo "venv python missing: $PY"; exit 2; }
docker info >/dev/null 2>&1 || { echo "docker daemon down"; exit 2; }
ollama list 2>/dev/null | grep -q "$CODER" || { echo "coder model $CODER not pulled"; exit 2; }
docker image inspect "$IMG" >/dev/null 2>&1 || { echo "pulling $IMG..."; docker pull "$IMG"; }
[[ -x "$BIN" ]] || { step "building client"; ( cd "$REPO/hh" && cargo build ) || exit 2; }
mkdir -p "$OUTDIR"
ok "tools present, docker up, $CODER ready"

# clear stale state
tmux kill-session -t "$REC_SESS" 2>/dev/null; tmux kill-session -t "$RUN_SESS" 2>/dev/null
tmux kill-session -t "$SRV_SESS" 2>/dev/null
docker rm -f "$CTR" >/dev/null 2>&1; docker rmi -f "$SNAP" >/dev/null 2>&1
rm -f "$CAST"

# ---- 0b. pre-warm the coder so first-token latency on camera is short -------
step "pre-warm $CODER (off camera)"
"$PY" - "$CODER" <<'PY' 2>/dev/null || true
import sys, json, urllib.request
m = sys.argv[1]
req = urllib.request.Request("http://127.0.0.1:11434/api/generate",
      data=json.dumps({"model": m, "prompt": "ok", "stream": False}).encode(),
      headers={"Content-Type": "application/json"})
try:
    urllib.request.urlopen(req, timeout=120).read()
except Exception:
    pass
PY
ok "model warmed"

# ---- 1. server (not recorded) ----------------------------------------------
step "boot server :$PORT"
tmux new-session -d -s "$SRV_SESS" -x 200 -y 50 \
  "cd '$REPO' && '$PY' cmd_chat.py serve 127.0.0.1 $PORT --password '$PW' --no-tls 2>&1 | tee /tmp/hhfilm-server.log"
WT=20 wait_cmd bash -c "grep -qiE 'listening|running|serving|started|websocket' /tmp/hhfilm-server.log" || sleep 3
ok "server up"

# ---- 2. inner demo pane + recorder -----------------------------------------
step "open recorded pane (${COLS}x${ROWS}) and start asciinema"
# inner demo session we drive (bash, sized for the cast)
tmux new-session -d -s "$RUN_SESS" -x "$COLS" -y "$ROWS" "bash --noprofile --norc"
sleep 0.5
tmux send-keys -t "$RUN_SESS" -l "cd '$REPO'"; tmux send-keys -t "$RUN_SESS" Enter
tmux send-keys -t "$RUN_SESS" -l "clear"; tmux send-keys -t "$RUN_SESS" Enter
sleep 0.5
# recorder session: same size, just attaches to the demo session and records it
tmux new-session -d -s "$REC_SESS" -x "$COLS" -y "$ROWS" \
  "'$ASCIINEMA' rec --overwrite -c 'tmux attach -t $RUN_SESS' '$CAST'"
sleep 2
ok "recording → $CAST"

# ---- 3. title + join --------------------------------------------------------
say "echo '⛧ hack-house — ephemeral by default, persistent on demand'"
sleep 1.2
say "$BIN connect 127.0.0.1 $PORT alice --password '$PW' --no-tls"
wait_for 'alice|roster|hack-house|owner' 20 && ok "alice joined" || fail "alice never joined"
sleep 1.5

# ---- 4. launch docker sandbox ----------------------------------------------
step "launch docker sandbox"
say "/sbx launch docker $IMG"
WT=60 wait_cmd docker ps --format '{{.Names}}' --filter "name=^${CTR}$" && ok "container up" || fail "sandbox never came up"
wait_for 'summoned|sandbox|ready|online' 60 >/dev/null
sleep 1.5

# ---- 5. spawn fast qwen agent (auto-grant drive) ---------------------------
step "spawn agent (auto-grant sandbox drive)"
say "/ai start $CODER allow"
wait_for 'online|ollama|qwen' 45 && ok "agent online" || note "no online line yet"
sleep 1.5

# ---- 6. the fast model builds code in the sandbox --------------------------
# Transcription-only, ZERO-indentation task so the 1.5B coder can't break it
# through the PTY. Validate-by-running; retry once; abort before save if it
# still fails (no silent fallback in a film).
step "fast qwen writes /root/fib.py and runs it"
TASK="/ai $CODER !create /root/fib.py with exactly two lines and nothing else: line 1 is  nums = [0, 1, 1, 2, 3, 5, 8, 13, 21, 34]  and line 2 is  print(*nums)  then run it with: python3 /root/fib.py"
BUILT=0
for attempt in 1 2; do
  note "build attempt $attempt"
  say "$TASK"
  WT=180 wait_cmd docker exec "$CTR" test -s /root/fib.py
  if docker exec "$CTR" test -s /root/fib.py 2>/dev/null && runout | grep -qE "$NEED"; then
    BUILT=1; break
  fi
  note "output not yet correct; re-prompting"
  sleep 2
done
if [[ $BUILT -eq 1 ]]; then
  ok "model wrote a working /root/fib.py"
else
  fail "model never produced runnable fib.py after retries — aborting before save"
  exit $FAIL
fi
ORIG_SHA="$(docker exec "$CTR" sha256sum /root/fib.py | awk '{print $1}')"
note "fib output: $(runout)"
sleep 1.5

# ---- 7. snapshot to an image -----------------------------------------------
step "/sbx save $LABEL"
say "/sbx save $LABEL"
WT=40 wait_cmd bash -c "docker images $SNAP --format '{{.Tag}}' | grep -qx '$LABEL'" && ok "image $SNAP created" || fail "snapshot not found"
wait_for "saved|hh-snap|$LABEL" 10 >/dev/null
sleep 2

# ---- 8. close the session (Ctrl-Q purges the container) --------------------
step "quit client (Ctrl-Q → teardown purges container)"
key C-q
sleep 3
WT=20 wait_cmd bash -c "! docker ps -a --format '{{.Names}}' | grep -qx '$CTR'" && ok "container purged" || fail "container still present"
docker images "$SNAP" --format '{{.Tag}}' | grep -qx "$LABEL" && ok "image survived purge" || fail "image missing"
# prove it on camera
sleep 1
say "docker ps -a --format '{{.Names}}' | grep hack-house || echo '(no hack-house container — purged)'"
sleep 1.5
say "docker images hh-snap --format '⛧ {{.Repository}}:{{.Tag}}'"
sleep 2

# ---- 9. fresh client → load -------------------------------------------------
step "fresh session → /sbx load $LABEL"
say "$BIN connect 127.0.0.1 $PORT alice --password '$PW' --no-tls"
wait_for 'alice|roster|hack-house|owner' 20 && ok "alice re-joined" || fail "alice never re-joined"
sleep 1.5
say "/sbx load $LABEL"
WT=60 wait_cmd docker ps --format '{{.Names}}' --filter "name=^${CTR}$" && ok "container relaunched" || fail "load never started"
wait_for 'summoned|sandbox|ready|loading|online' 60 >/dev/null
sleep 1.5

# ---- 10. the reveal ---------------------------------------------------------
step "reveal: the model's code is intact"
WT=30 wait_cmd docker exec "$CTR" test -s /root/fib.py
NEW_SHA="$(docker exec "$CTR" sha256sum /root/fib.py 2>/dev/null | awk '{print $1}')"
note "original sha: $ORIG_SHA"
note "loaded   sha: $NEW_SHA"
[[ -n "$NEW_SHA" && "$NEW_SHA" == "$ORIG_SHA" ]] && ok "byte-for-byte identical — PERSISTENCE PROVEN" || fail "differs/missing after reload"
# show it on the TUI for the camera
key F2; sleep 1
say "cat /root/fib.py && echo '---' && python3 /root/fib.py"
sleep 3

# ---- 11. stop recording -----------------------------------------------------
step "stop recording"
tmux kill-session -t "$RUN_SESS" 2>/dev/null   # attach exits → asciinema writes the cast
sleep 2
[[ -s "$CAST" ]] && ok "cast written: $CAST ($(du -h "$CAST" | cut -f1))" || fail "cast not written"

# ---- 12. render -------------------------------------------------------------
if [[ $RENDER -eq 1 && $FAIL -eq 0 && -s "$CAST" ]]; then
  step "render mp4"
  "$REPO/../../video-toolkit/bin/cast2mp4.sh" "$CAST" "$MP4" --font-size 28 --theme dracula \
    || bash ~/coding/video-toolkit/bin/cast2mp4.sh "$CAST" "$MP4" --font-size 28 --theme dracula
  [[ -s "$MP4" ]] && ok "mp4: $MP4 ($(du -h "$MP4" | cut -f1))" || fail "render produced no mp4"
fi

# ---- summary ----------------------------------------------------------------
step "result"
if [[ $FAIL -eq 0 ]]; then
  printf '%sFILM OK%s — cast=%s%s\n' "$GREEN" "$RST" "$CAST" "$( [[ -s "$MP4" ]] && echo "  mp4=$MP4" )"
else
  printf '%sFILM FAIL%s — inspect %s\n' "$RED" "$RST" "$CAST"
fi
exit $FAIL
