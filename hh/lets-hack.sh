#!/usr/bin/env bash
# lets-hack.sh — spin up a local hack-house test clergy in tmux
#
# Builds the client, makes sure a --no-tls server is running, then opens one
# tmux pane per user (default: alice bob) and attaches. Each pane is a real
# TTY, so the ratatui UI is fully interactive.
#
# usage:
#   ./lets-hack.sh                      # alice + bob on 127.0.0.1:4173
#   ./lets-hack.sh neo trinity dozer    # one pane per name (tiled)
#   ./lets-hack.sh --reuse              # keep an already-live server (don't reboot)
#   ./lets-hack.sh --theme neon         # dress every pane in themes/neon.toml
#   THEME=crypt ./lets-hack.sh          # same, via env (church | neon | crypt)
#   PORT=4200 PW=hunter2 ./lets-hack.sh # throwaway server you can kill/restart
#   ./lets-hack.sh --kill               # tear the test setup down
#   ./lets-hack.sh -h                   # full usage (also --help / -help)
#
# A fresh server is booted on every run by default (an existing one on PORT is
# stopped first), so you never inherit stale message history. Pass --reuse to
# keep a live server — useful for reconnect tests (Ctrl-R rejoins after --kill).
#
# Run from inside tmux: the clergy is built in its own session and your client is
# switched to it (switch-client). Run from a plain shell: the script attaches.
# Either way you land in the clergy — no manual `tmux attach` needed.
#
# Reconnect test: use a dedicated port (e.g. PORT=4200) so `--kill` can stop
# the server; press Ctrl-R in a pane after the drop to rejoin.
set -uo pipefail

usage() {
    cat <<EOF
lets-hack.sh — spin up a local hack-house test clergy in tmux

usage:
  ./lets-hack.sh [USERS...] [--theme NAME] [--fresh]
  ./lets-hack.sh --kill
  ./lets-hack.sh -h | --help

arguments:
  USERS...        one tmux pane per name (default: alice bob)

flags:
  --theme NAME    dress every pane in themes/NAME.toml (church | neon | crypt)
  --reuse         keep an already-live server instead of booting a fresh one
  --fresh         force a fresh server (the default — kept for clarity)
  --kill          tear down the tmux session and the server we started
  -h, --help      show this help and exit

note: by default a *fresh* server is booted every run; an existing one on PORT
      is stopped first so you never inherit stale history. Use --reuse to keep it.

environment (override any default):
  SESSION   tmux session name      (default: hh-test)
  HOST      server bind host       (default: 127.0.0.1)
  PORT      server port            (default: 4173)
  PW        shared room password   (default: malware-bless)
  THEME     theme name             (church | neon | crypt)

examples:
  ./lets-hack.sh                      # alice + bob on 127.0.0.1:4173
  ./lets-hack.sh neo trinity dozer    # one pane per name (tiled)
  ./lets-hack.sh --theme neon         # neon vestments for every pane
  PORT=4200 PW=hunter2 ./lets-hack.sh # throwaway server you can kill/restart
  ./lets-hack.sh --kill               # tear the test setup down
EOF
}

HERE="$(cd "$(dirname "$0")" && pwd)"   # .../hh
ROOT="$(cd "$HERE/.." && pwd)"          # repo root
PY="$ROOT/.venv/bin/python"             # venv always from the real checkout
BIN="$HERE/target/debug/hack-house"

# The demo always runs a *stable* branch (default: main), never your in-progress
# working branch. If you're on another branch (e.g. dev), the client + server are
# built from a detached git worktree of $DEMO_BRANCH so your checkout is untouched.
DEMO_BRANCH="${BRANCH:-main}"
DEMO_WT="${HH_DEMO_WORKTREE:-/tmp/hh-demo-$DEMO_BRANCH}"

SESSION="${SESSION:-hh-test}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-4173}"
PW="${PW:-malware-bless}"
SRV_LOG="/tmp/hh-${SESSION}-server.log"
SRV_PIDFILE="/tmp/hh-${SESSION}-server.pid"
THEMES_DIR="$HERE/themes"
THEME="${THEME:-}"               # name (church|neon|crypt) or empty for built-in default

is_up() { curl -s --max-time 2 "http://$HOST:$PORT/health" 2>/dev/null | grep -q '"status":"ok"'; }

# Stop the server on $PORT: the one we started (pidfile) and any process still
# holding the port (covers an untracked/stale server). Used by --kill / --fresh.
stop_server() {
    if [[ -f "$SRV_PIDFILE" ]]; then
        kill "$(cat "$SRV_PIDFILE")" 2>/dev/null && echo "stopped tracked server (pid $(cat "$SRV_PIDFILE"))"
        rm -f "$SRV_PIDFILE"
    fi
    local pid
    pid="$(ss -ltnpH "sport = :$PORT" 2>/dev/null | grep -oP 'pid=\K[0-9]+' | head -1)"
    if [[ -n "$pid" ]]; then
        kill "$pid" 2>/dev/null && echo "stopped server holding :$PORT (pid $pid)"
    fi
}

# Parse flags and collect usernames (flags may appear anywhere).
# REUSE=1 keeps an already-live server; the default is to boot a fresh one.
REUSE="${REUSE:-0}"
DO_KILL=0
USERS=()
want_theme=0   # set when --theme consumed the next arg as its value
for a in "$@"; do
    if [[ $want_theme -eq 1 ]]; then THEME="$a"; want_theme=0; continue; fi
    case "$a" in
        -h|--help|-help) usage; exit 0 ;;
        --kill)     DO_KILL=1 ;;
        --reuse)    REUSE=1 ;;
        --fresh)    REUSE=0 ;;   # explicit fresh server (this is the default)
        --theme)    want_theme=1 ;;
        --theme=*)  THEME="${a#--theme=}" ;;
        -*)         echo "✖ unknown flag: $a (try --reuse / --kill / --theme / --help)" >&2; exit 2 ;;
        *)          USERS+=("$a") ;;
    esac
done
[[ $want_theme -eq 1 ]] && { echo "✖ --theme needs a name (e.g. --theme neon)" >&2; exit 2; }

# --kill: tear down the tmux session and the server we started. (Done before
# theme resolution so teardown never depends on a valid --theme.)
if [[ $DO_KILL -eq 1 ]]; then
    tmux kill-session -t "$SESSION" 2>/dev/null && echo "killed tmux session $SESSION"
    stop_server
    if [[ -e "$DEMO_WT/.git" ]]; then
        git -C "$ROOT" worktree remove --force "$DEMO_WT" 2>/dev/null && echo "removed demo worktree $DEMO_WT"
    fi
    exit 0
fi

# Pin the demo to $DEMO_BRANCH (default: main) when we're on another branch, so
# e.g. running this from `dev` still demos `main`. A *detached* worktree is used
# so the branch isn't locked and your real checkout is never modified.
cur_branch="$(git -C "$ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
if [[ "$cur_branch" != "$DEMO_BRANCH" ]]; then
    if git -C "$ROOT" show-ref --verify --quiet "refs/heads/$DEMO_BRANCH"; then
        if [[ -e "$DEMO_WT/.git" ]]; then
            git -C "$DEMO_WT" checkout -f --detach "$DEMO_BRANCH" >/dev/null 2>&1
        else
            git -C "$ROOT" worktree add --detach --force "$DEMO_WT" "$DEMO_BRANCH" >/dev/null 2>&1 \
                || { echo "✖ could not create '$DEMO_BRANCH' worktree at $DEMO_WT" >&2; exit 1; }
        fi
        echo "demo pinned to '$DEMO_BRANCH' (you're on '$cur_branch') — building from $DEMO_WT"
        ROOT="$DEMO_WT"; HERE="$DEMO_WT/hh"
        BIN="$HERE/target/debug/hack-house"; THEMES_DIR="$HERE/themes"
    else
        echo "note: no local '$DEMO_BRANCH' branch — demoing current branch '$cur_branch'" >&2
    fi
fi

# Resolve a theme name → TOML path, or empty for the client's built-in default.
THEME_PATH=""
if [[ -n "$THEME" ]]; then
    THEME_PATH="$THEMES_DIR/$THEME.toml"
    if [[ ! -f "$THEME_PATH" ]]; then
        avail="$(cd "$THEMES_DIR" 2>/dev/null && ls -1 *.toml 2>/dev/null | sed 's/\.toml$//' | paste -sd' ' -)"
        echo "✖ no theme '$THEME' in $THEMES_DIR (available: ${avail:-none})" >&2
        exit 2
    fi
fi

[[ ${#USERS[@]} -eq 0 ]] && USERS=(alice bob)

# 1. build the client
echo "building client…"
( cd "$HERE" && cargo build --quiet ) || { echo "✖ build failed"; exit 1; }

# 2. (re)boot the server on $PORT. Default behaviour: always create a *fresh*
#    server — if one is already live we stop it first so we never inherit stale
#    message history / ghost users. Pass --reuse to keep an existing live server
#    (handy for reconnect tests where the server must outlive a client restart).
if [[ $REUSE -eq 1 ]] && is_up; then
    echo "--reuse: keeping server already live on $HOST:$PORT"
else
    if is_up; then
        echo "replacing server already live on $HOST:$PORT…"
        stop_server
        for _ in $(seq 1 20); do is_up || break; sleep 0.5; done
        is_up && { echo "✖ could not free port $PORT — is another process holding it?"; exit 1; }
    fi
    echo "booting fresh server on $HOST:$PORT…"
    "$PY" "$ROOT/cmd_chat.py" serve "$HOST" "$PORT" --password "$PW" --no-tls >"$SRV_LOG" 2>&1 &
    echo $! > "$SRV_PIDFILE"
    for _ in $(seq 1 20); do is_up && break; sleep 1; done
    is_up || { echo "✖ server did not come up — see $SRV_LOG"; exit 1; }
    echo "server up (pid $(cat "$SRV_PIDFILE"), log $SRV_LOG)"
fi

# 3. (re)build the tmux session: one pane per user, tiled.
#
# The client is launched as each pane's OWN command (run by tmux via `sh -c`),
# never with `send-keys`. send-keys raced the interactive shell's startup — on a
# shell with heavy rc init (conda/pyenv/nvm) the keystrokes land mid-init and get
# swallowed, leaving blank panes. Running it as the pane command sidesteps the
# shell entirely; the trailing `read` keeps the pane open so an auth error (e.g.
# name already taken) stays on screen instead of the pane vanishing.
launch_cmd() { # $1 = username  → command string for the pane's `sh -c`
    local theme_arg=""
    [[ -n "$THEME_PATH" ]] && theme_arg="$(printf -- '--theme %q' "$THEME_PATH")"
    printf '%q connect %q %q %q --password %q --no-tls %s; ec=$?; printf "\n%s left the house (exit %%s) — press enter to close\n" "$ec"; read _' \
        "$BIN" "$HOST" "$PORT" "$1" "$PW" "$theme_arg" "$1"
}

# Guard: if we're attached to a tmux session with this exact name, the
# kill-session below would tear down the session we're sitting in and drop us to
# a bare shell ("closing" tmux). Refuse with a clear fix instead of yanking it.
if [[ -n "${TMUX:-}" && "$(tmux display-message -p '#S' 2>/dev/null)" == "$SESSION" ]]; then
    echo "✖ you're inside the tmux session '$SESSION' — rebuilding it would close it." >&2
    echo "  use a different name (e.g. SESSION=hh-test $0 ${USERS[*]}) or run --kill from another window." >&2
    exit 2
fi

tmux kill-session -t "$SESSION" 2>/dev/null
tmux new-session -d -s "$SESSION" -x 220 -y 50 -c "$HERE" "$(launch_cmd "${USERS[0]}")"
for ((i = 1; i < ${#USERS[@]}; i++)); do
    tmux split-window -h -t "$SESSION" -c "$HERE" "$(launch_cmd "${USERS[i]}")"
    tmux select-layout -t "$SESSION" tiled >/dev/null
done
tmux select-layout -t "$SESSION" tiled >/dev/null

echo "clergy: ${USERS[*]}  ·  session: $SESSION  ·  $HOST:$PORT  ·  vestments: ${THEME:-church (default)}"
echo "tear down later with:  $0 --kill"

# 4. land in the clergy. From a plain shell we replace this process with `attach`.
#    From inside tmux we can't nest an attach, so switch the current client to
#    the new session — that actually drops you into the clergy instead of leaving
#    it orphaned with a hint you have to act on yourself.
if [[ -z "${TMUX:-}" ]]; then
    exec tmux attach -t "$SESSION"
else
    echo "inside tmux — switching this client to '$SESSION' (detach with Ctrl-b d)"
    tmux switch-client -t "$SESSION"
fi
