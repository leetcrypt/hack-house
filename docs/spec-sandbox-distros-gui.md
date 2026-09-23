# hack-house → Sandbox distro defaults, `/sbx` grammar, container GUI — Spec

> **Status:** Implemented v1 · **Date:** 2026-06-07
> **Scope:** (1) per-backend default distro images, (2) a uniform
> `/sbx <type> <option>` command grammar, (3) an opt-in noVNC desktop GUI for
> container sandboxes.
> **Baseline:** `hh/src/sbx.rs`, `hh/src/app.rs`, `hh/scripts/sandbox-bootstrap.sh`.
> **Builds on:** `spec-collaborative-sandbox.md`, `spec-goose-harness.md`.

---

## 0. Decisions locked (from product owner)

| # | Decision | Choice |
|---|----------|--------|
| A | Podman default distro | **Kali** (`kalilinux/kali-rolling`). Debian/apt-based ⇒ the apt-only `sandbox-bootstrap.sh` works unchanged. Minimal base (no pentest metapackages by default — fast first launch). |
| B | Docker default distro | **Parrot OS** (`parrotsec/core`). Also Debian/apt-based. `core` = minimal base; `parrotsec/security` per-launch for the full toolset. |
| C | Multipass / Local | Unchanged: Multipass = Ubuntu `24.04`; Local = host shell. |
| D | Grammar | **`/sbx <type> <option>`** across the board for clarity. vbox is the exception that takes extra options (`/sbx vbox gui <vm>`, `/sbx vbox new [name]`). `launch` kept as a transitional alias. |
| E | GUI | **Opt-in, container-only.** `/sbx <docker\|podman> gui` provisions an XFCE desktop served over noVNC, published on host **loopback only**. Headless backends/local: `gui` is a flagged no-op. |

**Why apt-only distros for the defaults:** `sandbox-bootstrap.sh` and
`sandbox-tools.json` are apt-based. Kali and Parrot are both Debian derivatives,
so they are drop-in. Non-apt distros (Arch/Fedora/Alpine) would each need a
package-manager branch in the bootstrap and are therefore *selectable images*,
not defaults.

---

## 1. Distro defaults

`Backend::default_image()` (`hh/src/sbx.rs`):

| Backend | Default image | Notes |
|---|---|---|
| `Docker` | `parrotsec/core` | swap `parrotsec/security` per-launch for full tools |
| `Podman` | `kalilinux/kali-rolling` | minimal; add `kali-linux-headless` for the toolset |
| `Multipass` | `24.04` | Ubuntu cloud release |
| `Local` | `""` | host shell |

Any image is overridable positionally: `/sbx podman parrotsec/security`,
`/sbx docker ubuntu:24.04`, etc.

---

## 2. `/sbx <type> <option>` grammar

The leading token after `/sbx` selects the backend; the rest are options.

```
/sbx docker [gui] [image] [install] [--start]
/sbx podman [gui] [image] [install]
/sbx multipass [image] [install]
/sbx local
/sbx vbox [gui] <vm> [yes]     # exception: extra option (VM name / confirm)
/sbx vbox new [name]           # exception: build a fresh VM
/sbx launch <backend> …        # transitional alias (still accepted)
```

Action subcommands are unchanged and remain non-backend-led:
`/sbx stop | save | load | snaps | vms | vmsave | vmload | vmsnaps`.

**Implementation:** the `/sbx` match arm accepts the backend tokens
(`docker|podman|multipass|local|vbox|virtualbox`) *and* `launch`. For the
backend-led form the matched token is pushed to the front of the positional args
so the existing `first = pos.next()` backend-selection logic is reused verbatim.
`gui` and `install` are keyword options filtered out of the positionals (so they
are never mistaken for the image), exactly like the pre-existing `install`.

---

## 3. Container GUI (noVNC)

### 3.1 Why a special path
Containers are **headless** — unlike VirtualBox (`spec-virtualbox-sandbox.md`),
there is no display to open. A GUI therefore means running a desktop + a remote
framebuffer *inside* the container and exposing it to the owner's browser.

### 3.2 Flow
1. `/sbx podman gui` → `want_gui` keyword detected → `gui = true` (only for
   Docker/Podman; flagged no-op otherwise).
2. `prepare()` publishes the noVNC port on host loopback:
   `-p 127.0.0.1:6080:6080` (`GUI_PORT = 6080`). **Never `0.0.0.0`** — reachable
   from this machine's browser, never the LAN.
3. `dk_bootstrap()` passes `HH_SBX_GUI=1` into the in-container bootstrap.
4. `sandbox-bootstrap.sh` (when `HH_SBX_GUI=1`) apt-installs a lightweight stack
   — `xfce4`, `xfce4-terminal`, `dbus-x11`, `tigervnc-standalone-server`,
   `novnc`, `websockify` — sets a non-interactive VNC password, starts a VNC
   server on `:1` (5901, `-localhost no` so the in-netns bridge can reach it),
   and daemonizes `websockify --web=/usr/share/novnc 6080 localhost:5901`.
5. The owner opens `http://127.0.0.1:6080` (noVNC). Default password
   `hackhouse` (override `HH_SBX_GUI_PASS`).

### 3.3 Security posture
- **Loopback-only publish.** The desktop is never bound to a routable address.
- The VNC password is obfuscation, not strong auth; the real gates are the
  loopback bind + the room password + the container's network namespace.
- No host bind mounts — consistent with the Goose containment invariant
  (`spec-goose-harness.md` §2.1). The GUI adds **one** published loopback port,
  nothing else crosses the boundary.

### 3.4 Limitations / non-goals
- **Heavy first pull** (a full desktop) — strictly opt-in, never default.
- The desktop+VNC processes are started during provisioning. The bootstrap is
  sentinel-guarded, so after a *container restart* the GUI services are not
  auto-restarted (fresh launches are the supported demo path). A future phase
  could move service-start out from under the sentinel.
- Fixed port 6080 ⇒ one GUI sandbox at a time on a host (matches the
  one-sandbox-at-a-time model).

---

## 4. Touch points
| File | Change |
|---|---|
| `hh/src/sbx.rs` | `default_image()` → Parrot (docker) / Kali (podman); `GUI_PORT` const; `prepare(gui)` publishes loopback `-p`; `provision(gui)`/`dk_bootstrap(gui)` thread `HH_SBX_GUI`. |
| `hh/src/app.rs` | `/sbx` match arm accepts backend-led tokens; `gui`/`install` keyword filtering; `gui` guarded to docker/podman; threaded through `PendingSudoLaunch` + `spawn_launch`; noVNC URL surfaced; help/usage strings → new grammar; "did you mean" suggestions (`KNOWN_COMMANDS`, `SBX_SUBCOMMANDS`, `levenshtein`/`closest`). |
| `hh/src/ui.rs` | `help_clusters()` VIRTUAL MACHINES → new grammar + `podman`/`gui` entries. |
| `hh/scripts/sandbox-bootstrap.sh` | `HH_SBX_GUI=1` section: apt desktop+VNC+noVNC, start server + websockify bridge. |
| `docs/command-reference.md` | Canonical categorized command list (mirrors `/help`), grammar + GUI + "did you mean" reference. |

---

## 5. Command reference

The full categorized command list (the `/help` popup, the `/sbx <type> <option>`
grammar, the container `gui` option, and the "did you mean" typo-suggestion
behaviour) lives in **`docs/command-reference.md`**. That file is the canonical
text mirror of `hh/src/ui.rs::help_clusters`. GUI/grammar quick form:

```
/sbx docker [gui] [image] [install]      # Parrot OS default; gui = noVNC desktop
/sbx podman [gui] [image] [install]      # Kali default;      gui = noVNC desktop
```

`gui` is honoured for Docker/Podman only (flagged no-op on headless/local);
noVNC is published on host loopback `127.0.0.1:6080` (open `http://127.0.0.1:6080`,
default password `hackhouse`).
