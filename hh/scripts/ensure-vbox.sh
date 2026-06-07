#!/usr/bin/env bash
# ensure-vbox.sh — make sure VirtualBox is installed before /sbx launch virtualbox
# or /sbx gui <vm>.
#
# Detect-first, never silent: if VirtualBox is already present this exits 0 and
# changes nothing. If it's missing it prints the EXACT command it would run and
# only installs with explicit consent (--yes). --plan shows a real, no-change
# download plan (apt's own --simulate) so you can see what would be fetched.
#
# usage:
#   ./ensure-vbox.sh           # interactive: prompt before installing
#   ./ensure-vbox.sh --yes     # install without prompting (used by --install)
#   ./ensure-vbox.sh --check   # test only; exit 0 if present, 1 if missing
#   ./ensure-vbox.sh --plan    # show the install/download plan; change nothing
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

# HH_VBOX_FORCE_MISSING=1 lets a demo exercise the missing→install path without
# actually uninstalling anything (the probe is the single source of truth).
installed() {
    [[ "${HH_VBOX_FORCE_MISSING:-0}" = "1" ]] && return 1
    command -v VBoxManage >/dev/null 2>&1 && VBoxManage --version >/dev/null 2>&1
}

vbox_version() { VBoxManage --version 2>/dev/null | head -1; }

# Already present: report and stop (idempotent). --plan still prints the plan.
if installed; then
    if [[ $PLAN_ONLY -ne 1 ]]; then
        [[ $CHECK_ONLY -eq 1 ]] || echo "VirtualBox already installed ($(vbox_version)) — nothing to do" >&2
        exit 0
    fi
fi
[[ $CHECK_ONLY -eq 1 ]] && exit 1

# Work out how to install on this platform, and the matching --simulate/plan cmd.
# The plan command is deliberately runnable WITHOUT root where the tool allows it
# (apt --simulate does), so the download plan can be shown with zero privilege.
install_cmd=""
plan_cmd=""
need_sudo=0
plan_sudo=0
manual_note=""
case "$(uname -s)" in
    Linux)
        if command -v apt-get >/dev/null 2>&1; then
            install_cmd="apt-get install -y virtualbox"
            plan_cmd="apt-get install --simulate virtualbox"   # no root needed
            need_sudo=1
        elif command -v dnf >/dev/null 2>&1; then
            install_cmd="dnf install -y VirtualBox"
            plan_cmd="dnf install --assumeno VirtualBox"
            need_sudo=1; plan_sudo=1
        elif command -v pacman >/dev/null 2>&1; then
            install_cmd="pacman -S --noconfirm virtualbox"
            plan_cmd="pacman -S --print virtualbox"
            need_sudo=1; plan_sudo=1
        fi
        ;;
    Darwin)
        if command -v brew >/dev/null 2>&1; then
            install_cmd="brew install --cask virtualbox"
            plan_cmd="brew info --cask virtualbox"
            manual_note="macOS: you'll be asked to approve Oracle's kernel extension in System Settings → Privacy & Security, then reboot."
        fi
        ;;
    MINGW*|MSYS*|CYGWIN*)
        if command -v winget >/dev/null 2>&1; then
            install_cmd="winget install -e --id Oracle.VirtualBox"
            plan_cmd="winget show -e --id Oracle.VirtualBox"
        fi
        ;;
esac

if [[ -z "$install_cmd" ]]; then
    echo "✖ don't know how to install VirtualBox here — get it from https://www.virtualbox.org/wiki/Downloads" >&2
    exit 1
fi
[[ $need_sudo -eq 1 ]] && install_cmd="sudo $install_cmd"
[[ $plan_sudo -eq 1 ]] && plan_cmd="sudo $plan_cmd"

# Secure Boot needs the vboxdrv kernel module signed/enrolled (MOK) or it won't
# load. We check and warn rather than fail opaquely — it's a one-time manual step.
if command -v mokutil >/dev/null 2>&1; then
    if mokutil --sb-state 2>/dev/null | grep -qi 'SecureBoot enabled'; then
        echo "⚠ Secure Boot is ENABLED — after install you must enroll a MOK so the" >&2
        echo "  vboxdrv kernel module can load (the installer prompts for a password" >&2
        echo "  you re-enter at the blue MOK screen on next reboot)." >&2
    fi
fi
[[ -n "$manual_note" ]] && echo "ⓘ $manual_note" >&2

# --plan: show the real download/install plan and change nothing.
if [[ $PLAN_ONLY -eq 1 ]]; then
    echo "plan (no changes will be made): $plan_cmd" >&2
    eval "$plan_cmd"
    exit 0
fi

# Confirmation (skipped with --yes).
if [[ $ASSUME_YES -ne 1 ]]; then
    printf 'VirtualBox is not installed. Install it with "%s"? [y/N] ' "$install_cmd" >&2
    read -r reply
    case "$reply" in
        y|Y|yes|YES) ;;
        *) echo "✖ aborted — VirtualBox left uninstalled" >&2; exit 1 ;;
    esac
fi

echo "installing VirtualBox: $install_cmd" >&2
eval "$install_cmd" || { echo "✖ install failed (sudo password needed? run it in a terminal)" >&2; exit 1; }

# Confirm the binary is now callable.
if installed; then
    echo "VirtualBox is ready ($(vbox_version))" >&2
    exit 0
fi
echo "✖ install ran but VBoxManage is still not callable — check the install log" >&2
exit 1
