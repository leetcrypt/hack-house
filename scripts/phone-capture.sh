#!/usr/bin/env bash
# phone-capture.sh — capture content FROM the Fairphone and pull it to the
# laptop. Runs on the LAPTOP/dev-box (it SSHes into the phone). Optionally
# ships the result to Telegram via the video-toolkit tg-send.sh.
#
#   phone-capture.sh file  <remote> [local]     pull a file/dir (tar over ssh)
#   phone-capture.sh term  [tmux-session]       capture tmux pane text (default: hh)
#   phone-capture.sh shot  [local.png]          device screenshot via ADB *
#   phone-capture.sh screen [secs] [local.mp4]  device screen record via ADB *
#   phone-capture.sh webshot <url> [local.png]  screenshot a URL via headless chromium
#
#   * ADB routes need Wireless Debugging on the phone (Termux can't reach
#     SurfaceFlinger — app uid, non-root). If not connected, this prints the
#     one-time pairing steps instead of failing.
#
# Add --tg [target] to any command to also send the artifact to Telegram
# (default target: andre). Env: PHONE_IP, PHONE_PORT, PHONE_USER, PHONE_KEY.
set -euo pipefail

IP="${PHONE_IP:-100.95.202.68}"; SSHPORT="${PHONE_PORT:-8022}"
USER_="${PHONE_USER:-u0_a203}"; KEY="${PHONE_KEY:-$HOME/.ssh/phone-deploy}"
SSH="ssh -i $KEY -p $SSHPORT -o ConnectTimeout=8 -o BatchMode=yes -o StrictHostKeyChecking=accept-new $USER_@$IP"
TG_SEND="$HOME/coding/video-toolkit/bin/tg-send.sh"
OUTDIR="${HH_CAPTURE_DIR:-$HOME/coding/_data/phone-captures}"
mkdir -p "$OUTDIR"
TS="$(date +%Y%m%d-%H%M%S)"

_die(){ echo "phone-capture: $*" >&2; exit 1; }
# strip a trailing "--tg [target]" off the args, remember it
TG=0; TGT="andre"
_args=(); while [ $# -gt 0 ]; do
  case "$1" in
    --tg) TG=1; shift; case "${1:-}" in ""|-*) : ;; *) TGT="$1"; shift;; esac ;;
    *) _args+=("$1"); shift ;;
  esac
done
set -- "${_args[@]:-}"

_reachable(){ timeout 8 bash -c "cat </dev/null >/dev/tcp/$IP/$SSHPORT" 2>/dev/null; }
_ship(){  # <file>
  [ "$TG" = "1" ] || { echo "saved: $1"; return 0; }
  [ -x "$TG_SEND" ] || _die "tg-send.sh not found at $TG_SEND"
  echo "→ Telegram ($TGT): $1"; "$TG_SEND" "$1" "$TGT" "phone capture $TS"
}
# first fully-authorized adb device serial (wireless debugging uses a dynamic
# port, so don't assume :5555 — read whatever 'adb devices' actually shows)
_adb_serial(){ adb devices 2>/dev/null | awk '$2=="device"{print $1; exit}'; }
_adb_ready(){ [ -n "$(_adb_serial)" ]; }
_adb_help(){
  cat >&2 <<EOF
ADB not connected. One-time setup on the phone (ports are shown on-device;
the IP can be the Tailscale IP $IP since adbd binds all interfaces):
  1. Settings → System → Developer options → Wireless debugging: ON
  2. Tap "Pair device with pairing code" — note the PAIR_PORT and 6-digit code
  3. On the laptop:  adb pair $IP:<PAIR_PORT>     (enter the code)
  4. Then:           adb connect $IP:<DEBUG_PORT>   (port under "Wireless debugging")
Re-run this command once 'adb devices' shows a 'device' line.
EOF
}

cmd="${1:-}"; shift || true
case "$cmd" in
  file)
    [ $# -ge 1 ] || _die "usage: file <remote> [local]"
    rem="$1"; loc="${2:-$OUTDIR/$(basename "$rem")}"
    _reachable || _die "phone offline (TCP $IP:$SSHPORT). Wake it, ensure sshd + termux-wake-lock."
    # $rem substituted UNQUOTED so the phone's shell expands a leading ~ (avoid spaces in remote paths)
    $SSH "cd \"\$(dirname $rem)\" && tar czf - \"\$(basename $rem)\"" | tar xzf - -C "$(dirname "$loc")"
    echo "pulled $rem → $loc"; _ship "$loc"
    ;;
  term)
    ses="${1:-hh}"; loc="$OUTDIR/phone-term-$ses-$TS.txt"
    _reachable || _die "phone offline (TCP $IP:$SSHPORT)."
    $SSH "tmux capture-pane -t '$ses' -p -S -2000 2>/dev/null" > "$loc" || _die "no tmux session '$ses' on phone"
    echo "captured tmux '$ses' → $loc ($(wc -l < "$loc") lines)"; _ship "$loc"
    ;;
  shot)
    loc="${1:-$OUTDIR/phone-shot-$TS.png}"
    _adb_ready || { _adb_help; exit 1; }
    dev="$(_adb_serial)"
    adb -s "$dev" exec-out screencap -p > "$loc"
    [ -s "$loc" ] || _die "screencap produced no data"
    echo "device screenshot → $loc"; _ship "$loc"
    ;;
  screen)
    secs="${1:-15}"; loc="${2:-$OUTDIR/phone-screen-$TS.mp4}"
    _adb_ready || { _adb_help; exit 1; }
    dev="$(_adb_serial)"
    echo "recording ${secs}s of the device screen…"
    adb -s "$dev" shell screenrecord --time-limit "$secs" /sdcard/hh-rec.mp4
    adb -s "$dev" pull /sdcard/hh-rec.mp4 "$loc" >/dev/null && adb -s "$dev" shell rm -f /sdcard/hh-rec.mp4
    echo "device screen recording → $loc"; _ship "$loc"
    ;;
  webshot)
    [ $# -ge 1 ] || _die "usage: webshot <url> [local.png]"
    url="$1"; loc="${2:-$OUTDIR/phone-webshot-$TS.png}"
    chrome="$(command -v chromium || command -v chromium-browser || command -v google-chrome || true)"
    [ -n "$chrome" ] || _die "no chromium/chrome found (or use the 'screenshot' Claude skill)"
    "$chrome" --headless --disable-gpu --hide-scrollbars --window-size=430,932 \
      --screenshot="$loc" "$url" >/dev/null 2>&1
    [ -s "$loc" ] || _die "chromium produced no screenshot"
    echo "web console screenshot ($url) → $loc"; _ship "$loc"
    ;;
  -h|--help|help|"") awk 'NR>1 && /^#/{sub(/^# ?/,"");print;next} NR>1{exit}' "${BASH_SOURCE[0]}" ;;
  *) _die "unknown command '$cmd' (try: help)" ;;
esac
