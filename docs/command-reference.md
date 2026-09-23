# hack-house → Command Reference

> **Status:** Living reference · **Date:** 2026-06-07
> **Scope:** The full categorized list of in-room slash commands + keybindings,
> mirroring the `/help` popup (`hh/src/ui.rs::help_clusters`). This is the
> canonical text reference for the `/sbx <type> <option>` grammar, the container
> `gui` option, and the "did you mean" fuzzy-suggestion behaviour.
> **See also:** `spec-sandbox-distros-gui.md` (grammar + GUI spec),
> `spec-goose-harness.md`, `spec-virtualbox-sandbox.md`.

---

## 0. Grammar at a glance

```
/sbx <type> <option…>
```

The first token after `/sbx` selects the **backend** (`docker`, `podman`,
`multipass`, `local`, `vbox`); the rest are **options**. `vbox` is the exception
that takes extra options (a VM name / `new` / `gui`). The legacy
`/sbx launch <backend> …` form is still accepted as a transitional alias.

Keyword options (`gui`, `install`) are position-independent — they are filtered
out of the positionals, so they are never mistaken for the image name.

---

## 1. Virtual machines / sandboxes

| Command | What to expect |
|---|---|
| `/sbx docker [gui] [image] [install]` | Linux container, shared shell relayed to the room. Default image **`parrotsec/core`** (Parrot OS) + auto dev toolchain. `gui` → noVNC desktop (see §4). `install` → install Docker first if missing. |
| `/sbx podman [gui] [image] [install]` | Rootless/daemonless container — **no sudo modal**. Default image **`kalilinux/kali-rolling`** (Kali). `gui` → noVNC desktop. `install` → install Podman first if missing. |
| `/sbx multipass [image] [install]` | Full Ubuntu VM (default `24.04`), shared shell relayed to the room. `install` → install Multipass first if missing. |
| `/sbx vbox` | VirtualBox VM picker — local GUI (↑↓ move · Enter/Tab boot · Esc dismiss). |
| `/sbx vbox new [name]` | Build a FRESH Ubuntu VM from a cloud image (cloud-init installs the dev toolchain). |
| `/sbx vbox [gui] <vm> [yes]` | Boot a VirtualBox VM's GUI on YOUR machine (non-host appends `yes` to install/import first). |
| `/sbx gui <vm> [yes]` | Alias of `/sbx vbox gui <vm> [yes]`. |
| `/sbx local` | A plain shell on your own machine — no VM. |
| `/sbx stop` | Tear down the running sandbox (purges the VM/container). |

### Save / load / list

| Command | What to expect |
|---|---|
| `/sbx save [label] [--local]` | Save docker/podman/multipass state. `--local` on a container also writes a portable `.tar`. |
| `/sbx load <label>` | Relaunch a saved docker/podman/multipass snapshot (auto-detects backend). |
| `/sbx snaps` | List saved docker/podman/multipass snapshots. |
| `/sbx vmsave <vm> [label] [--local]` | Save a VirtualBox VM snapshot. `--local` also exports a portable `.ova` to `/send`. |
| `/sbx vmload <vm> [label]` | Restore a VirtualBox snapshot + boot (omit label for the VM's current snapshot). |
| `/sbx vms` · `/sbx vmsnaps <vm>` | List local VirtualBox VMs · a VM's snapshots. |
| `/drive` · `F2` | Type into the shared shell (`F2` releases; `Esc` reaches vim). |

### Defaults

| Backend | Default image | Notes |
|---|---|---|
| Docker | `parrotsec/core` | swap `parrotsec/security` per-launch for full tools |
| Podman | `kalilinux/kali-rolling` | minimal; add `kali-linux-headless` for the toolset |
| Multipass | `24.04` | Ubuntu cloud release |
| Local | *(host shell)* | — |

Any image is overridable positionally: `/sbx podman parrotsec/security`,
`/sbx docker ubuntu:24.04`, etc.

---

## 2. AI agents

| Command | What to expect |
|---|---|
| `/ai start [model\|profile]` | Spawn an agent (ollama tag or `models.toml` profile). |
| `/ai start <model> allow` | Spawn + auto-grant the agent sandbox drive. |
| `/ai stop` | Dismiss the agent you started. |
| `/ai <question>` | Ask an agent in the room (`/ai <name> <q>` if many). |
| `/ai <name> !<task>` | Have a **granted** agent run a task in the sandbox (Goose harness). |
| `/ai list` | List AI agents present + their provider/model. |
| `/ai models` | Show models the active agent's backend can serve. |

---

## 3. Permissions (owner)

| Command | What to expect |
|---|---|
| `/grant <user\|agent>` | Let a member OR an AI agent drive the shell. |
| `/revoke <user\|agent>` | Take back sandbox drive permission. |
| `/sudo <user>` | Delegate VM superuser (real sudo). |
| `/unsudo <user>` | Revoke VM superuser. |
| `/ai start <name> allow` | Shortcut: grant the agent drive at spawn. |

---

## 4. Container GUI (noVNC)

Containers are headless, so `gui` runs an XFCE desktop + VNC server **inside**
the container and bridges it to your browser via noVNC.

- Invoke: `/sbx podman gui` or `/sbx docker gui`.
- Backend-specific: `gui` is honoured only for Docker/Podman; on headless/local
  backends it is a flagged no-op.
- The noVNC port (`6080`) is published on **host loopback only**
  (`127.0.0.1:6080`) — reachable from this machine's browser, never the LAN.
- Open `http://127.0.0.1:6080`. Default VNC password `hackhouse` (override with
  `HH_SBX_GUI_PASS`).
- One GUI sandbox at a time per host (fixed port). Heavy first pull (a full
  desktop) — strictly opt-in.

---

## 5. Files

| Command | What to expect |
|---|---|
| `/send <user> <path>` | Send a file/dir directly to one member. |
| `/sendroom <path>` | Offer a file/dir to the whole room. |
| `/accept` · `/reject` | Respond to an incoming file offer. |

---

## 6. Appearance

| Command | What to expect |
|---|---|
| `/theme [name]` | Switch vestment, or bare `/theme` lists options. |
| `/theme save [name]` | Keep the vestment you're wearing for reuse. |
| `Ctrl+Alt+P` · `/theme random` | Conjure a random vestment (palette + sigil). |

---

## 7. Layout (resize panes)

| Key / Command | What to expect |
|---|---|
| `F4` | Fullscreen the terminal (cycle: terminal → chat → split). |
| click a pane · `F5` | Select a pane to resize (✎ marks it) — `F5` cycles chat → terminal → roster → input. |
| `↑` / `↓` | Grow / shrink height. |
| `←` / `→` | Grow / shrink the selected pane's width. |
| `Esc` / `Enter` | Finish editing the selected pane. |
| `/layout reset` | Restore the default split. |
| `/layout save <name>` · `load <name>` | Remember an arrangement and re-apply it. |
| `/layout list` · `rm <name>` | List or delete saved layouts. |

---

## 8. Keys

| Key | What to expect |
|---|---|
| `Enter` | Send chat message. |
| `F1` · `/help` | Toggle the categorized help popup. |
| `Ctrl-C` (while driving) | Interrupt the running command. |
| `Ctrl-X` (owner, not driving) | Kill switch — revoke all drive + interrupt the shell. |
| `PgUp` / `PgDn` | Scroll chat · `Home`/`End` = oldest/live (scrolls sandbox scrollback while driving). |
| `Up` / `Down` · wheel | Scroll the sandbox terminal (mouse works while driving). |
| `Ctrl-R` (when closed) | Reconnect to the house after a drop / AFK. |
| `/pw` | Show this room's password (local only). |
| `/clear` | Wipe your chat scrollback (local only). |
| `Ctrl-C` · `Ctrl-Q` | Quit hack-house. |

---

## 9. Roster glyphs (badges stack)

| Glyph | Meaning |
|---|---|
| *(theme sigil)* host | Opened the house (first in the room). |
| ⚡ sudoer | VM superuser in the sandbox. |
| ◆ driver | May drive the shell. |
| • member | Present — no extra powers. |

---

## 10. "Did you mean…?" (typo help for new users)

Unrecognised slash input is caught instead of being silently sent as chat:

- A mistyped top-level command (e.g. `/halp`) → `unknown command ‘/halp’ — did
  you mean \`/help\`?  (/help lists all)`.
- A mistyped `/sbx` subcommand (e.g. `/sbx dcoker`) → `unknown \`/sbx dcoker\` —
  did you mean \`/sbx docker\`?`.
- No close match → falls back to `… /help lists all commands`.

Matching uses Levenshtein edit distance (≤ 3 and shorter than the input), so
genuine free-text like `/ai <question>` (a known command) is never hijacked.
