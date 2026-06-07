#!/usr/bin/env bash
# test-features.sh — drive a headless hack-house clergy via `tmux send-keys` and
# assert each feature by scraping the rendered TUI with `tmux capture-pane`.
#
# It boots a --no-tls server, opens an OWNER pane and a MEMBER (bob) pane in a
# detached tmux session, fires deterministic keystrokes, and greps the captured
# screen for the state markers the UI paints (DRIVING, scrollback, chat ↑, …).
#
#   ./test-features.sh            # run the suite, leave the session up to inspect
#   ./test-features.sh --kill     # tear the session + server down
#   PORT=4199 PW=test-bless ./test-features.sh
#
# While it runs you can watch live in another terminal:
#   tmux attach -t hh-autotest
set -uo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"   # .../hh
ROOT="$(cd "$HERE/.." && pwd)"          # repo root
PY="$ROOT/.venv/bin/python"
BIN="$HERE/target/debug/hack-house"

SESSION="${SESSION:-hh-autotest}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-4199}"
PW="${PW:-test-bless}"
SRV_LOG="/tmp/hh-${SESSION}-server.log"
SRV_PIDFILE="/tmp/hh-${SESSION}-server.pid"

is_up() { curl -s --max-time 2 "http://$HOST:$PORT/health" 2>/dev/null | grep -q '"status":"ok"'; }

stop_server() {
    if [[ -f "$SRV_PIDFILE" ]]; then
        kill "$(cat "$SRV_PIDFILE")" 2>/dev/null && echo "stopped server (pid $(cat "$SRV_PIDFILE"))"
        rm -f "$SRV_PIDFILE"
    fi
}

if [[ "${1:-}" == "--kill" || "${1:-}" == "--teardown" ]]; then
    tmux kill-session -t "$SESSION" 2>/dev/null && echo "killed tmux $SESSION" || echo "no tmux session"
    stop_server
    exit 0
fi

# ─── result tracking ──────────────────────────────────────────────────────────
PASS=0; FAIL=0
declare -a RESULTS=()

cap() { tmux capture-pane -pt "$1" 2>/dev/null; }

# want <pane> <fixed-substring> <label>
want() {
    local pane="$1" needle="$2" label="$3"
    if cap "$pane" | grep -qF -- "$needle"; then
        echo "  ✓ PASS — $label"; PASS=$((PASS+1)); RESULTS+=("PASS  $label")
    else
        echo "  ✗ FAIL — $label   (looked for: '$needle')"; FAIL=$((FAIL+1)); RESULTS+=("FAIL  $label")
    fi
}
# wantnot <pane> <fixed-substring> <label>
wantnot() {
    local pane="$1" needle="$2" label="$3"
    if cap "$pane" | grep -qF -- "$needle"; then
        echo "  ✗ FAIL — $label   (did NOT want: '$needle')"; FAIL=$((FAIL+1)); RESULTS+=("FAIL  $label")
    else
        echo "  ✓ PASS — $label"; PASS=$((PASS+1)); RESULTS+=("PASS  $label")
    fi
}

# input helpers (type into the TUI input box, then Enter)
o()   { tmux send-keys -t "$OWNER"  -- "$*"; tmux send-keys -t "$OWNER"  Enter; sleep "${TYPE_PAUSE:-0.5}"; }
m()   { tmux send-keys -t "$MEMBER" -- "$*"; tmux send-keys -t "$MEMBER" Enter; sleep "${TYPE_PAUSE:-0.5}"; }
key() { tmux send-keys -t "$1" "$2"; sleep "${KEY_PAUSE:-0.7}"; }                 # named key (PageUp, F2, …)
raw() { tmux send-keys -t "$1" -l "$2"; sleep "${KEY_PAUSE:-0.7}"; }              # literal bytes (mouse SGR)

# ─── 1. build ───────────────────────────────────────────────────────────────
echo "building client…"
( cd "$HERE" && cargo build --quiet ) || { echo "✖ build failed"; exit 1; }
[[ -x "$PY" ]] || { echo "✖ no python at $PY"; exit 1; }

# ─── 2. server ───────────────────────────────────────────────────────────────
if is_up; then
    echo "reusing server on $HOST:$PORT"
else
    echo "booting server on $HOST:$PORT…"
    "$PY" "$ROOT/cmd_chat.py" serve "$HOST" "$PORT" --password "$PW" --no-tls >"$SRV_LOG" 2>&1 &
    echo $! > "$SRV_PIDFILE"
    for _ in $(seq 1 20); do is_up && break; sleep 1; done
    is_up || { echo "✖ server did not come up — see $SRV_LOG"; exit 1; }
    echo "server up (pid $(cat "$SRV_PIDFILE"))"
fi

# ─── 3. tmux session: OWNER | bob ─────────────────────────────────────────────
CONNECT="$BIN connect $HOST $PORT"
FLAGS="--password $PW --no-tls"
tmux kill-session -t "$SESSION" 2>/dev/null || true
# Big window so the TUI never clips; owner connects first → becomes room owner.
tmux new-session -d -s "$SESSION" -x 200 -y 50 -c "$HERE" "$CONNECT owner $FLAGS; read _"
sleep 2
tmux split-window -h -t "$SESSION" -c "$HERE" "$CONNECT bob $FLAGS; read _"
sleep 2
tmux select-layout -t "$SESSION" tiled >/dev/null
tmux set -t "$SESSION" pane-border-status top >/dev/null
tmux set -t "$SESSION" pane-border-format " #{pane_index} " >/dev/null

mapfile -t PANES < <(tmux list-panes -t "$SESSION" -F '#{pane_id}')
OWNER="${PANES[0]}"; MEMBER="${PANES[1]}"
echo "panes: OWNER=$OWNER  MEMBER=$MEMBER   (attach: tmux attach -t $SESSION)"
sleep 2

echo
echo "════════════════════════════════════════════════════════════════"
echo " RUNNING FEATURE SUITE"
echo "════════════════════════════════════════════════════════════════"

# ─── T1. connect + roster ─────────────────────────────────────────────────────
echo "[T1] connect + e2e + roster"
want "$OWNER" "hack-house" "owner UI is up"
want "$OWNER" "e2e"        "owner shows e2e-encrypted status"
want "$OWNER" "house 2/"   "roster shows 2 members"

# ─── T2. chat round-trip (owner → bob) ────────────────────────────────────────
echo "[T2] chat message round-trip"
o "MARKER_CHAT_OWNER_42"
sleep 0.6
want "$MEMBER" "MARKER_CHAT_OWNER_42" "bob received owner's chat message"
m "MARKER_CHAT_BOB_99"
sleep 0.6
want "$OWNER" "MARKER_CHAT_BOB_99" "owner received bob's chat message"

# a few more lines so there's chat history to scroll
for i in 1 2 3 4 5; do o "chat-history-line-$i"; done

# ─── T3. chat scrollback (PgUp/PgDn/Home/End) ─────────────────────────────────
echo "[T3] chat scrollback via PgUp/PgDn"
key "$OWNER" PageUp
want "$OWNER" "End=live"  "PgUp scrolled chat (shows 'End=live' hint)"
wantnot "$OWNER" "scrollback" "PgUp did NOT touch sandbox (no sandbox yet)"
key "$OWNER" End
wantnot "$OWNER" "End=live" "End returned chat to live"

# ─── T4. help overlay (F1 open / any-key close) ───────────────────────────────
echo "[T4] help overlay F1"
key "$OWNER" F1
want "$OWNER" "hack-house — help" "F1 opened the help overlay"
key "$OWNER" Escape
wantnot "$OWNER" "hack-house — help" "a keypress closed the help overlay"

# ─── T5. live theme switch ────────────────────────────────────────────────────
echo "[T5] /theme switch stays alive"
o "/theme neon"
o "/theme church"
want "$OWNER" "hack-house" "UI still alive after theme swap"

# ─── T6. sandbox launch (local backend = no docker) ───────────────────────────
echo "[T6] /sbx launch local  (allowing time to summon)"
o "/sbx launch local"
echo "    …waiting for the sandbox to summon"
sleep 5
want "$OWNER" "local-shell" "owner shows the sandbox pane (local-shell)"

# ─── T7. drive the shell (F2) + run a command ─────────────────────────────────
echo "[T7] drive shell + command output"
key "$OWNER" F2
want "$OWNER" "DRIVING" "F2 entered DRIVING mode"
o "echo HELLOSBX_OK_7"
sleep 1
want "$OWNER" "HELLOSBX_OK_7" "command output rendered in the sandbox terminal"
o "seq 1 60"
sleep 1

# ─── T8. NEW: PgUp scrolls sandbox scrollback WHILE DRIVING ────────────────────
echo "[T8] PgUp scrolls sandbox scrollback while driving (the new wiring)"
key "$OWNER" PageUp
key "$OWNER" PageUp
want "$OWNER" "DRIVING"    "still driving after PgUp (drive not released)"
want "$OWNER" "scrollback" "PgUp scrolled the sandbox scrollback while driving"
key "$OWNER" PageDown
key "$OWNER" PageDown
key "$OWNER" PageDown

# ─── T9. release drive (Esc) ──────────────────────────────────────────────────
echo "[T9] Esc releases the drive"
key "$OWNER" Escape
wantnot "$OWNER" "DRIVING" "Esc released DRIVING mode"

# ─── T10. arrow keys scroll sandbox when NOT driving ──────────────────────────
echo "[T10] Up arrow scrolls sandbox (not driving)"
key "$OWNER" Up
key "$OWNER" Up
want "$OWNER" "scrollback" "Up arrow scrolled the sandbox scrollback"
key "$OWNER" End

# ─── T11. PgUp scrolls CHAT (not sandbox) when not driving ────────────────────
echo "[T11] PgUp scrolls chat (not sandbox) when not driving"
key "$OWNER" PageUp
want "$OWNER" "End=live"   "PgUp scrolled chat while a sandbox is up"
wantnot "$OWNER" "scrollback" "PgUp left the sandbox at live (no 'scrollback')"
key "$OWNER" End

# ─── T12. mouse wheel (best-effort: raw SGR sequence) ─────────────────────────
echo "[T12] mouse wheel scroll (best-effort SGR injection)"
raw "$OWNER" $'\033[<64;20;20M'   # SGR wheel-up at (20,20)
raw "$OWNER" $'\033[<64;20;20M'
if cap "$OWNER" | grep -qF "scrollback"; then
    echo "  ✓ PASS — wheel-up scrolled the sandbox (SGR mouse recognised)"; PASS=$((PASS+1)); RESULTS+=("PASS  mouse wheel scroll")
else
    echo "  ⚠ INFO — wheel-up did not register via send-keys (mouse wheel needs a real terminal; verify by hand)"; RESULTS+=("INFO  mouse wheel scroll (manual)")
fi
key "$OWNER" End

# ─── T13. grant a member drive permission ─────────────────────────────────────
echo "[T13] /grant lets bob drive"
o "/grant bob"
sleep 0.6
key "$MEMBER" F2
want "$MEMBER" "DRIVING" "bob can drive after /grant"
key "$MEMBER" Escape

echo
echo "════════════════════════════════════════════════════════════════"
echo " RESULTS"
echo "════════════════════════════════════════════════════════════════"
for r in "${RESULTS[@]}"; do echo "  $r"; done
echo "----------------------------------------------------------------"
echo "  PASS=$PASS  FAIL=$FAIL"
echo
echo "session left up for inspection:  tmux attach -t $SESSION"
echo "tear down with:                  $0 --kill"

exit $(( FAIL > 0 ? 1 : 0 ))
