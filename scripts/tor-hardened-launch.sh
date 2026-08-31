#!/usr/bin/env bash
# tor-hardened-launch.sh — sandboxed, cookie-authenticated Tor instance for
# the hack-house Tor P2P relay (docs/spec-tor-p2p-relay.md §4.3/§4.4).
#
# Default backend: bwrap (bubblewrap) — no daemon, no image, already on
# trillsec/laptop. Alternative: rootless podman (--engine podman), for parity
# with the existing /sbx podman convention.
#
# What this buys you over `tor -f torrc` run bare:
#   - the tor process can see ONLY its own fresh DataDirectory and the network
#     — no $HOME, no other host processes (bwrap: separate PID/UTS/IPC/cgroup
#     namespaces; podman: --cap-drop=ALL --security-opt=no-new-privileges
#     --read-only root).
#   - ControlPort and SocksPort are Unix sockets under that DataDirectory, not
#     TCP ports — nothing to reach even from loopback except by filesystem
#     access to the socket files.
#   - cookie auth only; no control password ever exists to leak.
#   - NOT single-hop / non-anonymous mode — host anonymity is preserved, not
#     just NAT traversal (§4.5).
#
# Network is intentionally left un-isolated (tor needs full outbound
# reachability to relays) — this is filesystem/process isolation, not network
# sandboxing. Guard rotation is deliberately NOT done here (§4.6).
#
# Usage:
#   scripts/tor-hardened-launch.sh [--engine bwrap|podman] [--data-dir PATH]
#
# On success, prints two lines to stdout (and nothing else):
#   CONTROL_SOCKET=<path>
#   SOCKS_SOCKET=<path>
# so a caller can do:
#   eval "$(scripts/tor-hardened-launch.sh)"
#   cmd_chat.py serve 127.0.0.1 9500 --tor --tor-control-socket "$CONTROL_SOCKET" ...
#
# The tor process runs in the foreground of this script; Ctrl-C / SIGTERM
# tears it down (and, for the podman backend, removes the container).
set -euo pipefail

ENGINE="bwrap"
DATA_DIR=""

while [ $# -gt 0 ]; do
  case "$1" in
    --engine) ENGINE="$2"; shift 2 ;;
    --data-dir) DATA_DIR="$2"; shift 2 ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

case "$ENGINE" in
  bwrap|podman) ;;
  *) echo "--engine must be bwrap or podman, got: $ENGINE" >&2; exit 2 ;;
esac

command -v tor >/dev/null || { echo "tor is not installed" >&2; exit 1; }
command -v "$ENGINE" >/dev/null || { echo "$ENGINE is not installed" >&2; exit 1; }

RUNTIME_BASE="${XDG_RUNTIME_DIR:-/tmp}"
if [ -z "$DATA_DIR" ]; then
  DATA_DIR="$(mktemp -d "$RUNTIME_BASE/hh-tor-XXXXXX")"
fi
mkdir -p "$DATA_DIR"
chmod 700 "$DATA_DIR"

CONTROL_SOCKET="$DATA_DIR/control.sock"
SOCKS_SOCKET="$DATA_DIR/socks.sock"
TORRC="$DATA_DIR/torrc"
LOGFILE="$DATA_DIR/tor.log"

cat > "$TORRC" <<EOF
SocksPort unix:$SOCKS_SOCKET
ControlSocket $CONTROL_SOCKET
CookieAuthentication 1
DataDirectory $DATA_DIR/state
Log notice file $LOGFILE
RunAsDaemon 0
EOF
mkdir -p "$DATA_DIR/state"
chmod 700 "$DATA_DIR/state"

TOR_PID=""
cleanup() {
  [ -n "$TOR_PID" ] && kill -TERM "$TOR_PID" 2>/dev/null || true
  if [ "$ENGINE" = podman ]; then
    podman rm -f "hh-tor-$$" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT INT TERM

if [ "$ENGINE" = bwrap ]; then
  bwrap \
    --unshare-pid --unshare-uts --unshare-ipc --unshare-cgroup \
    --die-with-parent --new-session \
    --ro-bind /usr /usr --ro-bind /etc /etc --ro-bind /run /run \
    --symlink usr/bin /bin --symlink usr/lib /lib --symlink usr/lib64 /lib64 --symlink usr/sbin /sbin \
    --proc /proc --dev /dev --tmpfs /tmp \
    --bind "$DATA_DIR" "$DATA_DIR" \
    tor -f "$TORRC" &
  TOR_PID=$!
else
  # Requires an image with `tor` already installed — build one (e.g. a Debian
  # base + `apt-get install -y tor`) rather than installing at launch time;
  # this script won't do a network install on every run. Override the image
  # with TOR_IMAGE if the default isn't available locally.
  TOR_IMAGE="${TOR_IMAGE:-localhost/hh-tor:latest}"
  podman image exists "$TOR_IMAGE" || {
    echo "podman image '$TOR_IMAGE' not found — build one with tor installed" \
         "(or set TOR_IMAGE) before using --engine podman" >&2
    exit 1
  }
  podman run --rm --name "hh-tor-$$" \
    --network=host \
    --cap-drop=ALL --security-opt=no-new-privileges --read-only \
    --tmpfs /tmp \
    -v "$DATA_DIR:$DATA_DIR" \
    "$TOR_IMAGE" tor -f "$TORRC" &
  TOR_PID=$!
fi

for _ in $(seq 1 60); do
  grep -q "Bootstrapped 100%" "$LOGFILE" 2>/dev/null && break
  kill -0 "$TOR_PID" 2>/dev/null || { echo "tor exited before bootstrapping — see $LOGFILE" >&2; exit 1; }
  sleep 1
done
grep -q "Bootstrapped 100%" "$LOGFILE" 2>/dev/null || { echo "tor did not bootstrap in time — see $LOGFILE" >&2; exit 1; }

echo "CONTROL_SOCKET=$CONTROL_SOCKET"
echo "SOCKS_SOCKET=$SOCKS_SOCKET"

wait "$TOR_PID"
