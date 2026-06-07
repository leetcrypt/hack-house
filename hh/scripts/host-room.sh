#!/usr/bin/env bash
# host-room.sh — host a hack-house room (the server) for others to join.
#
# Thin, friendly wrapper around `cmd_chat.py serve`: picks the project venv,
# mints a room password if you don't supply one, prints the exact join command
# (preferring your Tailscale IP), and runs the server in the foreground until
# you Ctrl-C it.
#
# usage:
#   ./host-room.sh                       # 0.0.0.0:4173, --no-tls, random password
#   ./host-room.sh 4200                  # custom port
#   ./host-room.sh --host 100.x.y.z      # bind a specific address
#   PW=hunter2 ./host-room.sh            # pin a known password (or --password)
#   ./host-room.sh --tls --cert c.pem --key k.pem   # real TLS instead of --no-tls
#   ./host-room.sh -h                    # full usage
#
# The password lives in memory only (passed via $CMD_CHAT_PASSWORD, never on the
# command line, so it won't show up in `ps`). Reveal/share it in-app with /pw.
set -uo pipefail

usage() {
    cat <<EOF
host-room.sh — host a hack-house room (the server)

usage:
  ./host-room.sh [PORT] [--host ADDR] [--password PW]
  ./host-room.sh --tls --cert CERT --key KEY [PORT]
  ./host-room.sh -h | --help

flags:
  --host ADDR      bind address          (default: 0.0.0.0 — reachable on all NICs)
  --port PORT      listen port           (default: 4173; also accepted positionally)
  --password PW    room password         (default: random; or set PW=… / --password)
  --tls            serve real TLS (requires --cert and --key)
  --cert CERT      TLS certificate path  (implies --tls)
  --key  KEY       TLS private key path  (implies --tls)
  -h, --help       show this help and exit

environment (override any default):
  HOST   bind address     (default: 0.0.0.0)
  PORT   listen port      (default: 4173)
  PW     room password    (default: random, openssl-generated)

notes:
  - Default transport is --no-tls — the norm over a Tailscale tunnel. Pass --tls
    with a cert/key for a public/untrusted network.
  - Runs in the foreground; press Ctrl-C to stop the room.
  - Missing Python deps? run ./bootstrap.sh first.

examples:
  ./host-room.sh                       # quick room on 0.0.0.0:4173 (--no-tls)
  PW=hunter2 ./host-room.sh 4200       # known password, port 4200
  ./host-room.sh --tls --cert fullchain.pem --key privkey.pem 443
EOF
}

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"   # repo root
PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3 || command -v python)"

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-4173}"
PW="${PW:-}"
USE_TLS=0
CERT=""
KEY=""

# Parse flags (a lone number is taken as the port, for `./host-room.sh 4200`).
while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help|-help) usage; exit 0 ;;
        --host)     HOST="$2"; shift 2 ;;
        --host=*)   HOST="${1#--host=}"; shift ;;
        --port)     PORT="$2"; shift 2 ;;
        --port=*)   PORT="${1#--port=}"; shift ;;
        --password) PW="$2"; shift 2 ;;
        --password=*) PW="${1#--password=}"; shift ;;
        --tls)      USE_TLS=1; shift ;;
        --cert)     CERT="$2"; USE_TLS=1; shift 2 ;;
        --cert=*)   CERT="${1#--cert=}"; USE_TLS=1; shift ;;
        --key)      KEY="$2"; USE_TLS=1; shift 2 ;;
        --key=*)    KEY="${1#--key=}"; USE_TLS=1; shift ;;
        [0-9]*)     PORT="$1"; shift ;;
        *)          echo "✖ unknown argument: $1 (try --help)" >&2; exit 2 ;;
    esac
done

# Mint a strong password if none was supplied — random per run, in memory only.
if [[ -z "$PW" ]]; then
    if command -v openssl >/dev/null 2>&1; then PW="$(openssl rand -hex 12)"
    else PW="$(head -c 18 /dev/urandom | base64)"; fi
fi

# Resolve transport: --no-tls by default; --tls needs both cert and key.
if [[ $USE_TLS -eq 1 ]]; then
    [[ -n "$CERT" && -n "$KEY" ]] || { echo "✖ --tls needs both --cert and --key" >&2; exit 2; }
    [[ -f "$CERT" ]] || { echo "✖ cert not found: $CERT" >&2; exit 2; }
    [[ -f "$KEY"  ]] || { echo "✖ key not found: $KEY" >&2; exit 2; }
    SERVE_FLAGS=(--cert "$CERT" --key "$KEY")
    PROTO="https"; CLIENT_FLAG="--insecure"
else
    SERVE_FLAGS=(--no-tls)
    PROTO="http"; CLIENT_FLAG="--no-tls"
fi

# Sanity-check deps so failures are an actionable hint, not a stack trace.
if ! "$PY" -c "import sanic" >/dev/null 2>&1; then
    echo "✖ Python deps missing (sanic not importable with $PY)." >&2
    echo "  run ./bootstrap.sh to set up the .venv, then retry." >&2
    exit 1
fi

# Best IP to hand a joiner: Tailscale first (encrypted tunnel), else a global LAN IP.
TS_IP="$(tailscale ip -4 2>/dev/null | head -1 || true)"
LAN_IP="$(ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -1)"
JOIN_IP="${TS_IP:-${LAN_IP:-$HOST}}"

echo "═══════════════════════════════════════════════"
echo "  ⛧ hack-house room — $PROTO://$HOST:$PORT"
echo "═══════════════════════════════════════════════"
[[ -n "$TS_IP"  ]] && echo "  tailscale : $TS_IP  (recommended — encrypted)"
[[ -n "$LAN_IP" ]] && echo "  lan       : $LAN_IP"
echo "  password  : $PW   (share it; reveal in-app with /pw)"
echo
echo "  others join with:"
echo "    python cmd_chat.py connect $JOIN_IP $PORT <name> $CLIENT_FLAG"
echo "  or, with the TUI client:"
echo "    hh/target/debug/hack-house connect $JOIN_IP $PORT <name> $CLIENT_FLAG"
echo
echo "  Ctrl-C to stop the room."
echo "═══════════════════════════════════════════════"

# Hand the password to the server via env (resolve_password reads it), so it
# never appears in the process list. Foreground exec: Ctrl-C stops the room.
cd "$ROOT"
exec env CMD_CHAT_PASSWORD="$PW" "$PY" cmd_chat.py serve "$HOST" "$PORT" "${SERVE_FLAGS[@]}"
