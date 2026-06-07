#!/usr/bin/env bash
# ensure-docker.sh — make sure the Docker daemon is up before /sbx launch docker.
#
# Without this, `docker run` fails with "Cannot connect to the Docker daemon"
# and the sandbox launch dies with a raw error. This script detects a dead
# daemon and — after confirmation — starts it, then waits until it's accepting
# connections.
#
# usage:
#   ./ensure-docker.sh          # interactive: prompt before starting the daemon
#   ./ensure-docker.sh --yes    # start without prompting (used by hack-house --start)
#   ./ensure-docker.sh --check  # test only; exit 0 if up, 1 if down (no changes)
set -uo pipefail

ASSUME_YES=0
CHECK_ONLY=0
for arg in "$@"; do
    case "$arg" in
        -y|--yes)  ASSUME_YES=1 ;;
        --check)   CHECK_ONLY=1 ;;
        -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "✖ unknown arg: $arg" >&2; exit 2 ;;
    esac
done

daemon_up() { docker info >/dev/null 2>&1; }

if daemon_up; then
    [[ $CHECK_ONLY -eq 1 ]] || echo "docker daemon already running" >&2
    exit 0
fi
[[ $CHECK_ONLY -eq 1 ]] && exit 1

if ! command -v docker >/dev/null 2>&1; then
    echo "✖ docker is not installed — install Docker first" >&2
    exit 127
fi

# Work out how to start the daemon on this platform.
start_cmd=""
need_sudo=0
case "$(uname -s)" in
    Linux)
        if command -v systemctl >/dev/null 2>&1; then
            start_cmd="systemctl start docker"; need_sudo=1
        elif command -v service >/dev/null 2>&1; then
            start_cmd="service docker start"; need_sudo=1
        fi
        ;;
    Darwin)
        # Docker Desktop: opening the app boots the daemon VM.
        start_cmd="open -a Docker"
        ;;
esac

if [[ -z "$start_cmd" ]]; then
    echo "✖ don't know how to start the docker daemon here — start it manually" >&2
    exit 1
fi
[[ $need_sudo -eq 1 ]] && start_cmd="sudo $start_cmd"

# Confirmation (skipped with --yes).
if [[ $ASSUME_YES -ne 1 ]]; then
    printf 'docker daemon is not running. Start it with "%s"? [y/N] ' "$start_cmd" >&2
    read -r reply
    case "$reply" in
        y|Y|yes|YES) ;;
        *) echo "✖ aborted — docker daemon left stopped" >&2; exit 1 ;;
    esac
fi

echo "starting docker daemon: $start_cmd" >&2
eval "$start_cmd" || { echo "✖ failed to start docker daemon (sudo password needed? run it in a terminal)" >&2; exit 1; }

# Wait for it to accept connections (Desktop / a fresh VM can take a while).
for _ in $(seq 1 60); do
    daemon_up && { echo "docker daemon is up" >&2; exit 0; }
    sleep 1
done

echo "✖ docker daemon did not come up in time" >&2
exit 1
