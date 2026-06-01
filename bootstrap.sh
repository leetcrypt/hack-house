#!/usr/bin/env bash
# bootstrap.sh — one-shot setup for hack-house
#
# Gets a fresh clone ready to run: checks prerequisites, creates a Python venv
# for the server, installs its dependencies, and builds the Rust client.
#
# usage:
#   ./bootstrap.sh              # full setup (venv + deps + cargo build)
#   ./bootstrap.sh --release    # build the client in release mode
#   ./bootstrap.sh --check      # only report what's installed; change nothing
#   ./bootstrap.sh -h | --help  # this help
#
# After it finishes, spin up a local test session with:  cd hh && ./lets-hack.sh
set -uo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
VENV="$ROOT/.venv"
HH_DIR="$ROOT/hh"

RELEASE=0
CHECK_ONLY=0
for arg in "$@"; do
    case "$arg" in
        --release) RELEASE=1 ;;
        --check)   CHECK_ONLY=1 ;;
        -h|--help|-help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "✖ unknown arg: $arg (try --release / --check / --help)" >&2; exit 2 ;;
    esac
done

have() { command -v "$1" >/dev/null 2>&1; }

# 1. Prerequisites. python3 + cargo are required; tmux/docker/multipass are
#    optional (only needed for the test harness / certain sandbox backends).
echo "checking prerequisites…"
missing=0
for bin in python3 cargo; do
    if have "$bin"; then echo "  ✓ $bin ($($bin --version 2>&1 | head -1))"
    else echo "  ✖ $bin — REQUIRED"; missing=1; fi
done
for bin in tmux docker multipass direnv; do
    if have "$bin"; then echo "  ✓ $bin (optional)"
    else echo "  · $bin not found (optional)"; fi
done
if [[ $missing -eq 1 ]]; then
    echo "✖ install the required tools above, then re-run ./bootstrap.sh" >&2
    exit 1
fi

if [[ $CHECK_ONLY -eq 1 ]]; then
    echo "--check: prerequisites OK, no changes made"
    exit 0
fi

# 2. Python venv + server dependencies.
if [[ ! -d "$VENV" ]]; then
    echo "creating venv at $VENV…"
    python3 -m venv "$VENV" || { echo "✖ could not create venv" >&2; exit 1; }
fi
echo "installing server dependencies…"
"$VENV/bin/pip" install --quiet --upgrade pip || true
"$VENV/bin/pip" install --quiet -r "$ROOT/requirements.txt" \
    || { echo "✖ pip install failed — see output above" >&2; exit 1; }
echo "  ✓ server deps installed into .venv"

# 3. Build the Rust client.
echo "building the client…"
if [[ $RELEASE -eq 1 ]]; then
    ( cd "$HH_DIR" && cargo build --release ) || { echo "✖ cargo build --release failed" >&2; exit 1; }
    echo "  ✓ client built: hh/target/release/hack-house"
else
    ( cd "$HH_DIR" && cargo build ) || { echo "✖ cargo build failed" >&2; exit 1; }
    echo "  ✓ client built: hh/target/debug/hack-house"
fi

echo
echo "ready. next steps:"
echo "    cd hh && ./lets-hack.sh          # local test session (server + clients in tmux)"
echo "    # or run the server + client by hand — see README.MD"
