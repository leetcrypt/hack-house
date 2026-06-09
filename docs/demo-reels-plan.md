# hack-house → Demo Reels Plan (Instagram)

> **Status:** Draft v1 · **Date:** 2026-06-07
> **Scope:** Production plan + composed scripts for three Instagram Reels:
> (1) Podman **Kali** GUI, (2) Docker **Parrot** GUI, (3) **Goose** agent in the
> sandbox. Each follows the house framework: **what's covered → the demo →
> possibilities unlocked**. Save/load image is folded into the two GUI reels.
> **Infra:** `~/coding/video-toolkit` (`tmux-demo.py`, `screen-rec.sh`,
> `social-reframe.sh`, `video-forge`, `cast2mp4.sh`, `tg-send.sh`).
> **Frameworks applied (vault):** PAS hook, 3-second gate, 15–30s sweet spot,
> burn captions (sound-off), seamless loop, completion-first.

---

## 0. Status & prerequisites

| Item | State |
|---|---|
| `/sbx docker` → Parrot OS loads | ✅ verified (`Parrot Security 7.2 (echo)`) |
| Container noVNC GUI stack | ✅ verified end-to-end (HTTP 200 on `127.0.0.1:6080/vnc.html`) |
| `/sbx podman` engine | ⏳ installing (`sudo apt-get install -y podman`); rootless subuid/subgid already present |
| Demo scenarios composed | ✅ this doc (capture JSON to be written next) |
| Live X display for GUI browser capture | ⚠️ **required** for reels 1 & 2 (the desktop is a browser window) |

**Two capture tracks:**
- **Terminal track (headless, deterministic):** `tmux-demo.py` drives the hh TUI,
  records the `/sbx …` command + save/load to an asciinema `.cast`. Works with no
  display. Covers all of reel 3 and the TUI half of reels 1 & 2.
- **Browser track (needs live X):** `screen-rec.sh` x11grabs the noVNC desktop in
  a browser (`http://127.0.0.1:6080`). This is the GUI *payoff* shot for reels 1 & 2.

The forge cut stitches: `title_card` (what's covered) → terminal clip → browser
clip → `result_card` (possibilities unlocked). Then `social-reframe.sh` → vertical
1080×1920, then `tg-send.sh` → Telegram (`andre`).

---

## 1. Reel 1 — Podman · Kali GUI + snapshot

**One-sentence promise:** Spin up a full **Kali desktop** in a shared, encrypted
room — rootless, no daemon, no sudo — and snapshot it like a save file.

**Length:** ~25s. **Backend:** `podman` (Kali default).

### Beat sheet
| t | Track | Beat (on-screen caption) |
|---|---|---|
| 0–3s | title | **HOOK (PAS):** "Your team shares one Kali box. No VMs. No root daemon." (pattern-interrupt: TUI glitch-in) |
| 3–6s | terminal | `/sbx podman gui` typed in the hh TUI → `summoning podman …` (caption: "one command") |
| 6–10s | terminal | status line: rootless, **no sudo modal**; noVNC URL surfaced (caption: "rootless · daemonless") |
| 10–17s | browser | noVNC desktop fades in — **Kali XFCE** in the browser; open a terminal, run `cat /etc/os-release` → Kali (caption: "a real Kali desktop, in your browser") |
| 17–22s | terminal | `/sbx save kali-lab` → `/sbx stop` → `/sbx load kali-lab` (caption: "snapshot → restore, like a save file") |
| 22–25s | result | **possibilities unlocked** (see card) |

### result_card — possibilities unlocked
```
headline: "what this unlocks"
stats:
  ["share",   "1 Kali"]
  ["root",    "none"]
  ["crypto",  "fernet"]
  ["restore", "1 cmd"]
  ["GUI",     "browser"]
```
Spoken/caption outro: "A shared Kali rig your whole crew drives — saved, restored,
end-to-end encrypted. † link in bio."

---

## 2. Reel 2 — Docker · Parrot GUI + snapshot

**One-sentence promise:** Boot a **Parrot OS security desktop** the whole room can
see and drive, then snapshot it to a portable image.

**Length:** ~25s. **Backend:** `docker` (Parrot default).

### Beat sheet
| t | Track | Beat (caption) |
|---|---|---|
| 0–3s | title | **HOOK:** "Parrot OS. Full desktop. In a chat room." (bold claim, frame-1 motion) |
| 3–7s | terminal | `/sbx docker gui` → `summoning docker …` → noVNC URL (caption: "one line") |
| 7–14s | browser | Parrot XFCE desktop in the browser; `/etc/os-release` → Parrot Security 7.2 (caption: "the real Parrot toolset") |
| 14–20s | terminal | `/sbx save parrot-lab --local` → writes portable `.tar`; `/sbx load parrot-lab` (caption: "save the whole rig to one file") |
| 20–25s | result | possibilities unlocked |

### result_card — possibilities unlocked
```
headline: "what this unlocks"
stats:
  ["distro",  "Parrot"]
  ["desktop", "noVNC"]
  ["bind mt", "none"]
  ["export",  ".tar"]
  ["bind",    "loopbk"]
```
Outro: "A portable Parrot desktop you can hand to a teammate as a single file —
no host mounts, loopback-only. †"

---

## 3. Reel 3 — Goose agent in the sandbox

**One-sentence promise:** Give an AI agent a task and watch it run **inside** the
sandbox — contained, granted, output streamed to chat.

**Length:** ~28s. **Backend:** any container (use `podman`/Kali). Terminal-only —
fully headless-capturable.

### Beat sheet
| t | Track | Beat (caption) |
|---|---|---|
| 0–3s | title | **HOOK (curiosity):** "What if your AI agent could only touch the sandbox — and nothing else?" |
| 3–7s | terminal | `/sbx podman` (container up) → `/ai start qwen2.5:3b` (caption: "spawn a local agent") |
| 7–11s | terminal | `/ai <q>` ungranted → it **advises only**, never touches the box (caption: "no grant = chat only") |
| 11–16s | terminal | owner `/grant <agent>` (caption: "grant the drive") |
| 16–24s | terminal | `/ai <name> !<task>` → Goose runs the task **in the container**, stdout streams to chat (caption: "Goose, contained in the sandbox") |
| 24–28s | result | possibilities unlocked |

### result_card — possibilities unlocked
```
headline: "what this unlocks"
stats:
  ["harness", "Goose"]
  ["reach",   "sandbox"]
  ["host fs", "none"]
  ["gate",    "/grant"]
  ["model",   "local"]
```
Outro: "An autonomous agent whose blast radius is one container — gated by a single
grant, running a local model. †"

---

## 4. Production pipeline (commands)

Per reel:

```bash
cd ~/coding/video-toolkit

# 1. Terminal track (headless) — drives the hh TUI, records the /sbx + save/load
bin/tmux-demo.py run scenarios/hh-<reel>.json --no-render   # iterate until asserts pass
bin/tmux-demo.py run scenarios/hh-<reel>.json               # → output/hh-<reel>.cast/.mp4
# sharp render override:
bin/cast2mp4.sh output/hh-<reel>.cast output/hh-<reel>-term.mp4 --font-size 28 --theme dracula

# 2. Browser track (live X display) — GUI reels only
#    open http://127.0.0.1:6080 in a browser, position it, then:
GEOM=1280x800 OFF=100,100 bin/screen-rec.sh output/hh-<reel>-gui.mp4 12

# 3. Forge cut: title (covered) → terminal → [browser] → result (unlocked)
cd tools/video-forge
/home/dell/anaconda3/bin/python3 forge.py validate ../../scenarios/hh-<reel>-cut.json
/home/dell/anaconda3/bin/python3 forge.py render  ../../scenarios/hh-<reel>-cut.json \
    -o ../../output/hh-<reel>-cut.mp4

# 4. Vertical reframe + ship
cd ~/coding/video-toolkit
bin/social-reframe.sh output/hh-<reel>-cut.mp4 --out output/hh-<reel>-reel.mp4 --size 1080x1920
bin/tg-send.sh output/hh-<reel>-reel.mp4 andre "hack-house — <reel> †"
```

### Gotchas (from prior demo work)
- Forge **`video` scenes: OMIT `duration`** (never `"auto"` — the renderer crashes); it plays the full clip. Use style `technical` (no 3s `social` clamp).
- `result_card` stat **values ≤ ~8 chars** or columns overflow (hence `loopbk`, `1 Kali`).
- Render the `.cast` with `--font-size 28` for sharpness; default 16 looks low-res.
- Trim the trailing `[exited]` black tail with `ffmpeg -t <N>` so it ends on a UI frame.
- Container teardown on `/sbx stop` + on client quit (Ctrl-Q) means each capture
  cold-pulls unless the image is already local — pre-pull `kalilinux/kali-rolling`
  and `parrotsec/core` before filming so the summon lands within the step waits.
- The GUI noVNC stack is a heavy first apt install (~150MB desktop). Pre-warm the
  container once (so the bootstrap sentinel is set) before the take, or budget a
  long step wait; otherwise the desktop isn't ready when the browser shot rolls.
