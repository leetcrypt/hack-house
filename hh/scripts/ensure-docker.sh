#!/usr/bin/env bash
# ensure-docker.sh — make sure Docker is installed AND its daemon is up before
# /sbx launch docker.
#
# Two jobs, detect-first and never silent:
#   1. If the docker binary is missing, optionally INSTALL it (--install) from
#      Docker's official, GPG-verified apt repo on Debian/Ubuntu (docker-ce),
#      with dnf (Fedora/RHEL) and pacman (Arch) fallbacks. We deliberately do
#      NOT pipe get.docker.com into a root shell.
#   2. If the daemon is down, start it (and wait until it accepts connections).
#
# Anything already in place is left untouched (idempotent).
#
# usage:
#   ./ensure-docker.sh           # interactive: prompt before starting the daemon
#   ./ensure-docker.sh --yes     # start (and install, if --install) without prompting
#   ./ensure-docker.sh --install # install Docker if the binary is missing, then start
#   ./ensure-docker.sh --check   # test only; exit 0 if daemon up, 1 if not (no changes)
#   ./ensure-docker.sh --plan    # show the install plan; change nothing
set -uo pipefail

ASSUME_YES=0
CHECK_ONLY=0
DO_INSTALL=0
PLAN_ONLY=0
for arg in "$@"; do
    case "$arg" in
        -y|--yes)         ASSUME_YES=1 ;;
        --check)          CHECK_ONLY=1 ;;
        --install)        DO_INSTALL=1 ;;
        --plan|--dry-run) PLAN_ONLY=1; DO_INSTALL=1 ;;
        -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "✖ unknown arg: $arg" >&2; exit 2 ;;
    esac
done

daemon_up()  { docker info >/dev/null 2>&1; }
have_docker() { command -v docker >/dev/null 2>&1; }

# ── Work out how to install Docker on this platform ──────────────────────────
# Emits the ordered list of commands (one per line) to PLAN_LINES; empty if we
# don't know how. Uses Docker's official repo so packages are GPG-verified and
# current (distro packages like docker.io are often stale).
build_install_plan() {
    PLAN_LINES=""
    [[ "$(uname -s)" == "Linux" ]] || { PLAN_LINES=""; return; }
    # shellcheck disable=SC1091
    local id="" id_like=""
    if [[ -r /etc/os-release ]]; then
        id="$(. /etc/os-release; echo "${ID:-}")"
        id_like="$(. /etc/os-release; echo "${ID_LIKE:-}")"
    fi
    case "$id $id_like" in
        *debian*|*ubuntu*)
            local repo="ubuntu"; [[ "$id" == "debian" ]] && repo="debian"
            PLAN_LINES=$(cat <<EOF
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/$repo/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=\$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/$repo \$(. /etc/os-release && echo \${VERSION_CODENAME}) stable" | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
EOF
)
            ;;
        *fedora*|*rhel*|*centos*)
            local repo="fedora"; case " $id $id_like " in *rhel*|*centos*) repo="centos";; esac
            PLAN_LINES=$(cat <<EOF
sudo dnf -y install dnf-plugins-core
sudo dnf config-manager --add-repo https://download.docker.com/linux/$repo/docker-ce.repo
sudo dnf install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
EOF
)
            ;;
        *arch*)
            PLAN_LINES="sudo pacman -S --noconfirm docker"
            ;;
    esac
}

# ── --plan: show the install plan and change nothing ─────────────────────────
if [[ $PLAN_ONLY -eq 1 ]]; then
    if have_docker; then
        echo "Docker already installed ($(docker --version 2>/dev/null)) — nothing to install" >&2
        exit 0
    fi
    build_install_plan
    if [[ -z "$PLAN_LINES" ]]; then
        echo "✖ don't know how to install Docker here — see https://docs.docker.com/engine/install/" >&2
        exit 1
    fi
    echo "plan (no changes will be made) — would run:" >&2
    printf '  %s\n' "$PLAN_LINES" >&2
    exit 0
fi

if daemon_up; then
    [[ $CHECK_ONLY -eq 1 ]] || echo "docker daemon already running" >&2
    exit 0
fi
[[ $CHECK_ONLY -eq 1 ]] && exit 1

if ! have_docker; then
    if [[ $DO_INSTALL -ne 1 ]]; then
        echo "✖ docker is not installed — re-run with --install to install it" >&2
        exit 127
    fi
    build_install_plan
    if [[ -z "$PLAN_LINES" ]]; then
        echo "✖ don't know how to install Docker here — see https://docs.docker.com/engine/install/" >&2
        exit 1
    fi
    echo "Docker is not installed. The following will run (Docker's official repo):" >&2
    printf '  %s\n' "$PLAN_LINES" >&2
    if [[ $ASSUME_YES -ne 1 ]]; then
        printf 'Proceed with install? [y/N] ' >&2
        read -r reply
        case "$reply" in
            y|Y|yes|YES) ;;
            *) echo "✖ aborted — Docker left uninstalled" >&2; exit 1 ;;
        esac
    fi
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        echo "+ $line" >&2
        eval "$line" || { echo "✖ install step failed: $line" >&2; exit 1; }
    done <<< "$PLAN_LINES"
    have_docker || { echo "✖ install ran but docker is still not callable — check the install log" >&2; exit 1; }
    echo "Docker installed ($(docker --version 2>/dev/null))" >&2
    # On Linux a fresh install usually needs the daemon started below; fall through.
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
