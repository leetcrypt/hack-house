# hack-house on the phone — mobile quickstart

Run and drive a hack-house room from the **Fairphone-6** (Termux, Android). This
is the *operator's* how-to; the design rationale lives in
[`termux-operator.md`](termux-operator.md).

Two devices are in play:

- **the phone** — Termux, `aarch64`, Python 3.13, reached over Tailscale as
  `<phone-tailnet-ip>` (sshd on **:8022**, user `<user>`). Hosts/joins rooms.
- **the laptop / dev-box** — pulls captures off the phone, joins phone-hosted
  rooms, and does any chromium/Whisper-heavy work the phone can't.

> **Reachability first.** Android kills backgrounded Termux, taking `sshd` with
> it — and `tailscale status` still shows the phone `active`. Always probe the
> **port**, not the tailnet:
> ```bash
> timeout 8 bash -c 'cat </dev/null >/dev/tcp/<phone-tailnet-ip>/8022' \
>   && echo REACHABLE || echo OFFLINE
> ```
> If OFFLINE the fix is on-device: wake the phone, open Termux, run
> `termux-wake-lock` and start `sshd`. Don't hammer a dead port.

---

## 1. One-time on-device setup

**Step 1 — sync the tree** onto the phone (tar-over-ssh from the laptop; no gitea
clone needed if the branch is unpushed):

```bash
# from the laptop, in this repo
tar czf - cmd_chat scripts requirements-operator.txt \
  | ssh -i ~/.ssh/<phone-key> -p 8022 <user>@<phone-tailnet-ip> 'mkdir -p ~/hh-mobile && tar xzf - -C ~/hh-mobile'
```

**Step 2 — run the bootstrap** in Termux on the phone. It installs deps
(`python-cryptography` to dodge a rust build; pure/wheeled pip deps; **no** `srp`
C-ext — the shim covers it), holds a wake-lock, installs the `hh` alias, and runs
the Phase-0 preflight. Idempotent — safe to re-run.

```bash
# in Termux, on the phone
bash ~/hh-mobile/scripts/termux-bootstrap.sh
source ~/.bashrc            # activate the 'hh' alias
```

Expect the preflight to end `RESULT: PASS`. (Manual equivalent, if you'd rather:
`pkg install python tmux git python-cryptography openssh` · `pip install requests
rich websockets` · `termux-wake-lock` · add `alias hh='bash
"$HOME/hh-mobile/scripts/hh"'` to `~/.bashrc`.)

---

## 2. The `hh` launcher

Everything runs in a persistent tmux session named `hh`, so it survives an SSH
drop. `HH_PY` defaults to `python`; override any config via env (see the header
of `scripts/hh`).

```bash
hh host  [NAME] [PORT]      # host a room ON THE PHONE           (default :8799)
hh join  HOST PORT [NAME]   # join an existing room as operator
hh web   [PORT]             # serve the mobile web console       (default :8790)
hh op    <args...>          # passthrough to `python -m cmd_chat.operator`
hh status                   # what's running + listening (curl health per room)
hh stop  [all|host [PORT]|web [PORT]|join]
```

Room windows are **port-scoped** (`host-8799`, `web-8790`), so several rooms
coexist — one per directory when paired with direnv below.

```bash
hh host myroom 8799         # → hosting on 0.0.0.0:8799 (password: hh-mobile)
hh status                   # host :8799 UP (health json) · operator daemons
hh stop host 8799           # tear down just that room
```

Config env (all optional): `HH_PASSWORD` (hh-mobile), `HH_PORT` (8799),
`HH_WEB_PORT` (8790), `HH_NAME` (dir basename), `HH_BIND` (0.0.0.0), `HH_TLS`
(1 → keep TLS; default drops it with `--no-tls`).

## 3. Auto-host a room per directory (direnv)

Drop an `.envrc` in any project dir so `cd`-ing in auto-hosts a room named after
the directory:

```bash
# .envrc
export HH_PORT=8801          # optional; override before sourcing
source "$HOME/hh-mobile/scripts/hh-direnv.sh"
```

`direnv allow`, then every `cd` in idempotently ensures the room is up (the
launcher no-ops if the window already exists). Disable hosting but keep the env
with `export HH_AUTOHOST=0`.

## 4. Mobile web console

`hh web` serves a stdlib web UI in front of the operator daemon — Terminal + Chat
tabs, a quick-keys row (Enter/^C/Esc/Tab/↑/↓/clear/^D), and a command box.

- On the phone browser: `http://127.0.0.1:8790`
- From the laptop: `http://<phone-tailnet-ip>:8790`

It's **read-only** until a room owner runs `/grant <name>`; until then keys/exec
are inert by design.

---

## 5. Capture & share from the phone (`scripts/phone-capture.sh`)

Runs on the **laptop**; it SSHes into the phone and pulls content back. Add
`--tg [target]` to any command to also ship the artifact to Telegram.

```bash
scripts/phone-capture.sh file  <remote> [local]    # pull a file/dir (tar over ssh)
scripts/phone-capture.sh term  [tmux-session]      # capture tmux pane text (default: hh)
scripts/phone-capture.sh webshot <url> [png]       # screenshot a URL via headless chromium
scripts/phone-capture.sh shot   [png]              # device screenshot  (needs ADB)
scripts/phone-capture.sh screen [secs] [mp4]       # device screen record (needs ADB)
```

**Non-root routes** (`file`, `term`, `webshot`) work straight from the Termux app
uid. Example — grab the live web console as a PNG:

```bash
scripts/phone-capture.sh webshot 'http://<phone-tailnet-ip>:8790/' --tg
```

**Screen capture** (`shot`/`screen`) can't reach SurfaceFlinger from an app uid,
so it needs ADB over **Wireless Debugging** (Settings → System → Developer
options). If ADB isn't connected the script prints the one-time pairing steps.

Env overrides: `PHONE_IP`, `PHONE_PORT`, `PHONE_USER`, `PHONE_KEY`; captures land
in `~/phone-captures/`.

---

## 6. Which Claude skills to ship to the phone

Lean, self-contained skills that earn their place on a phone (bash/curl/ssh, no
chromium, no build step):

**Ship now:** `hh-operator` · `tg-send`/`tg-direct` (ship captures out) ·
`transcribe` (offloads to laptop Whisper over SSH — ideal for a phone) ·
`dns-toolkit` · `news-intel` · `session-audit` · `project-map` · `site-audit`.

**Tether, don't relocate:** `screenshot` (already covered by
`phone-capture.sh webshot`), `web-video-grab` (yt-dlp mode only — the Playwright
path needs chromium), `youtube-research`/`vault-*`/`imagegen` (laptop-heavy).

**Skip:** anything GPU/vision, local-infra (gitea-admin, Nuclei/Suricata
digests), creds-on-laptop (IMAP/gopass email), or OAO business skills.

Leanest starter kit: `hh-operator + tg-send + transcribe + dns-toolkit +
session-audit + news-intel`.
