#!/usr/bin/env bash
# tor-onion-connect.sh — guest-side helper for a room hosted with `serve --tor`
# / `hh-go onion` (docs/spec-tor-p2p-relay.md).
#
# The printed connect commands assume you happen to be sitting in a hack-house
# checkout root, which breaks the moment the guest's shell lands somewhere
# else (a fresh terminal, an SSH login to a different default cwd, ...) — this
# finds a checkout regardless of $PWD, prefers the built Rust client, falls
# back to the Python one if the Rust binary isn't built, and wraps either in
# torsocks. No repo-specific code is needed to connect (no Rust/TUI changes
# were made for --tor — Tor is a transport swap only), so any hack-house
# checkout on this machine works, including one behind the checkout that
# minted the room.
#
# Usage:
#   tor-onion-connect.sh <onion-address> <port> <name> [--password PW] [--repo PATH]
#
# If --password is omitted, the underlying client prompts for it (RAM-only,
# no echo) same as a normal LAN/Tailscale connect.
#
# Env:
#   HH_REPO   explicit checkout path — skips the search entirely
#   HH_PY     python interpreter for the fallback client (default: python3)
set -euo pipefail

usage() { grep '^#' "$0" | sed -n '2,/^set -euo/{/^set -euo/d;s/^# \{0,1\}//;p}'; exit "${1:-0}"; }
[[ "${1:-}" == "-h" || "${1:-}" == "--help" ]] && usage 0
[[ $# -ge 3 ]] || { echo "usage: $0 <onion-address> <port> <name> [--password PW] [--repo PATH]" >&2; exit 2; }

ONION="$1"; PORT="$2"; NAME="$3"; shift 3
PASSWORD="" REPO="${HH_REPO:-}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --password) PASSWORD="$2"; shift 2 ;;
    --repo)     REPO="$2"; shift 2 ;;
    *) echo "unknown arg: $1 (see --help)" >&2; exit 2 ;;
  esac
done

command -v torsocks >/dev/null 2>&1 || { echo "✖ torsocks is not installed" >&2; exit 1; }
if ! (systemctl is-active --quiet tor 2>/dev/null) && ! pgrep -x tor >/dev/null 2>&1; then
  echo "⚠ no 'tor' process detected on this machine — start yours first" \
       "(systemctl start tor, or run your own instance) or this will just hang." >&2
fi

# ── find a checkout — $PWD first (the common case), then the usual spots ────
if [[ -z "$REPO" ]]; then
  self_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  for candidate in \
    "$PWD" \
    "$(dirname "$self_dir")" \
    "$HOME/coding/hack-house/main" \
    "$HOME/coding/hack-house" \
    "$HOME/hack-house"; do
    [[ -f "$candidate/cmd_chat.py" ]] && { REPO="$candidate"; break; }
  done
fi
[[ -n "$REPO" && -f "$REPO/cmd_chat.py" ]] || {
  echo "✖ no hack-house checkout found (tried \$PWD, this script's own repo," \
       "~/coding/hack-house/main, ~/coding/hack-house, ~/hack-house)." >&2
  echo "  Point at one explicitly: --repo PATH or HH_REPO=PATH" >&2
  exit 1
}

# ── pick a client: built Rust TUI if it exists, else the Python client ──────
RUST_BIN="$REPO/hh/target/debug/hack-house"
[[ -x "$RUST_BIN" ]] || RUST_BIN="$REPO/hh/target/release/hack-house"

PW_ARGS=()
[[ -n "$PASSWORD" ]] && PW_ARGS=(--password "$PASSWORD")

if [[ -x "$RUST_BIN" ]]; then
  echo "→ $REPO (Rust TUI: $RUST_BIN)" >&2
  exec torsocks "$RUST_BIN" connect "$ONION" "$PORT" "$NAME" "${PW_ARGS[@]}" --no-tls
fi

PY="${HH_PY:-python3}"
command -v "$PY" >/dev/null 2>&1 || {
  echo "✖ no Rust binary at $REPO/hh/target/{debug,release}/hack-house, and" \
       "'$PY' not found for the fallback client" >&2
  exit 1
}
echo "→ $REPO (Python client — no Rust binary built there yet;" \
     "'cd $REPO/hh && cargo build' for the TUI)" >&2
exec torsocks "$PY" "$REPO/cmd_chat.py" connect "$ONION" "$PORT" "$NAME" "${PW_ARGS[@]}" --no-tls
