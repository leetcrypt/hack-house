#!/usr/bin/env bash
# sandbox-bootstrap.sh — install a baseline dev toolchain inside a hack-house
# Docker sandbox.
#
# It is piped to `bash -s` (as root) inside the container at provision time, so
# fresh sandboxes come up usable instead of bare. Idempotent + sentinel-guarded:
# a re-provision (new member joins) or a snapshot load is a fast no-op.
#
# When launched by hh, the package list comes from scripts/sandbox-tools.json
# (the canonical, editable schema) via $HH_SBX_PKGS. DEFAULT_PKGS below is only
# the fallback for running this script standalone. Per-launch override:
#   HH_SBX_PKGS="vim tmux ripgrep" ./host-house.sh ...
set -uo pipefail

# -h/--help: print the usage header above and exit. (No effect in normal use —
# hh pipes this to `bash -s` inside the container with no args; the flag is for
# running it standalone to read what it does.)
case "${1:-}" in
  -h|--help|-help) sed -n '2,/^set /{/^set /d;s/^# \{0,1\}//;p}' "$0"; exit 0 ;;
esac

SENTINEL=/var/lib/hh-bootstrap.done
[[ -f "$SENTINEL" ]] && exit 0      # this container is already provisioned

# Baseline dev tools: editors, fetchers, vcs, net + inspect utilities, json,
# archives. Keep names valid for the base image (ubuntu:24.04) — an unknown
# package would otherwise abort the whole batch install.
DEFAULT_PKGS="vim nano less curl wget ca-certificates git \
build-essential pkg-config \
procps iproute2 iputils-ping net-tools \
jq unzip zip tree htop file ripgrep \
python3 python3-pip python3-venv"

PKGS="${HH_SBX_PKGS:-$DEFAULT_PKGS}"

export DEBIAN_FRONTEND=noninteractive

# Refresh the index first (base images ship without /var/lib/apt/lists). If the
# update fails (e.g. no network) we bail WITHOUT writing the sentinel, so the
# next launch retries instead of leaving a half-provisioned shell.
apt-get update -qq || exit 0

# --no-install-recommends keeps the image lean. apt is atomic on resolution, so
# if one name is unavailable the batch aborts — fall back to one-by-one so a
# single bad/missing package can't deprive the shell of everything else.
# shellcheck disable=SC2086
if ! apt-get install -y --no-install-recommends $PKGS; then
    for p in $PKGS; do
        apt-get install -y --no-install-recommends "$p" || true
    done
fi

mkdir -p "$(dirname "$SENTINEL")"
date -u +%FT%TZ > "$SENTINEL"       # mark done; skip on the next provision pass
