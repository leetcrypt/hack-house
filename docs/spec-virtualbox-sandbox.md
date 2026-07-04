# hack-house → VirtualBox Sandbox Backend — Spec

> **Status:** Draft v1 · **Date:** 2026-06-03
> **Scope:** Add VirtualBox as a sandbox backend, in two complementary modes:
> **(A)** a headless, owner-hosted VM driven through the existing shared PTY
> (drops into the current `Backend` abstraction), and **(B)** a *portable VM
> appliance* the room can hand out so each member boots the **actual GUI locally**
> on their own machine — including detecting and (with consent) installing
> VirtualBox if it's missing.
> **Baseline reviewed:** `hh/src/sbx.rs`, `hh/src/app.rs` @ `feat/ai-context`.

---

## 0. Decisions to lock

| # | Decision | Proposal |
|---|----------|----------|
| A | VirtualBox transport into the guest | **SSH** (NAT port-forward) as primary; `VBoxManage guestcontrol` as a no-SSH fallback. SSH gives a clean PTY and reuses the multipass provisioning model verbatim. |
| B | Single shared instance vs. per-user local copies | **Both, as two modes.** Mode A = one owner-hosted headless VM, shared PTY (zero-knowledge preserved). Mode B = export the VM as an `.ova`, distribute over the *existing* `/send` channel, each member imports + launches the GUI locally. |
| C | GUI sharing | **No live framebuffer relay.** Sharing the *desktop* = sharing the *appliance*, not the pixels. Sidesteps the zero-knowledge problem entirely (the image rides the encrypted file transfer). |
| D | Installation | **Detect-first, then opt-in install.** `ensure-vbox.sh` mirrors `ensure-docker.sh`: never installs silently; prints what it would do and requires an explicit `--yes` (or the `/sbx ... --install` flag). |

---

## 1. Why VirtualBox, and what's genuinely new

The existing backends (`Backend::{Local,Docker,Multipass}` in `hh/src/sbx.rs:51`)
are all **headless and text-only**. The owner hosts the box and runs a local PTY
into it (`command_for`, `sbx.rs:278`); the PTY bytes are encrypted with the room
key and relayed as `_sbx` frames, so the server only ever sees ciphertext.

VirtualBox adds two things the others can't:

1. **Arbitrary guest OSes** — Windows, BSD, old kernels, purpose-built
   malware-analysis or CTF images — with a mature snapshot tree.
2. **A real graphical desktop.** This is the part that doesn't fit the PTY relay,
   and it's the part you explicitly want: *people share a VM and each launches it
   locally with the GUI.*

So the integration is deliberately split so each mode keeps the project's trust
model intact:

- **Mode A (shared shell):** one VM, owner-hosted, driven collaboratively through
  the shared PTY — identical trust story to multipass.
- **Mode B (shared appliance):** the VM *image* is the shared artifact. It travels
  over the existing E2E `/send` transfer; each member runs their **own local copy**
  in the VirtualBox GUI. No pixels cross the wire — only the (encrypted) disk image.

---

## 2. Mode A — headless VirtualBox as a 4th backend

### 2.1 Enum + labels (`hh/src/sbx.rs`)

```rust
pub enum Backend { Local, Docker, Multipass, VirtualBox }   // new variant
```

- `Backend::parse`: add `"virtualbox" | "vbox" => Some(Backend::VirtualBox)`.
- `label()`: `"virtualbox"`.
- `default_image()`: a named base appliance, e.g. `"hh-base"` (an Ubuntu image we
  pre-register), since VirtualBox has no "pull by release string" like multipass.

### 2.2 Mapping every existing fn to `VBoxManage`

Each function in `sbx.rs` gets one new match arm. The transport (how a command
reaches *inside* the guest) is SSH over a NAT port-forward.

| Fn (`sbx.rs`) | Multipass today | VirtualBox arm |
|---|---|---|
| `prepare` (`:86`) | `multipass launch` | import appliance if absent (`VBoxManage import <ova> --vsys 0 --vmname <name>`), set forward (`modifyvm <name> --natpf1 "ssh,tcp,127.0.0.1,<port>,,22"`), then `startvm <name> --type headless`; if it already exists just `startvm`. Idempotent like the multipass arm. |
| `command_for` (`:278`) | `multipass exec … bash` | `ssh -tt -p <port> -o StrictHostKeyChecking=no <run_user>@127.0.0.1` (login shell). `run_user` empty ⇒ default account. |
| `provision` (`:355`) | `useradd` via `mp()` | identical `useradd`/sudoers scripts, run through an SSH helper `vbx()` (mirrors `mp()`/`dk()` at `sbx.rs:319`). Owner gets passwordless sudo via the same `mp_grant_sudo` script. |
| `set_sudo` (`:387`) | sudoers drop-in | same script over SSH; gate on `backend == VirtualBox`. |
| `save_state` (`:208`) | `multipass snapshot` | `VBoxManage snapshot <name> take <label> --pause` |
| `list_snapshots` (`:241`) | `multipass list --snapshots` | `VBoxManage snapshot <name> list --machinereadable`, parse `SnapshotName*=` lines |
| `teardown` (`:172`) | `multipass delete --purge` | `VBoxManage controlvm <name> poweroff` then `unregistervm <name> --delete` |

**Why SSH over `guestcontrol`:** `VBoxManage guestcontrol <vm> run --exe /bin/bash`
requires Guest Additions in the image and gives a rough PTY; interactive driving
is clunky. SSH needs only an sshd in the base image (cheap to bake once) and the
whole P4 permission stack (`/grant`, `/sudo`, drive ACL) works **unchanged**
because it's all "run a command through a transport." `guestcontrol` stays as a
documented fallback for images without sshd.

### 2.3 Port allocation

Each headless VM needs a unique host loopback port for its SSH forward. Reuse the
free-port discovery already used by the save/load PoC (see
`docs/demo-save-load-poc.md` / `hh/scripts/demo-save-load.sh`) so two sandboxes on one
host don't collide. The owner is the only one who ever connects to it
(`127.0.0.1:<port>`), so it never leaves the host.

### 2.4 Snapshots tie into existing `/sbx save`/`load`

`/sbx save`/`load`/`snaps` (`app.rs:1244`–`1307`) already branch on backend.
VirtualBox snapshots map cleanly onto the same commands, so the existing UX
("save state → quit → load") works for VBox with no new commands — just the new
match arms in `save_state`/`list_snapshots`, plus a VirtualBox arm in the `load`
path (today `load` hardcodes Docker at `app.rs:1282`; generalize it to the broker's
backend).

---

## 3. Mode B — share a VM, launch the GUI locally

This is the new product surface. The shared artifact is the **appliance**, not a
live session. Flow:

```
owner:   /sbx export [name]        → freezes the VM to an .ova on the owner's disk
owner:   /send hh-box.ova          → existing E2E file transfer (chunked, SHA-256)
member:  /accept                   → lands in ./downloads/ (existing path)
member:  /sbx open ./downloads/hh-box.ova
            → ensure VirtualBox is installed (detect; offer install)
            → VBoxManage import … --vmname hh-box-<member>
            → VBoxManage startvm hh-box-<member> --type gui   ← real desktop window
```

Everyone ends up running an **identical local VM** — same disk, same tools, same
state at export time — but each on their own machine, with a full GUI. Because the
image moved over the encrypted `/send` channel, the server never saw it, and there
is no live cross-machine display traffic to secure.

### 3.1 New commands

| Command | Who | Action |
|---|---|---|
| `/sbx export [name]` | owner of a VBox sandbox | `VBoxManage export <name> -o <out>.ova` (VM should be powered off or snapshot-exported). Emits the path and hints `/send` it. |
| `/sbx open <file.ova> [--install]` | any member | Detect VirtualBox → (consent) install if missing → `import` under a per-member VM name → `startvm --type gui`. |
| `/sbx gui [name]` | any member | Launch the GUI for an already-imported VM (`startvm --type gui`), or attach a running headless one (`VBoxManage startvm <name> --type separate`). |

`/sbx open` and `/sbx export` are deliberately **local-only** operations (like
`/pw`): they never broadcast. The only thing that crosses the room is the `.ova`
you choose to `/send`.

### 3.2 Relationship between the two modes

They compose: the owner can run a **Mode A** headless VM, `/sbx save` a snapshot,
`/sbx export` it to an `.ova`, and `/send` it — at which point each member can
`/sbx open` it and keep working **locally in the GUI** from the exact same state.
"Collaborate live in one shared shell" and "everyone take a copy home and run the
desktop" become two ends of one workflow.

---

## 4. Installation handling — `ensure-vbox.sh` (detect first)

Mirror `hh/scripts/ensure-docker.sh` exactly in spirit: **a backend never installs
anything silently.**

### 4.1 Detection (always first, zero side effects)

```rust
pub fn vbox_installed() -> bool {            // sbx.rs, beside docker_daemon_up()
    Command::new("VBoxManage").arg("--version")
        .stdout(Stdio::null()).stderr(Stdio::null())
        .status().map(|s| s.success()).unwrap_or(false)
}
```

If present, every Mode A/B path proceeds normally. If absent, the command **fails
loud with the remedy**, exactly like the Docker daemon message at `app.rs:1206`:

> `VirtualBox isn't installed — retry with /sbx open <file> --install to install it (needs sudo), or run ./ensure-vbox.sh in a terminal first`

### 4.2 The installer script

`hh/scripts/ensure-vbox.sh`, invoked as `bash ensure-vbox.sh --yes` only when the user
passed `--install` (matching how `prepare` shells `ensure-docker.sh --yes` at
`sbx.rs:31`). It:

1. Re-checks `VBoxManage --version`; if found, exits 0 immediately (idempotent).
2. Detects the platform and prints the **exact** command it will run *before*
   running it:
   - **Debian/Ubuntu:** `sudo apt-get install -y virtualbox` (or add Oracle's repo
     for a current build).
   - **Fedora:** `sudo dnf install -y VirtualBox`.
   - **Arch:** `sudo pacman -S --noconfirm virtualbox`.
   - **macOS:** `brew install --cask virtualbox` (note: needs the kernel-extension
     approval in System Settings; the script surfaces that as a manual step).
   - **Windows / unknown:** do **not** attempt; point at the download page and the
     `winget install Oracle.VirtualBox` one-liner.
3. On any failure, surfaces the last stderr line through the returned error (same
   pattern as `start_docker_daemon` at `sbx.rs:31`) so it lands in the TUI error
   popup, never bleeding raw onto the surface.

> **Honesty note for the spec:** VirtualBox needs a host kernel module
> (`vboxdrv`) and, on Secure-Boot machines, a signed/enrolled MOK. The script
> detects Secure Boot (`mokutil --sb-state`) and, rather than fail opaquely,
> tells the user the one manual step required. We check; we don't pretend it's
> always one command.

### 4.3 Consent UX

No surprise installs, no surprise sudo. The flow is: try → detect missing → tell
the user the remedy and the exact command → they re-issue with `--install`. This
matches the project's existing posture (`/sbx launch docker --start` is opt-in
daemon-start, not automatic).

---

## 5. Command surface (additions)

Extend the `/sbx` usage line (`app.rs:1309`):

```
/sbx launch [local|docker|multipass|virtualbox] [image]
/sbx stop | save [label] | load <label> | snaps
/sbx export [name]              # freeze host VM → .ova (then /send it)
/sbx open <file.ova> [--install]# import + launch the GUI locally
/sbx gui [name] [--install]     # launch GUI for an imported VM
```

| Command | Broadcasts? | Notes |
|---|---|---|
| `launch virtualbox` | yes (`_sbx status`) | Mode A; owner-hosted headless, shared PTY |
| `export` | no | local; produces an artifact to `/send` |
| `open` / `gui` | no | local; each member's own GUI window |

---

## 6. Code touchpoints (what actually changes)

Mode A is small — a new enum variant and ~7 match arms; the broker, drive ACL,
sudo delegation, and save/load machinery are all backend-agnostic already.

| File | Change |
|---|---|
| `hh/src/sbx.rs` | `Backend::VirtualBox` variant; arms in `parse`/`label`/`default_image`/`prepare`/`command_for`/`provision`/`set_sudo`/`save_state`/`list_snapshots`/`teardown`; `vbox_installed()`, `vbx()` SSH helper; `export_ova()` + `open_local()` (Mode B, local-only). |
| `hh/src/app.rs` | accept `virtualbox`/`vbox` in `/sbx launch`; generalize `/sbx load` off hardcoded Docker (`:1282`) to the broker backend; new `/sbx export`, `/sbx open`, `/sbx gui` arms; extend usage string (`:1309`); install-missing error mirroring `:1206`. |
| `hh/scripts/ensure-vbox.sh` (new) | detect-first installer, per `§4`. |
| `hh/src/ui.rs` | add the three new commands to the clustered help menu. |
| `README.MD` | backend table (`:175`) gains a `virtualbox` row; a short "share a VM, run it locally" subsection. |
| `models.toml` / docs | none. |

---

## 7. Security & trust notes

- **Mode A preserves zero-knowledge**: the VM is owner-local, the SSH forward is
  `127.0.0.1`-only, and only PTY ciphertext crosses the room — same as multipass.
- **Mode B preserves zero-knowledge** by *not* streaming a display at all. The
  `.ova` is just a file through the existing SHA-256-verified, Fernet-encrypted
  `/send` path (`hh/src/ft.rs`). Note the existing **50 MB** transfer cap (README
  `:215`) — real VM images blow past it, so Mode B needs either a raised cap for
  appliances or an out-of-band hand-off (documented honestly; see Open Questions).
- **No silent install, no silent sudo** (`§4.3`).
- **Appliance provenance**: a shared `.ova` is executable content. The spec should
  warn recipients exactly as `/accept` already implies trust in the sender — an
  imported VM runs code. Worth an explicit one-line caution in the `open` flow.

---

## 8. Phasing

| Phase | Deliverable | Gate |
|---|---|---|
| **V0** | `vbox_installed()` + `ensure-vbox.sh` (detect-first, Linux apt path) | manual: missing → guided install → present |
| **V1** | `Backend::VirtualBox` Mode A: launch/stop/PTY over SSH, provision, sudo | shared shell into a headless VBox VM, two clients driving |
| **V2** | snapshots: `save`/`load`/`snaps` arms; generalize `/sbx load` backend | save → stop → load round-trips on VBox |
| **V3** | Mode B: `/sbx export` → `/send` → `/sbx open` GUI launch | a second machine boots the shared appliance's desktop |
| **V4** | macOS/other-distro install paths; transfer-cap handling for `.ova` | cross-platform `open` works end-to-end |

---

## 9. Open questions

1. **Appliance size vs. the 50 MB `/send` cap.** Options: raise the cap for `.ova`
   only, compress (`.ova` + zstd), ship a thin base + a provisioning script instead
   of a fat image, or accept out-of-band transfer for large VMs and keep `/send`
   for small ones. Needs a product call.
2. **Base image sourcing.** Do we bake and ship an `hh-base.ova` (sshd + sudo
   preinstalled) so Mode A "just works," or import whatever the user points at and
   require them to have sshd? Baking one image once is the smoother UX.
3. **Per-member VM naming / cleanup** for Mode B locals — namespacing
   (`hh-box-<member>`) and a `/sbx open --replace` to re-import cleanly.
4. **`guestcontrol` fallback** — ship it in V1 or document-only until someone needs
   a no-sshd image?
```
