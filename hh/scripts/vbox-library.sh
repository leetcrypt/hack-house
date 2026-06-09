#!/usr/bin/env bash
# vbox-library.sh — browse + locally build VMs from the hack-house VM library.
#
# The repo ships ONLY pointers (scripts/vbox-library.json), never the multi-GB
# images themselves. When you choose a VM it is built on YOUR own machine here:
#   * cloudimg → hand off to vbox-new.sh (fully unattended cloud-init build)
#   * iso      → download the installer ISO, create a VM, attach it, boot the
#                installer (you finish setup in the VirtualBox window)
#   * ova      → download the appliance and `VBoxManage import` it
#   * manual   → pointer-only (licence/availability prevents an auto-build)
#
# Detect-first, never silent: --plan shows exactly what would be downloaded and
# created and changes nothing; an install refuses to clobber an existing VM and
# rolls a half-built one back. Direct URLs are best-effort + version-pinned: if
# one 404s, you get the official page and can supply a local file with --iso.
#
# usage:
#   ./vbox-library.sh --list                 # catalog (installed vs available)
#   ./vbox-library.sh --info <id>            # one entry's details + how to get it
#   ./vbox-library.sh --plan <id>            # show the build plan; change nothing
#   ./vbox-library.sh --install <id> [--iso <path>] [--no-boot] [--yes]
#
# Deps: jq (read the manifest), VBoxManage; curl|wget for downloads; for cloudimg
#       entries the deps of vbox-new.sh (qemu-img + a seed-ISO builder).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFEST="$SCRIPT_DIR/vbox-library.json"
VBOX_NEW="$SCRIPT_DIR/vbox-new.sh"
CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/hh/vbox-library"

ACTION=""
ID=""
ISO_OVERRIDE=""
DO_BOOT=1
ASSUME_YES=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --list)              ACTION="list"; shift ;;
        --info)              ACTION="info"; ID="${2:-}"; shift 2 ;;
        --plan|--dry-run)    ACTION="plan"; ID="${2:-}"; shift 2 ;;
        --install)           ACTION="install"; ID="${2:-}"; shift 2 ;;
        --iso)               ISO_OVERRIDE="${2:-}"; shift 2 ;;
        --iso=*)             ISO_OVERRIDE="${1#*=}"; shift ;;
        --no-boot)           DO_BOOT=0; shift ;;
        -y|--yes)            ASSUME_YES=1; shift ;;
        -h|--help)           grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "✖ unknown arg: $1" >&2; exit 2 ;;
    esac
done

need() { command -v "$1" >/dev/null 2>&1; }
need jq || { echo "✖ jq is required to read the VM library manifest (apt install jq)" >&2; exit 1; }
[[ -f "$MANIFEST" ]] || { echo "✖ manifest not found: $MANIFEST" >&2; exit 1; }

# A single jq query against one VM object, by id. Echoes the raw value ("" if null).
q() { jq -r --arg id "$ID" --arg k "$1" '.vms[] | select(.id==$id) | .[$k] // ""' "$MANIFEST"; }
vm_exists_in_manifest() { jq -e --arg id "$ID" '.vms[] | select(.id==$id)' "$MANIFEST" >/dev/null 2>&1; }
# Is a VM with this display name already registered in VirtualBox?
vbox_has() { VBoxManage list vms 2>/dev/null | grep -qiF "\"$1\""; }

# ── --list: print the catalog, marking what's already built locally ──────────
if [[ "$ACTION" == "list" ]]; then
    echo "hack-house VM library — pointers only; chosen VMs are built locally:" >&2
    while IFS=$'\t' read -r id name os size kind; do
        if vbox_has "$name"; then mark="✓ installed"; else mark="↓ available"; fi
        printf '  %-12s %-22s %-22s %-12s [%s]\n' "$mark" "$id" "$name" "$os" "$kind" >&2
    done < <(jq -r '.vms[] | [.id, .name, .os, .size, .kind] | @tsv' "$MANIFEST")
    echo "details: ./vbox-library.sh --info <id>   ·   build: ./vbox-library.sh --install <id>" >&2
    exit 0
fi

# Everything past here needs a valid id.
[[ -n "$ID" ]] || { echo "✖ no VM id given — see ./vbox-library.sh --list" >&2; exit 2; }
vm_exists_in_manifest || { echo "✖ unknown VM id '$ID' — see ./vbox-library.sh --list" >&2; exit 2; }

NAME="$(q name)"; OS="$(q os)"; SIZE="$(q size)"; KIND="$(q kind)"
URL="$(q url)"; PAGE="$(q page)"; NOTES="$(q notes)"
OSTYPE="$(q ostype)"; VERSION="$(q version)"
CPUS="$(q cpus)"; MEM="$(q mem_mb)"; DISK="$(q disk_gb)"
EFI="$(q efi)"; TPM="$(q tpm)"
[[ -n "$CPUS" ]] || CPUS=2
[[ -n "$MEM"  ]] || MEM=2048
[[ -n "$DISK" ]] || DISK=20

# ── --info: details + the honest "how to get it" pointer ─────────────────────
if [[ "$ACTION" == "info" ]]; then
    echo "● $NAME ($ID)" >&2
    echo "  os    : $OS" >&2
    echo "  size  : $SIZE" >&2
    echo "  build : $KIND" >&2
    [[ -n "$VERSION" ]] && echo "  pinned: v$VERSION" >&2
    if vbox_has "$NAME"; then
        echo "  state : ✓ already built locally — boot it with /sbx vbox \"$NAME\"" >&2
    else
        echo "  state : ↓ not installed" >&2
    fi
    [[ -n "$URL"  ]] && echo "  url   : $URL" >&2
    [[ -n "$PAGE" ]] && echo "  page  : $PAGE" >&2
    [[ -n "$NOTES" ]] && echo "  notes : $NOTES" >&2
    echo "build locally with: ./vbox-library.sh --install $ID" >&2
    exit 0
fi

# Where a downloaded image lands.
EXT="iso"; [[ "$KIND" == "ova" ]] && EXT="ova"
DL_PATH="$CACHE_DIR/${ID}.${EXT}"

# ── --plan: describe the build, change nothing ───────────────────────────────
if [[ "$ACTION" == "plan" ]]; then
    echo "plan for '$NAME' (no changes will be made):" >&2
    case "$KIND" in
        manual)
            echo "  ⚠ manual-only — cannot be auto-built (see notes)" >&2
            echo "  notes: $NOTES" >&2
            echo "  page : $PAGE" >&2
            ;;
        cloudimg)
            echo "  hand off to vbox-new.sh → unattended cloud-init build" >&2
            echo "  VM   : $NAME · ${CPUS} cpu · ${MEM}MiB · ${DISK}GiB · release ${VERSION:-24.04}" >&2
            "$VBOX_NEW" --name "$NAME" --release "${VERSION:-24.04}" \
                --cpus "$CPUS" --mem "$MEM" --disk "$DISK" --plan 2>&1 | sed 's/^/    /' >&2 || true
            ;;
        ova)
            echo "  source : ${URL:-<none — supply --iso/appliance>}" >&2
            echo "         → cache $DL_PATH" >&2
            echo "  import : VBoxManage import (registers '$NAME')" >&2
            ;;
        iso)
            iso_src="${ISO_OVERRIDE:-${URL:-<none>}}"
            echo "  iso    : $iso_src" >&2
            [[ -z "$ISO_OVERRIDE" && -n "$URL" ]] && echo "         → cache $DL_PATH" >&2
            echo "  VM     : $NAME · ${CPUS} cpu · ${MEM}MiB · ${DISK}GiB" >&2
            [[ "$EFI" == "true" ]] && echo "  firmware: EFI" >&2
            [[ -n "$TPM" ]] && echo "  tpm    : $TPM" >&2
            echo "  then   : boot the installer in the VirtualBox window" >&2
            ;;
    esac
    exit 0
fi

[[ "$ACTION" == "install" ]] || { echo "✖ nothing to do — pass --list, --info <id>, --plan <id> or --install <id>" >&2; exit 2; }

# ── install ──────────────────────────────────────────────────────────────────
need VBoxManage || { echo "✖ VirtualBox (VBoxManage) is not installed — run ./ensure-vbox.sh first" >&2; exit 1; }

# manual-only entries never auto-build: surface the pointer and stop.
if [[ "$KIND" == "manual" ]]; then
    echo "ⓘ '$NAME' can't be built automatically." >&2
    echo "  $NOTES" >&2
    echo "  follow: $PAGE" >&2
    exit 1
fi

# Refuse to clobber an already-registered VM of the same name.
if vbox_has "$NAME"; then
    echo "✓ '$NAME' is already built — boot it with /sbx vbox \"$NAME\" (nothing to do)" >&2
    exit 0
fi

# cloudimg: the existing, tested unattended path.
if [[ "$KIND" == "cloudimg" ]]; then
    echo "† building '$NAME' via vbox-new.sh (cloud-init, unattended)…" >&2
    args=(--name "$NAME" --release "${VERSION:-24.04}" --cpus "$CPUS" --mem "$MEM" --disk "$DISK")
    [[ $ASSUME_YES -eq 1 ]] && args+=(--yes)
    [[ $DO_BOOT -eq 0 ]] && args+=(--no-boot)
    exec "$VBOX_NEW" "${args[@]}"
fi

mkdir -p "$CACHE_DIR"

# Download helper → $DL_PATH. On failure, point at the page + --iso and bail
# (never leave a partial file or a half-built VM behind).
fetch() {
    local url="$1" dst="$2"
    [[ -f "$dst" ]] && { echo "↻ using cached $(basename "$dst")" >&2; return 0; }
    echo "↓ downloading $(basename "$dst") … (this is large; be patient)" >&2
    if need curl; then
        curl -fL --retry 3 -o "$dst.partial" "$url"
    elif need wget; then
        wget -O "$dst.partial" "$url"
    else
        echo "✖ neither curl nor wget available to download" >&2; return 1
    fi
}

# Resolve the source image: an explicit --iso wins; else download the pinned URL.
SRC=""
if [[ -n "$ISO_OVERRIDE" ]]; then
    [[ -f "$ISO_OVERRIDE" ]] || { echo "✖ --iso path not found: $ISO_OVERRIDE" >&2; exit 1; }
    SRC="$ISO_OVERRIDE"
elif [[ -n "$URL" ]]; then
    if fetch "$URL" "$DL_PATH"; then
        mv "$DL_PATH.partial" "$DL_PATH" 2>/dev/null || true
        SRC="$DL_PATH"
    else
        rm -f "$DL_PATH.partial"
        echo "✖ couldn't download '$NAME' from the pinned URL (it may have moved)." >&2
        echo "  get it from: $PAGE" >&2
        echo "  then build with: ./vbox-library.sh --install $ID --iso <downloaded-file>" >&2
        exit 1
    fi
else
    echo "✖ no direct download for '$NAME' — download it yourself, then:" >&2
    echo "  get it from: $PAGE" >&2
    echo "  ./vbox-library.sh --install $ID --iso <downloaded-file>" >&2
    exit 1
fi

# Confirmation (skipped with --yes).
if [[ $ASSUME_YES -ne 1 ]]; then
    printf 'Build VM "%s" now (%s, %s cpu / %sMiB / %sGiB)? [y/N] ' "$NAME" "$OS" "$CPUS" "$MEM" "$DISK" >&2
    read -r reply
    case "$reply" in y|Y|yes|YES) ;; *) echo "✖ aborted — nothing built" >&2; exit 1 ;; esac
fi

# ── ova: import the appliance and we're done ─────────────────────────────────
if [[ "$KIND" == "ova" ]]; then
    echo "† importing appliance '$NAME' …" >&2
    if VBoxManage import "$SRC" >/dev/null; then
        echo "✓ imported '$NAME' — boot it with /sbx vbox \"$NAME\"" >&2
        exit 0
    fi
    echo "✖ VBoxManage import failed" >&2; exit 1
fi

# ── iso: create a VM, attach the installer, boot it ──────────────────────────
echo "† creating VM '$NAME' (${OSTYPE:-Other_64}) …" >&2
VBoxManage createvm --name "$NAME" --ostype "${OSTYPE:-Other_64}" --register >/dev/null

# Roll the half-built VM back if any step below fails.
cleanup() { VBoxManage unregistervm "$NAME" --delete >/dev/null 2>&1 || true; }
trap 'echo "✖ build failed — rolling back VM" >&2; cleanup' ERR
set -e

MACHINE_FOLDER="$(VBoxManage list systemproperties | sed -n 's/^Default machine folder: *//p')"
VMDIR="$MACHINE_FOLDER/$NAME"
VDI="$VMDIR/$NAME.vdi"

VBoxManage modifyvm "$NAME" \
    --cpus "$CPUS" --memory "$MEM" \
    --nic1 nat --graphicscontroller vmsvga --vram 64 \
    --ioapic on --audio-driver none --boot1 dvd --boot2 disk >/dev/null
# Windows 11 (and other modern guests) require EFI firmware + a TPM 2.0 device.
[[ "$EFI" == "true" ]] && VBoxManage modifyvm "$NAME" --firmware efi >/dev/null
[[ -n "$TPM" ]] && VBoxManage modifyvm "$NAME" --tpm-type "$TPM" >/dev/null

# Boot disk.
VBoxManage createmedium disk --filename "$VDI" --size "$((DISK * 1024))" --format VDI >/dev/null
VBoxManage storagectl "$NAME" --name SATA --add sata --controller IntelAhci --portcount 2 >/dev/null
VBoxManage storageattach "$NAME" --storagectl SATA --port 0 --device 0 --type hdd --medium "$VDI" >/dev/null
# Installer ISO on an IDE optical drive.
VBoxManage storagectl "$NAME" --name IDE --add ide >/dev/null
VBoxManage storageattach "$NAME" --storagectl IDE --port 0 --device 0 --type dvddrive --medium "$SRC" >/dev/null

trap - ERR   # past the rollback point

echo "✓ VM '$NAME' created — the installer ISO is attached." >&2
echo "  finish setup inside the VirtualBox window (it boots into the installer)." >&2

if [[ $DO_BOOT -eq 1 ]]; then
    echo "† booting '$NAME' (GUI) …" >&2
    VBoxManage startvm "$NAME" --type gui >/dev/null
    echo "✓ launched $NAME" >&2
else
    echo "ⓘ not booting (--no-boot); start later with: /sbx vbox \"$NAME\"" >&2
fi
