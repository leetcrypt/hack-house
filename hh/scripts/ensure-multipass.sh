#!/usr/bin/env bash
# ensure-multipass.sh — make sure Multipass is installed before /sbx launch multipass.
#
# Detect-first, never silent: if Multipass is already present this exits 0 and
# changes nothing. If it's missing it prints the EXACT command it would run and
# only installs with explicit consent (--yes). --plan shows a real, no-change
# plan so you can see what would be fetched.
#
# On Linux, Multipass ships ONLY via snap (no apt/dnf package), so snapd must be
# present; if it isn't, this says so rather than guessing a package name.
#
# usage:
#   ./ensure-multipass.sh           # interactive: prompt before installing
#   ./ensure-multipass.sh --yes     # install without prompting
#   ./ensure-multipass.sh --check   # test only; exit 0 if present, 1 if missing
#   ./ensure-multipass.sh --plan    # show the install plan; change nothing
#   ./ensure-multipass.sh --stdin-pass  # read a sudo password from stdin (sudo -S)
set -uo pipefail

ASSUME_YES=0
CHECK_ONLY=0
PLAN_ONLY=0
STDIN_PASS=0
for arg in "$@"; do
    case "$arg" in
        -y|--yes)         ASSUME_YES=1 ;;
        --check)          CHECK_ONLY=1 ;;
        --plan|--dry-run) PLAN_ONLY=1 ;;
        # A sudo password is waiting on stdin (the hack-house TUI feeds it). Use
        # `sudo -S` so escalation reads that, never the controlling tty.
        --stdin-pass)     STDIN_PASS=1 ;;
        -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "✖ unknown arg: $arg" >&2; exit 2 ;;
    esac
done

# How to escalate (mirrors ensure-docker.sh):
#   * --stdin-pass: a password is on stdin → `sudo -S -p ''` (reads stdin, never
#     the tty; a raw-mode TUI would corrupt a tty prompt). First sudo caches it.
#   * --yes alone: `sudo -n` — fails fast if creds aren't cached, never hangs.
#   * interactive shell: plain `sudo` (a real terminal can prompt normally).
SUDO="sudo"
[[ $ASSUME_YES -eq 1 ]] && SUDO="sudo -n"
[[ $STDIN_PASS -eq 1 ]] && SUDO="sudo -S -p ''"

installed() { command -v multipass >/dev/null 2>&1 && multipass version >/dev/null 2>&1; }
mp_version() { multipass version 2>/dev/null | head -1; }

# Already present: report and stop (idempotent). --plan still prints the plan.
if installed; then
    if [[ $PLAN_ONLY -ne 1 ]]; then
        [[ $CHECK_ONLY -eq 1 ]] || echo "Multipass already installed ($(mp_version)) — nothing to do" >&2
        exit 0
    fi
fi
[[ $CHECK_ONLY -eq 1 ]] && exit 1

# Work out how to install on this platform, and the matching no-change plan cmd.
install_cmd=""
plan_cmd=""
need_sudo=0
manual_note=""
case "$(uname -s)" in
    Linux)
        if command -v snap >/dev/null 2>&1; then
            install_cmd="snap install multipass"
            plan_cmd="snap info multipass"        # no root needed to inspect
            need_sudo=1
        else
            echo "✖ Multipass on Linux requires snap (snapd), which isn't installed." >&2
            echo "  Install snapd first (e.g. 'sudo apt-get install snapd'), then re-run," >&2
            echo "  or see https://multipass.run/install for alternatives." >&2
            exit 1
        fi
        ;;
    Darwin)
        if command -v brew >/dev/null 2>&1; then
            install_cmd="brew install --cask multipass"
            plan_cmd="brew info --cask multipass"
            manual_note="macOS: Multipass installs a system helper; you may be prompted for your password."
        fi
        ;;
    MINGW*|MSYS*|CYGWIN*)
        if command -v winget >/dev/null 2>&1; then
            install_cmd="winget install -e --id Canonical.Multipass"
            plan_cmd="winget show -e --id Canonical.Multipass"
        fi
        ;;
esac

if [[ -z "$install_cmd" ]]; then
    echo "✖ don't know how to install Multipass here — get it from https://multipass.run/install" >&2
    exit 1
fi
[[ $need_sudo -eq 1 ]] && install_cmd="$SUDO $install_cmd"
[[ -n "$manual_note" ]] && echo "ⓘ $manual_note" >&2

# --plan: show the real plan and change nothing.
if [[ $PLAN_ONLY -eq 1 ]]; then
    echo "plan (no changes will be made): $plan_cmd" >&2
    eval "$plan_cmd"
    exit 0
fi

# Confirmation (skipped with --yes).
if [[ $ASSUME_YES -ne 1 ]]; then
    printf 'Multipass is not installed. Install it with "%s"? [y/N] ' "$install_cmd" >&2
    read -r reply
    case "$reply" in
        y|Y|yes|YES) ;;
        *) echo "✖ aborted — Multipass left uninstalled" >&2; exit 1 ;;
    esac
fi

echo "installing Multipass: $install_cmd" >&2
eval "$install_cmd" || { echo "✖ install failed (sudo password needed? run it in a terminal)" >&2; exit 1; }

# Confirm the binary is now callable.
if installed; then
    echo "Multipass is ready ($(mp_version))" >&2
    exit 0
fi
echo "✖ install ran but multipass is still not callable — check the install log" >&2
exit 1
