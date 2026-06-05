#!/usr/bin/env bash
# film-virtualbox.sh — RECORD the "VirtualBox, local GUI VM" beat and compose a
# narrated, subtitled demo. Sibling of film-save-load.sh.
#
#   connect alice  →  /sbx vms (detect VirtualBox + list VMs)
#   →  /sbx gui NE-Lab-Win11  (boots the real Windows VM locally, in its GUI)
#   →  capture the VM window coming up  →  title/result cards, TTS, subtitles
#
# Two captures: a clean terminal asciinema cast (the commands) and an x11grab of
# *only* the VirtualBox window region (not the whole desktop). Then video-forge
# stitches title→terminal→gui→result; edge-tts narration + an SRT are muxed/burned
# on top.
#
# Usage:  hh/film-virtualbox.sh [--keep] [--no-render] [--no-vm]
#   --keep        leave server/sessions up; don't power off the VM
#   --no-render   stop after the captures (skip compose)
#   --no-vm       skip booting the VM (reuse an already-running one for GUI grab)
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VTK="$HOME/coding/video-toolkit"
FORGE_PY="$HOME/anaconda3/bin/python3"
pick_port() { local p; for p in $(seq 4240 4290); do ss -ltn 2>/dev/null | grep -q ":$p " || { echo "$p"; return; }; done; echo 4173; }
PORT="${PORT:-$(pick_port)}"
PW="${PW:-malware-bless}"
VM="${VM:-NE-Lab-Win11}"
PY="$REPO/.venv/bin/python"
BIN="$REPO/hh/target/debug/hack-house"
# Two side-by-side client panes (alice | bob) in one recorded window, so the
# cast shows BOTH parties of the collaboration at once.
WIN_W=190; WIN_H=42
SRV_SESS="vbxfilm-srv"
RUN_SESS="vbxfilm"
REC_SESS="vbxfilm-rec"
OUTDIR="$REPO/docs/demo"
CAST="$OUTDIR/virtualbox.cast"
TERM_MP4="$OUTDIR/vbx-term.mp4"
GUI_MP4="$OUTDIR/vbx-gui.mp4"
CUT_MP4="$OUTDIR/vbx-cut.mp4"
NARR_MP3="$OUTDIR/vbx-narration.mp3"
SRT="$OUTDIR/vbx.srt"
FINAL_MP4="$OUTDIR/virtualbox.mp4"
MANIFEST="$OUTDIR/vbx-forge.json"
ACCENT="#39ff14"
GUI_SECS="${GUI_SECS:-18}"

KEEP=0; RENDER=1; BOOT_VM=1
for a in "$@"; do case "$a" in
  --keep) KEEP=1 ;; --no-render) RENDER=0 ;; --no-vm) BOOT_VM=0 ;;
esac; done

GREEN=$'\e[32m'; RED=$'\e[31m'; YEL=$'\e[33m'; DIM=$'\e[2m'; RST=$'\e[0m'
step() { printf '\n%s== %s ==%s\n' "$YEL" "$*" "$RST"; }
ok()   { printf '%s  ok %s%s\n' "$GREEN" "$*" "$RST"; }
bad()  { printf '%s  XX %s%s\n' "$RED" "$*" "$RST"; }
note() { printf '%s    %s%s\n' "$DIM" "$*" "$RST"; }
FAIL=0; fail() { bad "$*"; FAIL=1; }

cleanup() {
  if [[ $KEEP -eq 1 ]]; then note "--keep: leaving sessions + VM up."; return; fi
  step "cleanup"
  tmux kill-session -t "$REC_SESS" 2>/dev/null
  tmux kill-session -t "$RUN_SESS" 2>/dev/null
  tmux kill-session -t "$SRV_SESS" 2>/dev/null
  if [[ $BOOT_VM -eq 1 ]]; then
    VBoxManage controlvm "$VM" poweroff >/dev/null 2>&1 && note "powered off $VM"
  fi
}
trap cleanup EXIT

# Pane-targeted drivers. We resolve real pane IDs (APANE/BPANE) at split time
# rather than hardcoding .0/.1 — the user's tmux may set pane-base-index 1, which
# would shift the indices and silently drop sends to a nonexistent ".0".
APANE=""; BPANE=""
asay() { tmux send-keys -t "$APANE" -l "$*"; sleep 0.5; tmux send-keys -t "$APANE" Enter; sleep 0.8; }
bsay() { tmux send-keys -t "$BPANE" -l "$*"; sleep 0.5; tmux send-keys -t "$BPANE" Enter; sleep 0.8; }
acap() { tmux capture-pane -t "$APANE" -p 2>/dev/null; }
bcap() { tmux capture-pane -t "$BPANE" -p 2>/dev/null; }
await_for() { local re="$1" t="${2:-30}" i=0; while (( i < t*2 )); do acap | grep -qE "$re" && return 0; sleep 0.5; ((i++)); done; return 1; }
bwait_for() { local re="$1" t="${2:-30}" i=0; while (( i < t*2 )); do bcap | grep -qE "$re" && return 0; sleep 0.5; ((i++)); done; return 1; }

# ---- 0. preflight -----------------------------------------------------------
step "preflight"
command -v tmux >/dev/null || { echo "tmux required"; exit 2; }
command -v VBoxManage >/dev/null || { echo "VirtualBox required"; exit 2; }
command -v xdotool >/dev/null || { echo "xdotool required"; exit 2; }
ASCIINEMA="$( [[ -x "$HOME/anaconda3/bin/asciinema" ]] && echo "$HOME/anaconda3/bin/asciinema" || command -v asciinema )"
[[ -n "$ASCIINEMA" ]] || { echo "asciinema required"; exit 2; }
[[ -x "$PY" ]] || { echo "venv python missing: $PY"; exit 2; }
VBoxManage list vms | grep -q "\"$VM\"" || { echo "VM '$VM' not registered"; exit 2; }
[[ -x "$BIN" ]] || { step "building client"; ( cd "$REPO/hh" && cargo build ) || exit 2; }
mkdir -p "$OUTDIR"
ok "tools present; VM '$VM' registered"

tmux kill-session -t "$REC_SESS" 2>/dev/null; tmux kill-session -t "$RUN_SESS" 2>/dev/null
tmux kill-session -t "$SRV_SESS" 2>/dev/null
VBoxManage controlvm "$VM" poweroff >/dev/null 2>&1 || true
rm -f "$CAST"

# ---- 1. server (not recorded) ----------------------------------------------
step "boot server :$PORT"
tmux new-session -d -s "$SRV_SESS" -x 200 -y 50 \
  "cd '$REPO' && '$PY' cmd_chat.py serve 127.0.0.1 $PORT --password '$PW' --no-tls 2>&1 | tee /tmp/vbxfilm-srv.log"
sleep 3
ok "server up"

# ---- 2. two-party demo window + recorder -----------------------------------
step "open recorded window (${WIN_W}x${WIN_H}, two panes) and start asciinema"
tmux new-session -d -s "$RUN_SESS" -x "$WIN_W" -y "$WIN_H" "bash --noprofile --norc"
APANE="$(tmux list-panes -t "$RUN_SESS" -F '#{pane_id}' | head -1)"            # alice
BPANE="$(tmux split-window -h -t "$APANE" -P -F '#{pane_id}' "bash --noprofile --norc")"  # bob
tmux select-layout -t "$RUN_SESS" even-horizontal
sleep 0.5
for pane in "$APANE" "$BPANE"; do
  tmux send-keys -t "$pane" -l "cd '$REPO'"; tmux send-keys -t "$pane" Enter
  tmux send-keys -t "$pane" -l "clear"; tmux send-keys -t "$pane" Enter
done
sleep 0.5
tmux new-session -d -s "$REC_SESS" -x "$WIN_W" -y "$WIN_H" \
  "'$ASCIINEMA' rec --overwrite -c 'tmux attach -t $RUN_SESS' '$CAST'"
sleep 2
ok "recording → $CAST"

# ---- 3. both parties join (alice = owner-side, bob = guest/client) ----------
asay "echo '⛧ alice — host'"; bsay "echo '⛧ bob — guest'"
sleep 0.8
asay "$BIN connect 127.0.0.1 $PORT alice --password '$PW' --no-tls"
await_for 'alice|roster|hack-house|owner' 20 && ok "alice joined" || fail "alice never joined"
bsay "$BIN connect 127.0.0.1 $PORT bob --password '$PW' --no-tls"
bwait_for 'bob|roster|hack-house' 20 && ok "bob joined" || fail "bob never joined"
sleep 1.5

# ---- 4. detect + list VMs (alice asks the room what's on the metal) ---------
step "/sbx vms — detect VirtualBox, list VMs"
asay "/sbx vms"
await_for "VirtualBox .* detected|$VM" 15 && ok "detected + listed" || fail "no detect line"
sleep 2.5

# ---- 5. the GUEST launches the shared VM through the consent gate -----------
# /sbx gui is open to ANY member (not owner-gated) — the per-client consent gate
# IS the client's permission. Bob (guest) opens it: gate → `/sbx gui yes` → boot.
# A second gate would appear if a hypervisor held VT-x; we pre-freed it so only
# the open-locally consent shows. On success bob's client broadcasts to the room.
step "/sbx gui $VM — guest (bob) consents, then boots the real VM locally"
if [[ $BOOT_VM -eq 1 ]]; then
  bsay "/sbx gui $VM"
  bwait_for "open .*locally|continue|cancel" 15 && ok "consent gate shown to guest" || note "no consent prompt (already running?)"
  sleep 2.0
  bsay "/sbx gui yes"
  bwait_for "launched $VM|already running|freeing VT-x" 25 && ok "guest launch acked" || fail "no launch ack"
  # The room learns the shared appliance is live: alice should see the notice.
  await_for "bob opened .*$VM|open your own copy" 15 && ok "host saw the room notice" || note "host notice not seen"
  sleep 1.5
  # And the host can open her own copy with one command (same host → already up).
  step "/sbx gui $VM — host (alice) opens her own copy"
  asay "/sbx gui $VM"
  await_for "already running|open .*locally|launched $VM" 15 && ok "host can open too" || note "host open not acked"
  sleep 2.0
else
  note "--no-vm: skipping actual boot"
fi
sleep 2

# stop the terminal recording now (the rest is the GUI grab + compose)
step "stop terminal recording"
tmux kill-session -t "$RUN_SESS" 2>/dev/null
sleep 2
[[ -s "$CAST" ]] && ok "cast: $CAST ($(du -h "$CAST" | cut -f1))" || fail "cast not written"

# ---- 6. render the terminal cast → mp4 -------------------------------------
step "render terminal cast → mp4"
bash "$VTK/bin/cast2mp4.sh" "$CAST" "$TERM_MP4" --font-size 28 --theme dracula \
  && ok "term mp4: $TERM_MP4" || fail "term render failed"

# ---- 7. grab ONLY the VirtualBox window region -----------------------------
if [[ $BOOT_VM -eq 1 ]]; then
  step "capture the $VM window (${GUI_SECS}s)"
  WIN=""; for i in $(seq 1 20); do
    WIN="$(xdotool search --name "$VM" 2>/dev/null | tail -1)"
    [[ -n "$WIN" ]] && break; sleep 1
  done
  if [[ -n "$WIN" ]]; then
    xdotool windowactivate "$WIN" 2>/dev/null; sleep 1
    # Park the window at the top-left so a tall VM window can't run off the
    # bottom of the screen (x11grab refuses a capture area outside the display).
    xdotool windowmove "$WIN" 0 0 2>/dev/null; sleep 0.6
    eval "$(xdotool getwindowgeometry --shell "$WIN")"   # sets X Y WIDTH HEIGHT
    read -r SCRW SCRH < <(xdotool getdisplaygeometry)
    (( X < 0 )) && X=0; (( Y < 0 )) && Y=0
    MAXW=$(( SCRW - X )); MAXH=$(( SCRH - Y ))
    (( WIDTH  > MAXW )) && WIDTH=$MAXW
    (( HEIGHT > MAXH )) && HEIGHT=$MAXH
    EW=$(( WIDTH  - WIDTH  % 2 )); EH=$(( HEIGHT - HEIGHT % 2 ))
    note "window $WIN @ ${EW}x${EH}+${X},${Y} (screen ${SCRW}x${SCRH})"
    GEOM="${EW}x${EH}" OFF="${X},${Y}" bash "$VTK/bin/screen-rec.sh" "$GUI_MP4" "$GUI_SECS" \
      && ok "gui mp4: $GUI_MP4" || fail "gui capture failed"
  else
    fail "could not find the $VM window"
  fi
fi

[[ $RENDER -eq 0 ]] && { step "result (--no-render)"; exit $FAIL; }

# ---- 8. narration (edge-tts) + subtitles -----------------------------------
step "generate TTS narration + SRT"
"$FORGE_PY" - "$NARR_MP3" "$SRT" <<'PY'
import sys, asyncio, edge_tts
from edge_tts import SubMaker
out_mp3, out_srt = sys.argv[1], sys.argv[2]
VOICE = "en-US-GuyNeural"
script = (
    "Hack house now speaks VirtualBox. "
    "Ask the room what's on the metal: it detects your install and lists every VM. "
    "Any member can summon one — host or guest. Bob, the guest, opens it, and a consent gate asks first, because the window lands on his own machine. "
    "He confirms, and a real Windows eleven lab boots locally. "
    "The room sees it go live, so Alice can open her own copy with a single command. "
    "One shared appliance, each party running it locally. No pixels cross the wire — same zero knowledge trust, now with a full graphical VM."
)
async def main():
    sm = SubMaker()
    with open(out_mp3, "wb") as f:
        async for chunk in edge_tts.Communicate(script, VOICE, rate="-4%", boundary="WordBoundary").stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                sm.feed(chunk)
    open(out_srt, "w").write(sm.get_srt())   # edge-tts 7.x emits SRT directly
asyncio.run(main())
print("narration written")
PY
[[ -s "$NARR_MP3" ]] && ok "narration: $NARR_MP3 ($(du -h "$NARR_MP3" | cut -f1))" || fail "tts failed"
[[ -s "$SRT" ]] && ok "srt: $SRT" || note "no SRT produced"

# ---- 9. compose the visual cut (video-forge) -------------------------------
step "compose visual cut (forge)"
TERM_VID="$TERM_MP4"; GUI_VID="$GUI_MP4"
cat > "$MANIFEST" <<JSON
{
  "meta": {"resolution": [1920, 1080], "fps": 30, "style": "technical", "crossfade": 0.4},
  "scenes": [
    {"type": "title_card", "duration": 4.0,
     "headline": "VIRTUALBOX", "subhead": "share a VM — run it locally",
     "accent": "$ACCENT", "transition_out": "fade"},
    {"type": "video", "source": "$TERM_VID",
     "transition_in": "fade", "transition_out": "fade"},
    {"type": "video", "source": "$GUI_VID",
     "transition_in": "fade", "transition_out": "fade"},
    {"type": "result_card", "duration": 6.5,
     "headline": "WHAT HAPPENED", "subhead": "both parties · client consent · zero-knowledge",
     "accent": "$ACCENT",
     "items": [
       "detected the local VirtualBox install + listed VMs",
       "any member can launch — not owner-gated",
       "the guest consented (a gate, on his own machine)",
       "a real Windows 11 VM booted locally in its GUI",
       "the room was notified — the host can open hers too",
       "one shared appliance, each party runs it locally"
     ],
     "transition_in": "fade"}
  ]
}
JSON
( cd "$VTK/tools/video-forge" && "$FORGE_PY" forge.py render "$MANIFEST" --output "$CUT_MP4" ) \
  && ok "cut: $CUT_MP4" || fail "forge render failed"

# ---- 10. mux narration + burn subtitles → final ----------------------------
step "mux narration + burn subtitles → final"
if [[ -s "$CUT_MP4" && -s "$NARR_MP3" ]]; then
  TMP_AV="$OUTDIR/.vbx-av.mp4"
  # add the voiceover (the cut may be silent or have music; keep it low if present)
  if ffprobe -v error -select_streams a -show_entries stream=index -of csv=p=0 "$CUT_MP4" | grep -q .; then
    ffmpeg -y -loglevel error -i "$CUT_MP4" -i "$NARR_MP3" \
      -filter_complex "[0:a]volume=0.18[bg];[bg][1:a]amix=inputs=2:duration=first:dropout_transition=0[a]" \
      -map 0:v -map "[a]" -c:v copy -c:a aac -movflags +faststart "$TMP_AV"
  else
    ffmpeg -y -loglevel error -i "$CUT_MP4" -i "$NARR_MP3" \
      -map 0:v -map 1:a -c:v copy -c:a aac -shortest -movflags +faststart "$TMP_AV"
  fi
  if [[ -s "$SRT" ]]; then
    bash "$VTK/bin/captions.sh" "$TMP_AV" "$FINAL_MP4" --srt "$SRT" --accent "$ACCENT" \
      && ok "final (subtitled): $FINAL_MP4" || { cp "$TMP_AV" "$FINAL_MP4"; note "caption burn failed; final without subs"; }
  else
    cp "$TMP_AV" "$FINAL_MP4"; note "no SRT; final without subs"
  fi
  rm -f "$TMP_AV"
else
  fail "missing cut or narration; cannot finalize"
fi

# ---- summary ----------------------------------------------------------------
step "result"
if [[ $FAIL -eq 0 && -s "$FINAL_MP4" ]]; then
  printf '%sFILM OK%s — %s (%s)\n' "$GREEN" "$RST" "$FINAL_MP4" "$(du -h "$FINAL_MP4" | cut -f1)"
else
  printf '%sFILM FAIL%s — inspect %s\n' "$RED" "$RST" "$OUTDIR"
fi
exit $FAIL
