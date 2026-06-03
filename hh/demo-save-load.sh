#!/usr/bin/env bash
# demo-save-load.sh — PoC harness for the "persistent sandbox" video beat.
#
# Flow (see docs/demo-save-load-poc.md):
#   session A: /sbx launch docker → /ai start → /grant oracle →
#              /ai oracle !build fib.py & run it  →  /sbx save buildbox  → quit
#   prove:     container purged on quit, but hh-snap:buildbox image survives
#   session B: fresh client → /sbx load buildbox → the model's code is intact
#
# Headless: drives the ratatui client over tmux send-keys, asserts via
# capture-pane + `docker exec`. PoC/correctness first; feeds video-toolkit later.
#
# Usage:  hh/demo-save-load.sh [--keep]
#   --keep   leave the server, container, image and tmux sessions up afterwards
set -uo pipefail

# ---- config -----------------------------------------------------------------
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Pick a free TCP port so we never collide with a stale server from another
# session (a leftover server on a fixed port answers SRP with its own password
# → spurious 401s). Honour an explicit $PORT if the caller forces one.
pick_port() { local p; for p in $(seq 4200 4280); do ss -ltn 2>/dev/null | grep -q ":$p " || { echo "$p"; return; }; done; echo 4173; }
PORT="${PORT:-$(pick_port)}"
PW="${PW:-malware-bless}"
LABEL="${LABEL:-buildbox}"
IMG="${IMG:-python:3.12-slim}"   # base image: ships python3 so the built code runs
CTR="hack-house"            # sbx::SBX_NAME — the container/instance name
SNAP="hh-snap:${LABEL}"
PY="$REPO/.venv/bin/python"
BIN="$REPO/hh/target/debug/hack-house"
SRV_SESS="hhpoc-srv"
A_SESS="hhpoc-a"
B_SESS="hhpoc-b"
EVID="$(mktemp -d /tmp/hh-poc.XXXXXX)"
KEEP=0; [[ "${1:-}" == "--keep" ]] && KEEP=1

GREEN=$'\e[32m'; RED=$'\e[31m'; YEL=$'\e[33m'; DIM=$'\e[2m'; RST=$'\e[0m'
step() { printf '\n%s== %s ==%s\n' "$YEL" "$*" "$RST"; }
ok()   { printf '%s  ok %s%s\n' "$GREEN" "$*" "$RST"; }
bad()  { printf '%s  XX %s%s\n' "$RED" "$*" "$RST"; }
note() { printf '%s    %s%s\n' "$DIM" "$*" "$RST"; }

FAIL=0
fail() { bad "$*"; FAIL=1; }

cleanup() {
  if [[ $KEEP -eq 1 ]]; then
    note "--keep: leaving server/sessions/image up. Evidence: $EVID"
    return
  fi
  step "cleanup"
  tmux kill-session -t "$A_SESS"   2>/dev/null
  tmux kill-session -t "$B_SESS"   2>/dev/null
  tmux kill-session -t "$SRV_SESS" 2>/dev/null
  docker rm -f "$CTR"      >/dev/null 2>&1
  docker rmi -f "$SNAP"    >/dev/null 2>&1
  note "removed container + $SNAP; sessions killed. Evidence kept: $EVID"
}
trap cleanup EXIT

# ---- helpers ----------------------------------------------------------------
# say <session> <text> : type a literal line then Enter (no Ctrl-U; renders race)
say() {
  local sess="$1"; shift
  tmux send-keys -t "$sess" -l "$*"
  sleep 0.4
  tmux send-keys -t "$sess" Enter
  sleep 0.6
}
cap() { tmux capture-pane -t "$1" -p 2>/dev/null; }  # snapshot a pane to stdout
snap_evid() { cap "$1" > "$EVID/$2.txt"; }            # ...and save it

# wait_for <session> <regex> <timeout_s> : poll the pane until regex appears
wait_for() {
  local sess="$1" re="$2" t="${3:-30}" i=0
  while (( i < t*2 )); do
    cap "$sess" | grep -qE "$re" && return 0
    sleep 0.5; ((i++))
  done
  return 1
}
# wait_cmd <cmd...> : succeeds within a timeout (seconds via $WT, default 30)
wait_cmd() {
  local t="${WT:-30}" i=0
  while (( i < t )); do "$@" >/dev/null 2>&1 && return 0; sleep 1; ((i++)); done
  return 1
}

# ---- 0. preflight -----------------------------------------------------------
step "preflight"
command -v tmux >/dev/null || { echo "tmux required"; exit 2; }
[[ -x "$PY"  ]] || { echo "venv python missing: $PY"; exit 2; }
docker info >/dev/null 2>&1 || { echo "docker daemon down - start it first"; exit 2; }
ollama list 2>/dev/null | grep -q 'qwen2.5-coder' || note "warn: qwen2.5-coder not in 'ollama list' (coder path may fall back)"
ollama list 2>/dev/null | grep -q 'qwen2.5:3b'    || note "warn: qwen2.5:3b not present (chat default)"
docker image inspect "$IMG" >/dev/null 2>&1 || { echo "pulling $IMG..."; docker pull "$IMG"; }
if [[ ! -x "$BIN" ]]; then
  step "building client (debug)"; ( cd "$REPO/hh" && cargo build ) || exit 2
fi
ok "tools present, docker up, models checked"
note "evidence dir: $EVID"

# clear any stale state
tmux kill-session -t "$A_SESS" 2>/dev/null; tmux kill-session -t "$B_SESS" 2>/dev/null
tmux kill-session -t "$SRV_SESS" 2>/dev/null
docker rm -f "$CTR" >/dev/null 2>&1
docker rmi -f "$SNAP" >/dev/null 2>&1

# ---- 1. server --------------------------------------------------------------
step "boot server :$PORT"
tmux new-session -d -s "$SRV_SESS" -x 200 -y 50 \
  "cd '$REPO' && '$PY' cmd_chat.py serve 127.0.0.1 $PORT --password '$PW' --no-tls 2>&1 | tee '$EVID/server.log'"
WT=20 wait_cmd bash -c "grep -qiE 'listening|running|serving|started|websocket' '$EVID/server.log'" \
  || sleep 3   # some builds log nothing; give it a beat
ok "server session up"

# ---- 2. session A: client ---------------------------------------------------
step "session A - alice joins"
tmux new-session -d -s "$A_SESS" -x 200 -y 50 \
  "'$BIN' connect 127.0.0.1 $PORT alice --password '$PW' --no-tls 2>&1 | tee '$EVID/clientA.log'"
wait_for "$A_SESS" 'alice|roster|hack-house|owner' 20 && ok "alice in the room" \
  || fail "alice never joined (see $EVID/clientA.log)"
snap_evid "$A_SESS" 01-joined

# ---- 3. launch docker sandbox ----------------------------------------------
step "launch docker sandbox ($IMG)"
say "$A_SESS" "/sbx launch docker $IMG"
WT=60 wait_cmd docker ps --format '{{.Names}}' --filter "name=^${CTR}$" \
  && ok "container '$CTR' running" || fail "sandbox container never came up"
wait_for "$A_SESS" 'summoned|sandbox|ready|online' 60 >/dev/null
snap_evid "$A_SESS" 02-sandbox

# ---- 4. spawn the coder agent + grant drive --------------------------------
step "spawn oracle (qwen2.5:3b chat, qwen2.5-coder:1.5b for !task)"
say "$A_SESS" "/ai start"
wait_for "$A_SESS" 'oracle|online|ollama' 45 && ok "oracle announced" \
  || note "no 'online' line yet - agent log: ${TMPDIR:-/tmp}/hh-agent-oracle.log"
say "$A_SESS" "/grant oracle"
sleep 1
snap_evid "$A_SESS" 03-agent

# ---- 5. fast model builds code in the sandbox ------------------------------
step "fast qwen builds /root/fib.py in the sandbox"
say "$A_SESS" "/ai oracle !create /root/fib.py that prints the first 10 fibonacci numbers space-separated on one line, then run it with python3"
# Give the CPU coder model room to think, then poll for the file.
WT=150 wait_cmd docker exec "$CTR" test -s /root/fib.py
NEED='0 1 1 2 3 5 8 13 21 34'
runout() { docker exec "$CTR" sh -c 'cd /root && python3 fib.py' 2>&1; }
# Accept the model's work only if the file exists AND actually runs to the right
# sequence. A 1.5B model typed through a PTY sometimes drops indentation, so fall
# back to a known-good file (written BEFORE save, so the snapshot is meaningful).
if docker exec "$CTR" test -s /root/fib.py 2>/dev/null && runout | grep -qE "$NEED"; then
  ok "model wrote a working /root/fib.py"
  BUILT_BY="qwen2.5-coder"
else
  note "model output missing or not runnable - writing deterministic fallback so the"
  note "save/load proof still completes (retry for a clean model take in the video)."
  docker exec "$CTR" sh -c 'cat > /root/fib.py <<"PY"
a, b = 0, 1
out = []
for _ in range(10):
    out.append(str(a))
    a, b = b, a + b
print(" ".join(out))
PY'
  BUILT_BY="fallback"
fi
runout > "$EVID/fib-output.txt" 2>&1
ORIG_SHA="$(docker exec "$CTR" sha256sum /root/fib.py | awk '{print $1}')"
note "fib.py built by: $BUILT_BY"
note "fib.py output: $(cat "$EVID/fib-output.txt")"
docker exec "$CTR" cat /root/fib.py > "$EVID/fib-src-original.py"
snap_evid "$A_SESS" 04-built
grep -qE "$NEED" "$EVID/fib-output.txt" \
  && ok "fib.py prints the sequence" || fail "fib.py output unexpected"

# ---- 6. snapshot to an image -----------------------------------------------
step "/sbx save $LABEL  (docker commit -> $SNAP)"
say "$A_SESS" "/sbx save $LABEL"
WT=40 wait_cmd bash -c "docker images $SNAP --format '{{.Tag}}' | grep -qx '$LABEL'" \
  && ok "image $SNAP created" || fail "snapshot image not found"
wait_for "$A_SESS" "saved|hh-snap|$LABEL" 10 >/dev/null
snap_evid "$A_SESS" 05-saved

# ---- 7. close the session (quit the client) --------------------------------
step "close session A (Ctrl-Q -> teardown purges the container)"
tmux send-keys -t "$A_SESS" C-q
sleep 3
tmux kill-session -t "$A_SESS" 2>/dev/null
WT=20 wait_cmd bash -c "! docker ps -a --format '{{.Names}}' | grep -qx '$CTR'" \
  && ok "container '$CTR' purged on quit" || fail "container still present after quit"
if docker images "$SNAP" --format '{{.Tag}}' | grep -qx "$LABEL"; then
  ok "image $SNAP survived the purge"
else
  fail "image $SNAP missing after purge"
fi

# ---- 8. session B: reopen and load -----------------------------------------
step "session B - fresh client, /sbx load $LABEL"
tmux new-session -d -s "$B_SESS" -x 200 -y 50 \
  "'$BIN' connect 127.0.0.1 $PORT alice --password '$PW' --no-tls 2>&1 | tee '$EVID/clientB.log'"
wait_for "$B_SESS" 'alice|roster|hack-house|owner' 20 && ok "alice re-joined" \
  || fail "alice never re-joined"
say "$B_SESS" "/sbx load $LABEL"
WT=60 wait_cmd docker ps --format '{{.Names}}' --filter "name=^${CTR}$" \
  && ok "container relaunched from $SNAP" || fail "load never started a container"
wait_for "$B_SESS" 'summoned|sandbox|ready|loading|online' 60 >/dev/null
snap_evid "$B_SESS" 06-loaded

# ---- 9. the reveal: the model's code is intact -----------------------------
step "verify the work persisted"
WT=30 wait_cmd docker exec "$CTR" test -s /root/fib.py
NEW_SHA="$(docker exec "$CTR" sha256sum /root/fib.py 2>/dev/null | awk '{print $1}')"
docker exec "$CTR" cat /root/fib.py > "$EVID/fib-src-loaded.py" 2>/dev/null
docker exec "$CTR" sh -c 'cd /root && python3 fib.py' > "$EVID/fib-output-loaded.txt" 2>&1
note "original sha: $ORIG_SHA"
note "loaded   sha: $NEW_SHA"
note "loaded output: $(cat "$EVID/fib-output-loaded.txt" 2>/dev/null)"
if [[ -n "$NEW_SHA" && "$NEW_SHA" == "$ORIG_SHA" ]]; then
  ok "fib.py is byte-for-byte identical after close+reload - PERSISTENCE PROVEN"
else
  fail "fib.py differs or missing after reload"
fi
# show it on the TUI for the camera
tmux send-keys -t "$B_SESS" F2; sleep 1                       # drive
say "$B_SESS" "cat /root/fib.py && python3 /root/fib.py"
sleep 2
snap_evid "$B_SESS" 07-reveal

# ---- summary ----------------------------------------------------------------
step "result"
if [[ $FAIL -eq 0 ]]; then
  printf '%sPoC PASS%s - built-by=%s, saved=%s, purged-on-quit, reloaded-intact\n' \
    "$GREEN" "$RST" "$BUILT_BY" "$SNAP"
else
  printf '%sPoC FAIL%s - inspect captures in %s\n' "$RED" "$RST" "$EVID"
fi
note "captures: $EVID/{01-joined,02-sandbox,03-agent,04-built,05-saved,06-loaded,07-reveal}.txt"
note "code: $EVID/fib-src-original.py vs fib-src-loaded.py"
exit $FAIL
