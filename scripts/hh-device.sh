#!/usr/bin/env bash
# hh-device — bring a physical device into a hack-house room as a persona member.
#
# The device joins the room as `@<device>`; members drive it with curated chat
# commands (status/scan/clients/loot/payloads/push/run) and, once granted, a raw
# `/sbx <device>` shell. See docs/device-bridge.md.
#
# Usage:
#   hh-device <device> <room-host> <room-port> <room-password> [--owner <you>] [extra…]
#
# Examples:
#   # add your Pineapple Pager to a loopback room you host on :4173
#   hh-device pager 127.0.0.1 4173 hunter2 --owner alice
#   # join an onion room (reads nothing — pass its port/password)
#   hh-device pager 127.0.0.1 9000 "$ONION_PW" --owner alice
#
# Env: HH_REPO (default ~/coding/hack-house/main), HH_DEVICE_ALIAS (ssh alias /
# transport handle; defaults to the device name).
set -euo pipefail

REPO="${HH_REPO:-$HOME/coding/hack-house/main}"
[[ -x "$REPO/.venv/bin/python" ]] || { echo "✖ no venv at $REPO/.venv — set HH_REPO"; exit 1; }

DEVICE="${1:-}"; HOST="${2:-}"; PORT="${3:-}"; PW="${4:-}"
if [[ -z "$DEVICE" || -z "$HOST" || -z "$PORT" || -z "$PW" ]]; then
  echo "usage: hh-device <device> <room-host> <room-port> <room-password> [--owner <you>]"
  echo "       device is one of: pager, flipper"
  echo "       (flipper: USB-serial, tethered-only; HH_DEVICE_ALIAS=/dev/ttyACM0 pins the node)"
  exit 2
fi
shift 4 || true
ALIAS="${HH_DEVICE_ALIAS:-$DEVICE}"

# --no-tls for a plain local/onion room; drop it (or pass --insecure) for a TLS relay.
exec "$REPO/.venv/bin/python" -m cmd_chat.device "$HOST" "$PORT" \
  --device "$DEVICE" --persona "$DEVICE" --alias "$ALIAS" \
  --password "$PW" --no-tls "$@"
