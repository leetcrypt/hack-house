#!/usr/bin/env bash
# connect.sh — join a hack-house room without leaving the password on disk.
#
# The password lives only in RAM: it is read into a shell variable (never a
# file), and the interactive prompt keeps it out of your shell history. Supply
# it three ways, most to least private:
#   1) interactive (recommended):  ./connect.sh alice 100.117.177.50
#        → prompts "room password:" with no echo
#   2) environment:                HH_PASSWORD=secret ./connect.sh alice <host>
#   3) flag:                       ./connect.sh alice <host> -p secret
#
# Caveat: however it arrives, the client receives the password as a CLI argument,
# so it is briefly visible in the process list (ps) to other *local* users for
# the lifetime of the session. Nothing is ever written to disk.
#
# Usage: ./connect.sh [NAME] [HOST] [-p PASSWORD] [-P PORT] [--tls] [--insecure]
#   NAME   display handle; omit to be prompted for one on join
#   HOST   server IP/host                       (default: 127.0.0.1)
#   -P     port                                 (default: 4173, or $HH_PORT)
#   --tls  use wss/https instead of the default plaintext-over-Tailscale
set -euo pipefail
cd "$(dirname "$0")/.."

DEFAULT_PORT=4173
DEFAULT_HOST=127.0.0.1

NAME=""
HOST=""
PORT="${HH_PORT:-$DEFAULT_PORT}"
PASSWORD="${HH_PASSWORD:-}"
NO_TLS=1        # rooms run --no-tls over Tailscale/LAN by default
INSECURE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    -p|--password) PASSWORD="$2"; shift 2 ;;
    -P|--port)     PORT="$2";     shift 2 ;;
    --tls)         NO_TLS=0;      shift ;;
    --insecure)    INSECURE=1;    shift ;;
    -h|--help)     sed -n '2,/^set /{/^set /d;s/^# \{0,1\}//;p}' "$0"; exit 0 ;;
    -*)            echo "✖ unknown option: $1" >&2; exit 2 ;;
    *)
      if   [[ -z "$NAME" ]]; then NAME="$1"
      elif [[ -z "$HOST" ]]; then HOST="$1"
      else echo "✖ unexpected argument: $1" >&2; exit 2; fi
      shift ;;
  esac
done

HOST="${HOST:-$DEFAULT_HOST}"

# No password yet? Prompt with no echo — straight into RAM, out of history.
if [[ -z "$PASSWORD" ]]; then
  read -rsp "⛧ room password: " PASSWORD < /dev/tty
  echo
fi
[[ -n "$PASSWORD" ]] || { echo "✖ a password is required" >&2; exit 1; }

BIN=./target/release/hack-house
[[ -x "$BIN" ]] || BIN=./target/debug/hack-house
[[ -x "$BIN" ]] || { echo "✖ no hack-house binary — run: cargo build --release" >&2; exit 1; }

args=(connect "$HOST" "$PORT")
[[ -n "$NAME" ]] && args+=("$NAME")          # omit → client prompts for a handle
args+=(--password "$PASSWORD")
[[ "$NO_TLS"  -eq 1 ]] && args+=(--no-tls)
[[ "$INSECURE" -eq 1 ]] && args+=(--insecure)

# exec replaces this shell, so our $PASSWORD copy dies here; only the child holds
# it (via argv), and we never exported it into the child's environment.
exec "$BIN" "${args[@]}"
