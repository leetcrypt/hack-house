#!/usr/bin/env bash
# host-house.sh — host a hack-house room AND take your own GUI seat, all in tmux.
#
# All-in-one launcher: spins up the server (via host-room.sh) in a 'server' window
# and the ratatui client — already joined to that room — in its own window, both in
# a new, distinctly named tmux session (default: hh-house). Others on your
# Tailscale/LAN join with the command host-room.sh prints in the server window.
#
# usage:
#   ./host-house.sh                 # host as $USER on 0.0.0.0:4173 (--no-tls)
#   ./host-house.sh neo             # host, take your seat as "neo"
#   ./host-house.sh 4200            # custom port (also --port)
#   PW=hunter2 ./host-house.sh      # pin a known password (or --password)
#   ./host-house.sh --theme neon    # vestments for your client pane
#   ./host-house.sh --tls --cert c.pem --key k.pem   # real TLS instead of --no-tls
#   ./host-house.sh --kill          # tear down the session + the server we started
#   ./host-house.sh -h              # full usage
#
# The password lives in memory only; it's handed to the server via $CMD_CHAT_PASSWORD
# (host-room.sh), never on its command line. Reveal/share it in-app with /pw.
set -uo pipefail

usage() {
    cat <<EOF
host-house.sh — host a room + open your GUI seat, all in one tmux session

usage:
  ./host-house.sh [NAME] [PORT] [--user NAME] [--host ADDR] [--theme NAME]
  ./host-house.sh --tls --cert CERT --key KEY [NAME] [PORT]
  ./host-house.sh --kill
  ./host-house.sh -h | --help

arguments:
  NAME            your seat in the room          (default: \$USER)
  PORT            listen port (positional)       (default: 4173)

flags:
  --user NAME     your seat in the room (alias: --name; overrides positional NAME)
  --host ADDR     server bind address            (default: 0.0.0.0 — all NICs)
  --port PORT     listen port                    (default: 4173)
  --password PW   room password                  (default: random; or PW=…)
  --theme NAME    vestments for your client pane (church | neon | crypt)
  --tls           serve real TLS (needs --cert and --key)
  --cert CERT     TLS certificate path           (implies --tls)
  --key  KEY      TLS private key path           (implies --tls)
  --kill          tear down the tmux session and the server we started
  -h, --help      show this help and exit

environment (override any default):
  SESSION   tmux session name   (default: hh-house)
  HOST      bind address        (default: 0.0.0.0)
  PORT      listen port         (default: 4173)
  PW        room password       (default: random, openssl-generated)
  THEME     theme name          (church | neon | crypt)
  BRANCH    git branch to build (default: main; BRANCH= keeps current checkout)

notes:
  - Two windows in one session: 'server' (host-room.sh — join banner + logs) and
    your client window (the ratatui GUI). Ctrl-b n/p to switch, Ctrl-b d detach.
  - Default transport is --no-tls (the norm over a Tailscale tunnel); pass --tls
    with a cert/key for a public/untrusted network.

examples:
  ./host-house.sh                 # host as \$USER on 0.0.0.0:4173
  ./host-house.sh neo 4200        # seat "neo", port 4200
  ./host-house.sh --kill          # tear it all down
EOF
}

HERE="$(cd "$(dirname "$0")/.." && pwd)"   # .../hh
ROOT="$(cd "$HERE/.." && pwd)"             # repo root
PY="$ROOT/.venv/bin/python"
BIN="$HERE/target/debug/hack-house"
HOST_ROOM="$HERE/scripts/host-room.sh"

SESSION="${SESSION:-hh-house}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-4173}"
PW="${PW:-$(openssl rand -hex 12)}"
THEMES_DIR="$HERE/themes"
THEME="${THEME:-}"
BRANCH="${BRANCH-main}"   # default: build from main; BRANCH= keeps current checkout
USE_TLS=0; CERT=""; KEY=""
DO_KILL=0
NAME=""

# Parse flags. A bare number is the port; the first bare word is your seat name.
while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help|-help) usage; exit 0 ;;
        --kill)       DO_KILL=1; shift ;;
        --user|--name)     NAME="$2"; shift 2 ;;
        --user=*|--name=*) NAME="${1#*=}"; shift ;;
        --host)       HOST="$2"; shift 2 ;;
        --host=*)     HOST="${1#--host=}"; shift ;;
        --port)       PORT="$2"; shift 2 ;;
        --port=*)     PORT="${1#--port=}"; shift ;;
        --password)   PW="$2"; shift 2 ;;
        --password=*) PW="${1#--password=}"; shift ;;
        --theme)      THEME="$2"; shift 2 ;;
        --theme=*)    THEME="${1#--theme=}"; shift ;;
        --tls)        USE_TLS=1; shift ;;
        --cert)       CERT="$2"; USE_TLS=1; shift 2 ;;
        --cert=*)     CERT="${1#--cert=}"; USE_TLS=1; shift ;;
        --key)        KEY="$2"; USE_TLS=1; shift 2 ;;
        --key=*)      KEY="${1#--key=}"; USE_TLS=1; shift ;;
        [0-9]*)       PORT="$1"; shift ;;
        -*)           echo "✖ unknown flag: $1 (try --help)" >&2; exit 2 ;;
        *)            [[ -z "$NAME" ]] && NAME="$1"; shift ;;
    esac
done
NAME="${NAME:-${USER:-$(id -un)}}"

# Build from a known branch (default: main) so the house never demos a stale
# checkout. BRANCH= (empty) keeps whatever's checked out. Won't clobber dirty work.
ensure_branch() {
    [[ -z "$BRANCH" ]] && return 0
    git -C "$ROOT" rev-parse --git-dir >/dev/null 2>&1 || return 0
    local cur
    cur="$(git -C "$ROOT" symbolic-ref --short -q HEAD || echo DETACHED)"
    [[ "$cur" == "$BRANCH" ]] && return 0
    if [[ -n "$(git -C "$ROOT" status --porcelain)" ]]; then
        echo "✖ on '$cur' with uncommitted changes — can't switch to '$BRANCH'." >&2
        echo "  commit/stash first, or run with BRANCH= to build '$cur' as-is." >&2
        exit 2
    fi
    echo "⛧ switching $cur → $BRANCH before build"
    git -C "$ROOT" switch "$BRANCH" || { echo "✖ couldn't switch to '$BRANCH'" >&2; exit 2; }
}

# Stop the server on $PORT (the one holding the port), used by --kill and before
# we (re)launch so only the room we start answers on this port.
stop_server() {
    local pid
    pid="$(ss -ltnpH "sport = :$PORT" 2>/dev/null | grep -oP 'pid=\K[0-9]+' | head -1)"
    [[ -n "$pid" ]] && kill "$pid" 2>/dev/null && echo "stopped server holding :$PORT (pid $pid)"
}

if [[ $DO_KILL -eq 1 ]]; then
    tmux kill-session -t "$SESSION" 2>/dev/null && echo "killed tmux session $SESSION"
    stop_server
    exit 0
fi

command -v tmux >/dev/null 2>&1 || { echo "✖ tmux not found — install it first" >&2; exit 1; }

# Resolve transport: --no-tls by default; --tls needs a cert and key.
if [[ $USE_TLS -eq 1 ]]; then
    [[ -n "$CERT" && -n "$KEY" ]] || { echo "✖ --tls needs both --cert and --key" >&2; exit 2; }
    [[ -f "$CERT" ]] || { echo "✖ cert not found: $CERT" >&2; exit 2; }
    [[ -f "$KEY"  ]] || { echo "✖ key not found: $KEY" >&2; exit 2; }
    SCHEME="https"; CURLK="-k"; CLIENT_FLAG="--insecure"
    TLS_ARGS=(--tls --cert "$CERT" --key "$KEY")
else
    SCHEME="http"; CURLK=""; CLIENT_FLAG="--no-tls"
    TLS_ARGS=()
fi

# The local client connects over loopback when the server binds all interfaces.
case "$HOST" in
    0.0.0.0|::|"") CLIENT_HOST="127.0.0.1" ;;
    *)             CLIENT_HOST="$HOST" ;;
esac

# Resolve a theme name → TOML path (empty = the client's built-in default).
THEME_PATH=""
if [[ -n "$THEME" ]]; then
    THEME_PATH="$THEMES_DIR/$THEME.toml"
    if [[ ! -f "$THEME_PATH" ]]; then
        avail="$(cd "$THEMES_DIR" 2>/dev/null && ls -1 ./*.toml 2>/dev/null | sed 's#.*/##; s/\.toml$//' | paste -sd' ' -)"
        echo "✖ no theme '$THEME' in $THEMES_DIR (available: ${avail:-none})" >&2
        exit 2
    fi
fi

# Guard: don't rebuild the session we're sitting in (it would close on us).
if [[ -n "${TMUX:-}" && "$(tmux display-message -p '#S' 2>/dev/null)" == "$SESSION" ]]; then
    echo "✖ you're inside the tmux session '$SESSION' — rebuilding it would close it." >&2
    echo "  use a different name (e.g. SESSION=hh-host $0 $NAME) or run --kill from another window." >&2
    exit 2
fi

is_up() { curl -s $CURLK --max-time 2 "$SCHEME://$CLIENT_HOST:$PORT/health" 2>/dev/null | grep -q '"status":"ok"'; }

# 1. build the client so the GUI pane has a binary to run — from $BRANCH (main).
ensure_branch
echo "building client… (branch: ${BRANCH:-current checkout})"
( cd "$HERE" && cargo build --quiet ) || { echo "✖ build failed"; exit 1; }

# 2. clear the port so only the room we start answers on it.
stop_server

# 3. server pane: run host-room.sh (foreground, shows the join banner + logs).
#    host-room reads HOST/PORT/PW from the env; --tls flags are passed through.
server_cmd="$(printf 'env HOST=%q PORT=%q PW=%q %q' "$HOST" "$PORT" "$PW" "$HOST_ROOM")"
for arg in "${TLS_ARGS[@]}"; do server_cmd+=" $(printf '%q' "$arg")"; done

tmux kill-session -t "$SESSION" 2>/dev/null
tmux new-session -d -s "$SESSION" -n server -x 220 -y 50 -c "$HERE" "$server_cmd"

# 4. wait for the room to answer before seating the client.
echo "waiting for the room on $CLIENT_HOST:$PORT…"
for _ in $(seq 1 30); do is_up && break; sleep 0.5; done
is_up || echo "⚠ server not up yet — the client pane will keep its own error on screen" >&2

# 5. client window: the ratatui GUI, joined to the room — a separate window in
#    the SAME session (server stays on its own window). The trailing read keeps
#    the window open if connect exits (e.g. auth error) instead of vanishing.
theme_arg=""
[[ -n "$THEME_PATH" ]] && theme_arg="$(printf -- '--theme %q' "$THEME_PATH")"
client_cmd="$(printf '%q connect %q %q %q --password %q %s %s; ec=$?; printf "\n%s left the house (exit %%s) — press enter to close\n" "$ec"; read _' \
    "$BIN" "$CLIENT_HOST" "$PORT" "$NAME" "$PW" "$CLIENT_FLAG" "$theme_arg" "$NAME")"
tmux new-window -t "$SESSION" -n "$NAME" -c "$HERE" "$client_cmd"
tmux select-window -t "$SESSION:$NAME"   # land on the GUI; Ctrl-b p → server logs

echo "house: $NAME  ·  session: $SESSION  ·  $SCHEME://$HOST:$PORT  ·  vestments: ${THEME:-church (default)}"
echo "room password: $PW   (share with joiners; reveal in-app with /pw)"
echo "tear down later with:  $0 --kill"

# 6. land in the house: attach from a plain shell, switch-client from inside tmux.
if [[ -z "${TMUX:-}" ]]; then
    exec tmux attach -t "$SESSION"
else
    echo "inside tmux — switching this client to '$SESSION' (detach with Ctrl-b d)"
    tmux switch-client -t "$SESSION"
fi
