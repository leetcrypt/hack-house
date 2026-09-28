#!/usr/bin/env bash
# hh-go — one command to launch a hack-house: routing sugar over the repo's own
# scripts and modules. Every flag works with or without dashes (`hh-go up` ==
# `hh-go --up`). Portable: it finds the repo from its own location, pins the
# project venv, and holds nothing machine-specific.
#
# Usage:
#   hh-go                    host a local room and take your own seat (default)
#   hh-go up                 ...same
#   hh-go host [lan|tailnet|tor]
#                            host the server only (no seat). Default tailnet/LAN;
#                            `tor` mints a throwaway .onion (needs tor). Prints the
#                            join command.
#   hh-go join <host> <port> [name]
#                            connect to a room (password prompted, RAM-only)
#   hh-go ai [model|profile] summon an AI instance into your local room. You OWN it
#                            (control who queries it: /ai <name> private|allow|…).
#                            Default local Ollama; a models.toml profile name uses
#                            whatever backend it defines (bring-your-own).
#   hh-go device [flipper|<alias>]
#                            bridge a physical device into the room as a member
#   hh-go share              start the browser relay for this room; then run
#                            `/share` in the TUI to get the shareable URL
#   hh-go status             what's running right now (changes nothing)
#   hh-go down               tear down hh-go tmux sessions + local servers
#   hh-go install            interactive setup wizard (install.sh)
#   hh-go -h | --help        this help
set -uo pipefail

# --- locate the repo + venv --------------------------------------------------
SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SELF/../.." && pwd)"           # hh/scripts -> repo root
[[ -d "$ROOT/cmd_chat" ]] || { echo "hh-go: can't find the repo (no cmd_chat/ at $ROOT)" >&2; exit 1; }
PY="$ROOT/.venv/bin/python"; [[ -x "$PY" ]] || PY="${HH_AI_PYTHON:-python3}"
HH_SESSION="hh-go"
DEFAULT_PORT="${HH_PORT:-4173}"

die()  { echo "hh-go: $*" >&2; exit 1; }
note() { echo "→ $*"; }
have() { command -v "$1" >/dev/null 2>&1; }

# normalize: strip a single leading -- from the first token
cmd="${1:-up}"; cmd="${cmd#--}"; shift || true

tui_bin() {  # echo the built client, preferring release
  for b in "$ROOT/hh/target/release/hack-house" "$ROOT/hh/target/debug/hack-house"; do
    [[ -x "$b" ]] && { echo "$b"; return 0; }
  done
  return 1
}

case "$cmd" in
  up|"")
    exec "$ROOT/hh/scripts/host-house.sh" "$@"
    ;;

  host)
    mode="${1:-tailnet}"; mode="${mode#--}"
    case "$mode" in
      lan|tailnet) exec "$ROOT/hh/scripts/host-room.sh" ;;      # binds 0.0.0.0, prints tailnet/LAN join
      tor)         note "hosting a throwaway .onion (needs tor) — see README § Tor onion hosting"
                   read -rp "room password: " -s pw; echo
                   exec "$PY" "$ROOT/cmd_chat.py" serve 127.0.0.1 "$DEFAULT_PORT" --password "$pw" --no-tls --tor ;;
      *)           die "host mode must be: lan | tailnet | tor" ;;
    esac
    ;;

  join)
    [[ $# -ge 2 ]] || die "usage: hh-go join <host> <port> [name]"
    host="$1"; port="$2"; name="${3:-$USER}"
    exec "$ROOT/hh/scripts/connect.sh" "$name" "$host" -P "$port"
    ;;

  ai)
    model="${1:-qwen2.5:3b}"
    note "summoning an AI instance you OWN into the local room on :$DEFAULT_PORT"
    note "control it in the TUI: /ai $model private | /ai $model allow <user> | /ai $model ask-mode on"
    read -rp "room password: " -s pw; echo
    # A bare name with no ':' or '/' is treated as a models.toml profile (BYO
    # backend); otherwise a local Ollama tag. Runs against your machine.
    if [[ "$model" == *:* || "$model" == */* ]]; then
      exec "$PY" -m cmd_chat.agent 127.0.0.1 "$DEFAULT_PORT" --provider ollama --model "$model" \
           --owner "$USER" --password "$pw" --no-tls
    else
      exec "$PY" -m cmd_chat.agent 127.0.0.1 "$DEFAULT_PORT" --profile "$model" \
           --owner "$USER" --password "$pw" --no-tls 2>/dev/null \
        || exec "$PY" -m cmd_chat.agent 127.0.0.1 "$DEFAULT_PORT" --provider ollama --model "$model" \
             --owner "$USER" --password "$pw" --no-tls
    fi
    ;;

  device)
    dev="${1:-flipper}"; dev="${dev#--}"
    note "bridging device '$dev' into the local room on :$DEFAULT_PORT (joins as a persona member)"
    read -rp "room password: " -s pw; echo
    exec "$PY" -m cmd_chat.device 127.0.0.1 "$DEFAULT_PORT" --device "$dev" --alias "$dev" \
         --owner "$USER" --password "$pw" --no-tls
    ;;

  share|browser)
    # Simple browser sharing: run the zero-knowledge web relay + a publisher that
    # joins THIS room. The publisher writes the shareable URL to a local 0600 file
    # — surface it in the TUI with `/share` (the #k key never touches the relay).
    read -rp "room port [$DEFAULT_PORT]: " port; port="${port:-$DEFAULT_PORT}"
    read -rp "room password: " -s pw; echo
    tmux new-session -d -s "$HH_SESSION-web" -n relay \
      "cd '$ROOT/web-relay' && '$PY' -m relay --host 127.0.0.1 --port 8080 --no-tls" 2>/dev/null \
      || die "tmux required for hh-go share"
    sleep 1
    tmux new-window -t "$HH_SESSION-web" -n publisher \
      "'$PY' -m cmd_chat.web 127.0.0.1 $port --password '$pw' --no-tls --relay http://127.0.0.1:8080 --label 'hack-house room'"
    note "web relay + publisher up in tmux session '$HH_SESSION-web'."
    note "In your TUI, run  /share  to get the browser URL (bearer credential — share out-of-band)."
    note "For a phone with only a browser + no LAN/tailnet, front the relay with Tor — see README § Browser access."
    ;;

  status)
    echo "== hh-go status =="
    echo "-- tmux sessions --"; tmux ls 2>/dev/null | grep -E "^(hh-|$HH_SESSION)" || echo "  (none)"
    echo "-- room servers --";  pgrep -af "cmd_chat.py serve" | sed 's/^/  /' || echo "  (none)"
    echo "-- AI instances --";  pgrep -af "cmd_chat.agent"   | sed 's/^/  /' || echo "  (none)"
    echo "-- device bridges --";pgrep -af "cmd_chat.device"  | sed 's/^/  /' || echo "  (none)"
    echo "-- browser relay --"; pgrep -af "cmd_chat.web|web-relay" | sed 's/^/  /' || echo "  (none)"
    ;;

  down|stop|kill|off)
    note "tearing down hh-go sessions + local servers"
    for s in "$HH_SESSION" "$HH_SESSION-web" hh-house hh-room; do tmux kill-session -t "$s" 2>/dev/null; done
    pkill -TERM -f "cmd_chat.py serve"  2>/dev/null || true
    pkill -TERM -f "cmd_chat.web"       2>/dev/null || true
    pkill -TERM -f "web-relay|-m relay" 2>/dev/null || true
    note "done. (AI instances / device bridges you started keep running — stop them in-room or by PID.)"
    ;;

  install)
    exec "$ROOT/hh/scripts/install.sh" "$@"
    ;;

  h|help|-h|--help|-help)
    sed -n '2,/^set /{/^set /d;s/^# \{0,1\}//;p}' "${BASH_SOURCE[0]}"
    ;;

  *)
    die "unknown command '$cmd' — try: up | host | join | ai | device | share | status | down | install | help"
    ;;
esac
