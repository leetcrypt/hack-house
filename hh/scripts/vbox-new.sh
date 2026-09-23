#!/usr/bin/env bash
# vbox-new.sh — create + boot a FRESH Ubuntu VirtualBox VM, pre-provisioned with
# the hack-house dev toolchain via cloud-init.
#
# VirtualBox guests can't be `docker exec`/`multipass exec`'d into, so the
# launch-time sandbox-bootstrap.sh path doesn't reach them. Instead we hand the
# guest a cloud-init NoCloud seed ISO whose user-data installs the same package
# set (scripts/sandbox-tools.json) and creates a sudo login user on first boot.
#
# Detect-first, never silent: if the named VM already exists this refuses rather
# than clobber it. --plan prints exactly what it would download/create and
# changes nothing.
#
# usage:
#   ./vbox-new.sh [--name hh-vbox] [--user dell] [--cpus 2] [--mem 2048]
#                 [--disk 15] [--release 24.04] [--pass <pw>] [--pubkey <file>]
#                 [--no-boot] [--plan] [--yes]
#
# Deps: VBoxManage, qemu-img (convert the cloud image to VDI), and one of
#       cloud-localds | genisoimage | mkisofs | xorriso (build the seed ISO).
set -uo pipefail

NAME="hh-vbox"
VM_USER="${USER:-hacker}"
CPUS=2
MEM=2048          # MiB
DISK=15           # GiB (cloud image grows to fill it on first boot via growpart)
RELEASE="24.04"
PASSWORD=""       # empty → generate a random one and print it once
PUBKEY=""         # path to an SSH public key to authorize (auto-detects if empty)
DO_BOOT=1
PLAN_ONLY=0
ASSUME_YES=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --name)      NAME="$2"; shift 2 ;;
        --name=*)    NAME="${1#*=}"; shift ;;
        --user|--name-user) VM_USER="$2"; shift 2 ;;
        --user=*)    VM_USER="${1#*=}"; shift ;;
        --cpus)      CPUS="$2"; shift 2 ;;
        --cpus=*)    CPUS="${1#*=}"; shift ;;
        --mem)       MEM="$2"; shift 2 ;;
        --mem=*)     MEM="${1#*=}"; shift ;;
        --disk)      DISK="$2"; shift 2 ;;
        --disk=*)    DISK="${1#*=}"; shift ;;
        --release)   RELEASE="$2"; shift 2 ;;
        --release=*) RELEASE="${1#*=}"; shift ;;
        --pass)      PASSWORD="$2"; shift 2 ;;
        --pass=*)    PASSWORD="${1#*=}"; shift ;;
        --pubkey)    PUBKEY="$2"; shift 2 ;;
        --pubkey=*)  PUBKEY="${1#*=}"; shift ;;
        --no-boot)   DO_BOOT=0; shift ;;
        --plan|--dry-run) PLAN_ONLY=1; shift ;;
        -y|--yes)    ASSUME_YES=1; shift ;;
        -h|--help)   grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "✖ unknown arg: $1" >&2; exit 2 ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOLS_JSON="$SCRIPT_DIR/sandbox-tools.json"
CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/hh/vbox"
IMG_NAME="ubuntu-${RELEASE}-server-cloudimg-amd64.img"
IMG_URL="https://cloud-images.ubuntu.com/releases/${RELEASE}/release/${IMG_NAME}"
IMG_PATH="$CACHE_DIR/$IMG_NAME"

# ── dependency probes ────────────────────────────────────────────────────────
need() { command -v "$1" >/dev/null 2>&1; }

missing=()
need VBoxManage || missing+=("VBoxManage (install VirtualBox; see ensure-vbox.sh)")
need qemu-img   || missing+=("qemu-img (apt install qemu-utils)")

# Seed-ISO builder: prefer cloud-localds, fall back to a generic ISO maker.
SEED_TOOL=""
for t in cloud-localds genisoimage mkisofs xorriso; do
    if need "$t"; then SEED_TOOL="$t"; break; fi
done
[[ -z "$SEED_TOOL" ]] && missing+=("cloud-localds OR genisoimage/mkisofs/xorriso (apt install cloud-image-utils genisoimage)")

if [[ ${#missing[@]} -gt 0 ]]; then
    echo "✖ missing required tools:" >&2
    printf '   - %s\n' "${missing[@]}" >&2
    exit 1
fi

# ── package list (mirrors the Rust guarantee: vim + curl always present) ─────
read_pkgs() {
    if need jq && [[ -f "$TOOLS_JSON" ]]; then
        jq -r '.packages[]?' "$TOOLS_JSON" 2>/dev/null
    fi
}
# Collect, force vim+curl, dedupe while preserving order.
declare -A seen
PKG_LIST=()
for p in vim curl $(read_pkgs); do
    [[ -n "$p" && -z "${seen[$p]:-}" ]] || continue
    seen[$p]=1
    PKG_LIST+=("$p")
done

# ── refuse to clobber an existing VM ─────────────────────────────────────────
if VBoxManage showvminfo "$NAME" >/dev/null 2>&1; then
    echo "✖ a VM named '$NAME' already exists — pick another --name, or boot it with /sbx gui $NAME" >&2
    exit 1
fi

# ── plan mode: describe, change nothing ──────────────────────────────────────
if [[ $PLAN_ONLY -eq 1 ]]; then
    echo "plan (no changes will be made):" >&2
    echo "  base image : $IMG_URL" >&2
    echo "             → cache $IMG_PATH $( [[ -f "$IMG_PATH" ]] && echo '(already downloaded)' || echo '(~600MB download)' )" >&2
    echo "  VM         : $NAME · ${CPUS} cpu · ${MEM}MiB · ${DISK}GiB disk · NAT" >&2
    echo "  login user : $VM_USER (sudo, NOPASSWD)" >&2
    echo "  seed tool  : $SEED_TOOL" >&2
    echo "  packages   : ${PKG_LIST[*]}" >&2
    echo "  boot GUI   : $( [[ $DO_BOOT -eq 1 ]] && echo yes || echo no )" >&2
    exit 0
fi

# ── confirmation (skipped with --yes) ────────────────────────────────────────
if [[ $ASSUME_YES -ne 1 ]]; then
    if [[ ! -f "$IMG_PATH" ]]; then
        printf 'Create VM "%s" (downloads ~600MB Ubuntu %s cloud image first)? [y/N] ' "$NAME" "$RELEASE" >&2
    else
        printf 'Create VM "%s" from cached Ubuntu %s image? [y/N] ' "$NAME" "$RELEASE" >&2
    fi
    read -r reply
    case "$reply" in y|Y|yes|YES) ;; *) echo "✖ aborted" >&2; exit 1 ;; esac
fi

mkdir -p "$CACHE_DIR"

# ── 1. fetch the cloud image (cached) ────────────────────────────────────────
if [[ ! -f "$IMG_PATH" ]]; then
    echo "↓ downloading $IMG_NAME …" >&2
    if need curl; then
        curl -fL --retry 3 -o "$IMG_PATH.partial" "$IMG_URL" || { echo "✖ download failed" >&2; rm -f "$IMG_PATH.partial"; exit 1; }
    elif need wget; then
        wget -O "$IMG_PATH.partial" "$IMG_URL" || { echo "✖ download failed" >&2; rm -f "$IMG_PATH.partial"; exit 1; }
    else
        echo "✖ neither curl nor wget available to fetch the image" >&2; exit 1
    fi
    mv "$IMG_PATH.partial" "$IMG_PATH"
fi

# ── 2. register the VM (creates its folder) ──────────────────────────────────
echo "† creating VM '$NAME' …" >&2
VBoxManage createvm --name "$NAME" --ostype Ubuntu_64 --register >/dev/null
MACHINE_FOLDER="$(VBoxManage list systemproperties | sed -n 's/^Default machine folder: *//p')"
VMDIR="$MACHINE_FOLDER/$NAME"
VDI="$VMDIR/$NAME.vdi"
SEED_ISO="$VMDIR/seed.iso"

# Clean up a half-built VM if anything below fails.
cleanup() { VBoxManage unregistervm "$NAME" --delete >/dev/null 2>&1 || true; }
trap 'echo "✖ build failed — rolling back VM" >&2; cleanup' ERR
set -e

# ── 3. cloud-image → VDI, grown to the requested size ────────────────────────
echo "† converting cloud image → VDI …" >&2
qemu-img convert -O vdi "$IMG_PATH" "$VDI"
VBoxManage modifymedium disk "$VDI" --resize "$((DISK * 1024))" >/dev/null

# ── 4. build the cloud-init NoCloud seed ─────────────────────────────────────
# Credentials: a random password is generated unless --pass was given, and any
# available SSH public key is authorized so password login isn't the only door.
if [[ -z "$PASSWORD" ]]; then
    if need openssl; then PASSWORD="$(openssl rand -base64 12)"; else PASSWORD="hackhouse-$RANDOM"; fi
    GENERATED_PW=1
else
    GENERATED_PW=0
fi
if [[ -z "$PUBKEY" ]]; then
    for k in "$HOME"/.ssh/id_ed25519.pub "$HOME"/.ssh/id_rsa.pub; do
        [[ -f "$k" ]] && { PUBKEY="$k"; break; }
    done
fi

WORK="$(mktemp -d)"
trap 'echo "✖ build failed — rolling back VM" >&2; cleanup; rm -rf "$WORK"' ERR
{
    echo "instance-id: $NAME"
    echo "local-hostname: $NAME"
} > "$WORK/meta-data"

{
    echo "#cloud-config"
    echo "hostname: $NAME"
    echo "ssh_pwauth: true"
    echo "users:"
    echo "  - name: $VM_USER"
    echo "    groups: [sudo]"
    echo "    sudo: 'ALL=(ALL) NOPASSWD:ALL'"
    echo "    shell: /bin/bash"
    echo "    lock_passwd: false"
    if [[ -n "$PUBKEY" ]]; then
        echo "    ssh_authorized_keys:"
        echo "      - $(cat "$PUBKEY")"
    fi
    echo "chpasswd:"
    echo "  expire: false"
    echo "  users:"
    echo "    - {name: $VM_USER, password: '$PASSWORD', type: text}"
    echo "package_update: true"
    echo "packages:"
    for p in "${PKG_LIST[@]}"; do echo "  - $p"; done
} > "$WORK/user-data"

echo "† building cloud-init seed ($SEED_TOOL) …" >&2
case "$SEED_TOOL" in
    cloud-localds)
        cloud-localds "$SEED_ISO" "$WORK/user-data" "$WORK/meta-data"
        ;;
    genisoimage|mkisofs)
        "$SEED_TOOL" -output "$SEED_ISO" -volid cidata -joliet -rock \
            "$WORK/user-data" "$WORK/meta-data" >/dev/null 2>&1
        ;;
    xorriso)
        xorriso -as mkisofs -output "$SEED_ISO" -volid cidata -joliet -rock \
            "$WORK/user-data" "$WORK/meta-data" >/dev/null 2>&1
        ;;
esac
rm -rf "$WORK"
trap 'echo "✖ build failed — rolling back VM" >&2; cleanup' ERR

# ── 5. wire up storage + hardware ────────────────────────────────────────────
VBoxManage modifyvm "$NAME" \
    --cpus "$CPUS" --memory "$MEM" \
    --nic1 nat --graphicscontroller vmsvga --vram 32 \
    --ioapic on --audio-driver none --boot1 disk --boot2 dvd >/dev/null
VBoxManage storagectl "$NAME" --name SATA --add sata --controller IntelAhci --portcount 2 >/dev/null
VBoxManage storageattach "$NAME" --storagectl SATA --port 0 --device 0 --type hdd --medium "$VDI" >/dev/null
VBoxManage storageattach "$NAME" --storagectl SATA --port 1 --device 0 --type dvddrive --medium "$SEED_ISO" >/dev/null

trap - ERR   # past the point of automatic rollback

echo "✓ VM '$NAME' created (user '$VM_USER')" >&2
if [[ "${GENERATED_PW:-0}" -eq 1 ]]; then
    echo "  login password (generated, shown once): $PASSWORD" >&2
fi
[[ -n "$PUBKEY" ]] && echo "  authorized SSH key: $PUBKEY" >&2
echo "  cloud-init runs the toolchain install on first boot (give it a minute)." >&2

# ── 6. boot ──────────────────────────────────────────────────────────────────
if [[ $DO_BOOT -eq 1 ]]; then
    echo "† booting '$NAME' (GUI) …" >&2
    VBoxManage startvm "$NAME" --type gui >/dev/null
    echo "✓ launched $NAME" >&2
else
    echo "ⓘ not booting (--no-boot); start later with: /sbx gui $NAME" >&2
fi
