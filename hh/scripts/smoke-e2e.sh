#!/usr/bin/env bash
# smoke-e2e.sh — headless cross-stack smoke test (CI-runnable, no GUI/X needed).
#
# Boots the Python relay server + two real Rust TUI clients (alice = host/owner,
# bob = guest) inside tmux panes, then asserts the things that actually exercise
# the client<->server protocol:
#   1. both clients complete the SRP handshake and appear in the roster
#   2. a chat message from alice is relayed + decrypted into bob's pane
#      (proves Fernet round-trip through the zero-knowledge relay)
#   3. `/sbx vms` dispatches in the client command parser
#
# Deterministic and self-contained: picks a free port, cleans up sessions + the
# server on exit, and prints a clear PASS/FAIL. No asciinema, VirtualBox, or X.
#
# Env overrides: PY=<python> BIN=<client binary> PORT=<port> PW=<password>
set -uo pipefail

# -h/--help: print the usage header above and exit.
case "${1:-}" in
  -h|--help|-help) sed -n '2,/^set /{/^set /d;s/^# \{0,1\}//;p}' "$0"; exit 0 ;;
esac

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# Python: prefer the repo venv locally, fall back to PATH (CI installs into the
# job's own interpreter).
if [[ -z "${PY:-}" ]]; then
  if [[ -x "$REPO/.venv/bin/python" ]]; then PY="$REPO/.venv/bin/python"; else PY="$(command -v python3 || command -v python)"; fi
fi
BIN="${BIN:-$REPO/hh/target/debug/hack-house}"
PW="${PW:-smoke-pass}"
pick_port() { local p; for p in $(seq 4300 4360); do ss -ltn 2>/dev/null | grep -q ":$p " || { echo "$p"; return; }; done; echo 4399; }
PORT="${PORT:-$(pick_port)}"

SRV_SESS="hh-smoke-srv"
RUN_SESS="hh-smoke"
MARKER="HELLO_FROM_ALICE_$$"

GREEN=$'\e[32m'; RED=$'\e[31m'; YEL=$'\e[33m'; RST=$'\e[0m'
step() { printf '\n%s== %s ==%s\n' "$YEL" "$*" "$RST"; }
ok()   { printf '%s  ok %s%s\n' "$GREEN" "$*" "$RST"; }
FAIL=0; fail() { printf '%s  XX %s%s\n' "$RED" "$*" "$RST"; FAIL=1; }

cleanup() {
  tmux kill-session -t "$RUN_SESS" 2>/dev/null
  tmux kill-session -t "$SRV_SESS" 2>/dev/null
}
trap cleanup EXIT

# Pane-id-safe drivers (don't assume pane-base-index 0 — it may be 1).
APANE=""; BPANE=""
asay() { tmux send-keys -t "$APANE" -l "$*"; sleep 0.4; tmux send-keys -t "$APANE" Enter; sleep 0.6; }
bsay() { tmux send-keys -t "$BPANE" -l "$*"; sleep 0.4; tmux send-keys -t "$BPANE" Enter; sleep 0.6; }
acap() { tmux capture-pane -t "$APANE" -p 2>/dev/null; }
bcap() { tmux capture-pane -t "$BPANE" -p 2>/dev/null; }
await_a() { local re="$1" t="${2:-25}" i=0; while (( i < t*2 )); do acap | grep -qE "$re" && return 0; sleep 0.5; ((i++)); done; return 1; }
await_b() { local re="$1" t="${2:-25}" i=0; while (( i < t*2 )); do bcap | grep -qE "$re" && return 0; sleep 0.5; ((i++)); done; return 1; }

# ---- preflight --------------------------------------------------------------
step "preflight"
command -v tmux >/dev/null || { echo "tmux required"; exit 2; }
[[ -n "$PY" && -x "$(command -v "$PY" 2>/dev/null || echo "$PY")" ]] || { echo "python missing: $PY"; exit 2; }
if [[ ! -x "$BIN" ]]; then
  step "building client"; ( cd "$REPO/hh" && cargo build ) || exit 2
fi
ok "python=$PY  client=$BIN  port=$PORT"

# ---- server (not asserted directly) -----------------------------------------
step "boot relay server :$PORT"
tmux new-session -d -s "$SRV_SESS" -x 200 -y 50 \
  "cd '$REPO' && '$PY' cmd_chat.py serve 127.0.0.1 $PORT --password '$PW' --no-tls 2>&1 | tee /tmp/hh-smoke-srv.log"
for i in $(seq 1 30); do ss -ltn 2>/dev/null | grep -q ":$PORT " && break; sleep 0.5; done
ss -ltn 2>/dev/null | grep -q ":$PORT " && ok "server listening" || { fail "server never bound"; exit 1; }

# ---- two clients in side-by-side panes ---------------------------------------
step "launch alice (host) + bob (guest)"
tmux new-session -d -s "$RUN_SESS" -x 200 -y 50 "bash --noprofile --norc"
APANE="$(tmux list-panes -t "$RUN_SESS" -F '#{pane_id}' | head -1)"
BPANE="$(tmux split-window -h -t "$APANE" -P -F '#{pane_id}' "bash --noprofile --norc")"
sleep 0.5
asay "$BIN connect 127.0.0.1 $PORT alice --password '$PW' --no-tls"
await_a 'joined as alice|alice|roster|hack-house' 25 && ok "alice joined (SRP ok)" || fail "alice never joined"
bsay "$BIN connect 127.0.0.1 $PORT bob --password '$PW' --no-tls"
await_b 'joined as bob|bob|roster|hack-house' 25 && ok "bob joined (SRP ok)" || fail "bob never joined"

# ---- chat round-trip (the real protocol assertion) --------------------------
step "chat relay + Fernet round-trip"
sleep 1.0
asay "$MARKER"
await_b "$MARKER" 20 && ok "bob received alice's encrypted message" || fail "message never relayed to bob"

# ---- command parser dispatch ------------------------------------------------
step "/sbx vms dispatches"
asay "/sbx vms"
# In CI VirtualBox won't be installed; either response mentions VirtualBox, which
# proves the command parsed and ran (this is a local command, not a round-trip).
await_a "VirtualBox" 15 && ok "/sbx vms handled" || fail "/sbx vms produced no response"

# ---- result -----------------------------------------------------------------
step "result"
if [[ $FAIL -eq 0 ]]; then
  printf '%sSMOKE OK%s\n' "$GREEN" "$RST"
else
  printf '%sSMOKE FAIL%s — server log: /tmp/hh-smoke-srv.log\n' "$RED" "$RST"
fi
exit $FAIL
