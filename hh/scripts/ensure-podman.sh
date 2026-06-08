#!/usr/bin/env bash
# ensure-podman.sh — make sure Podman is installed before /sbx launch podman.
#
# Detect-first, never silent: if Podman is already present this exits 0 and
# changes nothing. If it's missing it prints the EXACT command it would run and
# only installs with explicit consent (--yes). --plan shows a real, no-change
# plan so you can see what would be fetched.
#
# Podman is daemonless and rootless, so unlike Docker there's no daemon to start
# and no service to enable. The one rootless prerequisite is a subuid/subgid
# range for the current user (so the container can map a user namespace); this
# script checks for it and prints how to add one if it's missing, but never edits
# those files for you (they're security-sensitive and usually pre-seeded).
#
# usage:
#   ./ensure-podman.sh           # interactive: prompt before installing
#   ./ensure-podman.sh --yes     # install without prompting
#   ./ensure-podman.sh --check   # test only; exit 0 if present, 1 if missing
#   ./ensure-podman.sh --plan    # show the install plan; change nothing
set -uo pipefail

ASSUME_YES=0
CHECK_ONLY=0
PLAN_ONLY=0
for arg in "$@"; do
    case "$arg" in
        -y|--yes)         ASSUME_YES=1 ;;
        --check)          CHECK_ONLY=1 ;;
        --plan|--dry-run) PLAN_ONLY=1 ;;
        -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "✖ unknown arg: $arg" >&2; exit 2 ;;
    esac
done

installed() { command -v podman >/dev/null 2>&1 && podman --version >/dev/null 2>&1; }
pm_version() { podman --version 2>/dev/null | head -1; }

# Warn (don't fix) if the current user has no subuid/subgid range — rootless
# Podman needs one to map a user namespace. Most distros seed it on user
# creation; if it's absent the user adds it once with usermod.
check_rootless_preflight() {
    local u="${USER:-$(id -un)}"
    if ! grep -q "^${u}:" /etc/subuid 2>/dev/null || ! grep -q "^${u}:" /etc/subgid 2>/dev/null; then
        echo "ⓘ rootless preflight: no subuid/subgid range for '$u'." >&2
        echo "  add one once with: sudo usermod --add-subuids 100000-165535 --add-subgids 100000-165535 $u" >&2
    fi
}

# Already present: report, run the preflight, and stop (idempotent). --plan still
# prints the plan.
if installed; then
    if [[ $PLAN_ONLY -ne 1 ]]; then
        if [[ $CHECK_ONLY -ne 1 ]]; then
            echo "Podman already installed ($(pm_version)) — nothing to do" >&2
            check_rootless_preflight
        fi
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
        if command -v apt-get >/dev/null 2>&1; then
            install_cmd="apt-get update && apt-get install -y podman"
            plan_cmd="apt-cache policy podman"
            need_sudo=1
        elif command -v dnf >/dev/null 2>&1; then
            install_cmd="dnf install -y podman"
            plan_cmd="dnf info podman"
            need_sudo=1
        elif command -v pacman >/dev/null 2>&1; then
            install_cmd="pacman -S --noconfirm podman"
            plan_cmd="pacman -Si podman"
            need_sudo=1
        fi
        ;;
    Darwin)
        if command -v brew >/dev/null 2>&1; then
            install_cmd="brew install podman"
            plan_cmd="brew info podman"
            manual_note="macOS: after install run 'podman machine init && podman machine start' once."
        fi
        ;;
    MINGW*|MSYS*|CYGWIN*)
        if command -v winget >/dev/null 2>&1; then
            install_cmd="winget install -e --id RedHat.Podman"
            plan_cmd="winget show -e --id RedHat.Podman"
        fi
        ;;
esac

if [[ -z "$install_cmd" ]]; then
    echo "✖ don't know how to install Podman here — get it from https://podman.io/docs/installation" >&2
    exit 1
fi
[[ $need_sudo -eq 1 ]] && install_cmd="sudo $install_cmd"
[[ -n "$manual_note" ]] && echo "ⓘ $manual_note" >&2

# --plan: show the real plan and change nothing.
if [[ $PLAN_ONLY -eq 1 ]]; then
    echo "plan (no changes will be made): $plan_cmd" >&2
    eval "$plan_cmd"
    exit 0
fi

# Confirmation (skipped with --yes).
if [[ $ASSUME_YES -ne 1 ]]; then
    printf 'Podman is not installed. Install it with "%s"? [y/N] ' "$install_cmd" >&2
    read -r reply
    case "$reply" in
        y|Y|yes|YES) ;;
        *) echo "✖ aborted — Podman left uninstalled" >&2; exit 1 ;;
    esac
fi

echo "installing Podman: $install_cmd" >&2
eval "$install_cmd" || { echo "✖ install failed (sudo password needed? run it in a terminal)" >&2; exit 1; }

# Confirm the binary is now callable, then run the rootless preflight.
if installed; then
    echo "Podman is ready ($(pm_version))" >&2
    check_rootless_preflight
    exit 0
fi
echo "✖ install ran but podman is still not callable — check the install log" >&2
exit 1
