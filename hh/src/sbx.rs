//! Sandbox backends + a PTY-backed sandbox the broker drives.
//!
//! The broker (owner's client) spawns a sandbox shell inside a PTY. Output bytes
//! are pumped out of a reader thread onto an mpsc channel; the broker encrypts
//! them with the room key and relays them to the clergy as `sbx pty_data` frames.
//! Input frames (`sbx pty_input`) are written back into the PTY. The server only
//! ever sees ciphertext — identical trust model to chat/file transfer.

use anyhow::{Context, Result};
use portable_pty::{native_pty_system, Child, CommandBuilder, MasterPty, PtySize};
use std::io::{Read, Write};
use std::process::{Command, Stdio};
use std::sync::mpsc;

/// Helper that ensures the Docker daemon is running (ships in hh/scripts/).
const ENSURE_DOCKER: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/scripts/ensure-docker.sh");
/// Detect-first Multipass installer (ships in hh/scripts/).
const ENSURE_MULTIPASS: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/scripts/ensure-multipass.sh");
/// Detect-first Podman installer (apt; rootless preflight). Ships in hh/scripts/.
const ENSURE_PODMAN: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/scripts/ensure-podman.sh");
/// Detect-first VirtualBox installer (ships in hh/scripts/).
const ENSURE_VBOX: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/scripts/ensure-vbox.sh");
/// Baseline dev-toolchain installer run inside Docker sandboxes (hh/scripts/).
const SBX_BOOTSTRAP: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/scripts/sandbox-bootstrap.sh");
/// Editable package schema the bootstrap installs — vim+curl always added.
const SBX_TOOLS_JSON: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/scripts/sandbox-tools.json");
/// Creates a fresh, cloud-init-provisioned Ubuntu VirtualBox VM (hh/scripts/).
const VBOX_NEW: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/scripts/vbox-new.sh");
/// Browse + locally-build VMs from the curated library (hh/scripts/).
const VBOX_LIBRARY_SH: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/scripts/vbox-library.sh");
/// The library manifest: pointers (URLs/pages) to VMs, never the images. The
/// loader cross-references `list_vms` to mark which are already built locally.
const VBOX_LIBRARY_JSON: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/scripts/vbox-library.json");

/// Is the `docker` binary installed? (`docker --version` succeeds.) This is a
/// weaker check than `docker_daemon_up`: the CLI can be present while the daemon
/// is down. We need both before a Docker sandbox can launch.
pub fn docker_installed() -> bool {
    Command::new("docker")
        .arg("--version")
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map(|s| s.success())
        .unwrap_or(false)
}

/// Is the Docker daemon accepting connections? (`docker info` succeeds.)
pub fn docker_daemon_up() -> bool {
    Command::new("docker")
        .arg("info")
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map(|s| s.success())
        .unwrap_or(false)
}

/// Is the `podman` binary installed? (`podman --version` succeeds.) Podman is
/// daemonless, so unlike Docker there is no separate daemon-up check: if the CLI
/// is present a rootless container can launch immediately (no daemon, no sudo).
pub fn podman_installed() -> bool {
    Command::new("podman")
        .arg("--version")
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map(|s| s.success())
        .unwrap_or(false)
}

/// Is this a Docker Desktop (Linux) install? Its engine runs in a per-user VM
/// started by the *user* unit `docker-desktop.service` — there's no root
/// `docker.service`, so starting the daemon needs **no sudo**. Detect it so the
/// launch path doesn't pop a (useless, and on this box failing) sudo prompt.
pub fn docker_desktop() -> bool {
    Command::new("systemctl")
        .args(["--user", "cat", "docker-desktop.service"])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map(|s| s.success())
        .unwrap_or(false)
}

/// Can we sudo *without* a password prompt right now? (`sudo -n true` succeeds
/// when credentials are cached via a prior `sudo -v`, or NOPASSWD is configured.)
/// The launch paths that need root check this first: a raw-mode TUI can't host
/// sudo's interactive tty prompt, so we must never let sudo block on one.
pub fn sudo_ready() -> bool {
    Command::new("sudo")
        .args(["-n", "true"])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map(|s| s.success())
        .unwrap_or(false)
}

/// Run an `ensure-*.sh` installer `--yes` with the given extra args, optionally
/// feeding a sudo password to the script's `sudo -S` via stdin. Shared by every
/// backend installer (docker/podman/multipass/vbox) so sudo capture is uniform.
///
/// Sudo ladder (the scripts honour all three):
///   * password present ⇒ `--stdin-pass` ⇒ the script uses `sudo -S -p ''`,
///     reading the secret from stdin — never the controlling tty (a raw-mode TUI
///     would corrupt a tty prompt). The first sudo caches the credential.
///   * no password ⇒ stdin closed; the script's `--yes` selects `sudo -n`, which
///     fails fast (clear error) if creds aren't cached rather than hanging on a
///     tty prompt the TUI can't host.
///
/// Secret handling: the password (if any) only ever travels parent→child stdin;
/// the local buffer is wiped immediately after. It is NEVER echoed, logged, or
/// surfaced — sudo never prints the password, so the captured stderr (used for
/// error messages) can't contain it.
fn run_ensure(script: &str, extra: &[&str], password: Option<String>) -> Result<(bool, String)> {
    let mut cmd = Command::new("bash");
    cmd.arg(script).arg("--yes");
    for a in extra {
        cmd.arg(a);
    }
    if password.is_some() {
        cmd.arg("--stdin-pass").stdin(Stdio::piped());
    } else {
        cmd.stdin(Stdio::null());
    }
    cmd.stdout(Stdio::piped()).stderr(Stdio::piped());
    let mut child = cmd
        .spawn()
        .with_context(|| format!("running {script}"))?;
    if let Some(mut pw) = password {
        if let Some(mut stdin) = child.stdin.take() {
            // Feed the password to the first `sudo -S`; it caches the credential
            // so the rest of the plan authenticates without re-reading stdin.
            let _ = writeln!(stdin, "{pw}"); // stdin drops here → EOF
        }
        // Best-effort wipe of our copy of the secret.
        unsafe {
            for b in pw.as_bytes_mut() {
                *b = 0;
            }
        }
        pw.clear();
    }
    let out = child
        .wait_with_output()
        .with_context(|| format!("{script}"))?;
    Ok((out.status.success(), String::from_utf8_lossy(&out.stderr).into_owned()))
}

/// Start the Docker daemon via `ensure-docker.sh --yes`, waiting until it's
/// ready. With `password`, escalation goes through `sudo -S` (read from stdin);
/// without it the script uses `sudo -n` and fails fast if creds aren't cached.
fn start_docker_daemon(password: Option<String>) -> Result<()> {
    let (ok, err) = run_ensure(ENSURE_DOCKER, &[], password)?;
    if !ok {
        let last = err
            .lines()
            .last()
            .unwrap_or("could not start the docker daemon");
        anyhow::bail!("{last}");
    }
    Ok(())
}

/// Install Docker via `ensure-docker.sh --install --yes` (Docker's official,
/// GPG-verified repo), then leave the daemon started. Consent is the caller's
/// job (they passed `install`); the script is idempotent if Docker is present.
/// `password` feeds `sudo -S` as above.
pub fn ensure_docker_install(password: Option<String>) -> Result<()> {
    let (ok, err) = run_ensure(ENSURE_DOCKER, &["--install"], password)?;
    if !ok {
        let last = err.lines().last().unwrap_or("could not install Docker");
        anyhow::bail!("{last}");
    }
    Ok(())
}

/// Install Podman via `ensure-podman.sh --yes` (apt + a rootless subuid/subgid
/// preflight). Consent is the caller's job (they passed `install`); the script is
/// idempotent if Podman is already present. Daemonless — nothing to start after.
/// `password` feeds the script's `sudo -S` (apt needs root); without it the
/// script's `sudo -n` fails fast rather than hanging. Returns the last error line.
pub fn ensure_podman_install(password: Option<String>) -> Result<()> {
    let (ok, err) = run_ensure(ENSURE_PODMAN, &[], password)?;
    if !ok {
        let last = err.lines().last().unwrap_or("could not install Podman");
        anyhow::bail!("{last}");
    }
    Ok(())
}

/// Is Multipass installed? (`multipass version` succeeds.)
pub fn multipass_installed() -> bool {
    Command::new("multipass")
        .arg("version")
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map(|s| s.success())
        .unwrap_or(false)
}

/// Install Multipass via `ensure-multipass.sh --yes`. On Linux this is a snap
/// install (the only supported channel). Consent is the caller's job; the
/// script is idempotent if Multipass is already present. Returns the script's
/// last error line on failure (e.g. snapd missing, or needs sudo).
pub fn ensure_multipass_install(password: Option<String>) -> Result<()> {
    let (ok, err) = run_ensure(ENSURE_MULTIPASS, &[], password)?;
    if !ok {
        let last = err.lines().last().unwrap_or("could not install Multipass");
        anyhow::bail!("{last}");
    }
    Ok(())
}

// ---- VirtualBox (local GUI VMs) ---------------------------------------------
// VirtualBox is integrated as a *local* facility rather than a shared-PTY
// backend: a room shares a VM by handing out its appliance, and each member
// boots it in the real VirtualBox GUI on their own machine. None of this relays
// over the room — only the (separately `/send`-ed) image does — so the
// zero-knowledge model is untouched. A Windows guest has no sshd/guestcontrol
// shell to drive, so the GUI launch is the honest fit, not a faked PTY.

/// Is VirtualBox installed? (`VBoxManage --version` succeeds.)
pub fn vbox_installed() -> bool {
    Command::new("VBoxManage")
        .arg("--version")
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map(|s| s.success())
        .unwrap_or(false)
}

/// VirtualBox version string (e.g. `7.1.2r164945`), or None if not installed.
pub fn vbox_version() -> Option<String> {
    let out = Command::new("VBoxManage").arg("--version").output().ok()?;
    let v = String::from_utf8_lossy(&out.stdout).trim().to_string();
    (out.status.success() && !v.is_empty()).then_some(v)
}

/// Install VirtualBox via `ensure-vbox.sh --yes`. Consent is the caller's job
/// (they passed `--install`); detection is the script's (idempotent if present).
/// `password` feeds the script's `sudo -S` (apt needs root); without it the
/// script's `sudo -n` fails fast rather than hanging on a tty prompt the TUI
/// can't host. Returns the script's last error line on failure.
pub fn ensure_vbox_install(password: Option<String>) -> Result<()> {
    let (ok, err) = run_ensure(ENSURE_VBOX, &[], password)?;
    if !ok {
        let last = err.lines().last().unwrap_or("could not install VirtualBox");
        anyhow::bail!("{last}");
    }
    Ok(())
}

/// Names of registered VirtualBox VMs (`VBoxManage list vms`). Each line is
/// `"name" {uuid}`; we return the unquoted names.
pub fn list_vms() -> Result<Vec<String>> {
    let out = Command::new("VBoxManage")
        .args(["list", "vms"])
        .output()
        .context("VBoxManage list vms (is VirtualBox installed?)")?;
    Ok(String::from_utf8_lossy(&out.stdout)
        .lines()
        .filter_map(|l| {
            let start = l.find('"')? + 1;
            let end = l[start..].find('"')? + start;
            Some(l[start..end].to_string())
        })
        .filter(|s| !s.is_empty())
        .collect())
}

/// Is a VM with this name registered locally? (i.e. present in `list_vms`).
/// Used to tell a "host" (already has the appliance imported) from someone who
/// still needs it pulled in before `gui_launch` can find it.
pub fn vm_registered(name: &str) -> bool {
    list_vms()
        .map(|vms| vms.iter().any(|v| v == name))
        .unwrap_or(false)
}

/// Import a VirtualBox appliance (`.ova`/`.ovf`) so its VM registers locally and
/// becomes launchable via `gui_launch`. This is how a non-host "pulls" a shared
/// VM onto their own machine after the appliance arrives over the encrypted
/// channel. Blocking — run off the UI thread. Returns the imported VM's name.
pub fn import_appliance(ova: &std::path::Path) -> Result<String> {
    let before = list_vms().unwrap_or_default();
    let out = Command::new("VBoxManage")
        .arg("import")
        .arg(ova)
        .output()
        .context("VBoxManage import (is VirtualBox installed?)")?;
    if !out.status.success() {
        let err = String::from_utf8_lossy(&out.stderr);
        anyhow::bail!(
            "VBoxManage import failed: {}",
            err.lines().last().unwrap_or("").trim()
        );
    }
    // The imported name is whatever's newly registered; fall back to the file's
    // stem if the diff is ambiguous (e.g. a re-import of an existing name).
    let after = list_vms().unwrap_or_default();
    let added = after.into_iter().find(|v| !before.contains(v));
    Ok(added.unwrap_or_else(|| {
        ova.file_stem()
            .and_then(|s| s.to_str())
            .unwrap_or("imported-vm")
            .to_string()
    }))
}

/// Is a VM currently running? (`VBoxManage list runningvms`)
pub fn vm_running(name: &str) -> bool {
    Command::new("VBoxManage")
        .args(["list", "runningvms"])
        .output()
        .map(|o| {
            let needle = format!("\"{name}\"");
            String::from_utf8_lossy(&o.stdout)
                .lines()
                .any(|l| l.contains(&needle))
        })
        .unwrap_or(false)
}

/// Launch a registered VM's GUI locally (`VBoxManage startvm <name> --type gui`).
/// The window opens on the caller's own desktop — this is the "share a VM, run
/// it locally" path; nothing about the display is relayed to the room.
pub fn gui_launch(name: &str) -> Result<String> {
    if vm_running(name) {
        return Ok(format!("{name} is already running"));
    }
    let out = Command::new("VBoxManage")
        .args(["startvm", name, "--type", "gui"])
        .output()
        .context("VBoxManage startvm (is VirtualBox installed?)")?;
    if !out.status.success() {
        let err = String::from_utf8_lossy(&out.stderr);
        anyhow::bail!(
            "startvm failed: {}",
            err.lines().last().unwrap_or("").trim()
        );
    }
    Ok(format!("launched {name} (GUI)"))
}

/// Create + boot a *fresh* Ubuntu VirtualBox VM pre-provisioned with the dev
/// toolchain, by driving `scripts/vbox-new.sh` (downloads a cloud image, builds
/// a cloud-init NoCloud seed installing scripts/sandbox-tools.json, then boots
/// the GUI). Unlike `/sbx launch vbox <vm>`, which opens an *existing* image,
/// this builds one from scratch — the only path that doesn't need a pre-made VM.
///
/// Blocking and slow on first run (~600MB image download). Runs the script
/// non-interactively (`--yes`); returns its final status line(s) — including the
/// generated login password, which is shown exactly once.
pub fn vbox_new(name: &str, user: &str) -> Result<String> {
    let out = Command::new("bash")
        .arg(VBOX_NEW)
        .args(["--yes", "--name", name, "--user", user])
        .output()
        .context("running vbox-new.sh (is bash available?)")?;
    let stderr = String::from_utf8_lossy(&out.stderr);
    if !out.status.success() {
        anyhow::bail!(
            "vbox-new failed: {}",
            stderr.lines().rev().find(|l| !l.trim().is_empty()).unwrap_or("").trim()
        );
    }
    // The script narrates progress on stderr. Surface the final success line and,
    // if one was generated, the one-time login password.
    let last = stderr
        .lines()
        .rev()
        .find(|l| l.trim_start().starts_with('✓'))
        .unwrap_or("VM created")
        .trim();
    let pw = stderr.lines().find(|l| l.contains("login password"));
    Ok(match pw {
        Some(p) => format!("{last} · {}", p.trim()),
        None => last.to_string(),
    })
}

/// Names of VMs that are currently running (`VBoxManage list runningvms`), so the
/// `/sbx vms` listing can flag which of the registered VMs are live.
pub fn running_vms() -> Vec<String> {
    Command::new("VBoxManage")
        .args(["list", "runningvms"])
        .output()
        .map(|o| {
            String::from_utf8_lossy(&o.stdout)
                .lines()
                .filter_map(|l| {
                    let start = l.find('"')? + 1;
                    let end = l[start..].find('"')? + start;
                    Some(l[start..end].to_string())
                })
                .filter(|s| !s.is_empty())
                .collect()
        })
        .unwrap_or_default()
}

// ---- VM library (curated pointers; built locally on demand) -----------------
//
// The repo ships scripts/vbox-library.json — POINTERS only (download URLs/pages),
// never the multi-GB images. Choosing a VM builds it on the caller's own machine
// via scripts/vbox-library.sh. Here we parse the manifest for listing/info; the
// heavy build is shelled out to the script (which already handles download +
// VBoxManage create/import + boot, with rollback).

/// One curated VM as declared in the manifest. Only the fields we surface in the
/// TUI are deserialized; the rest of the JSON (specs, firmware, etc.) is consumed
/// by the build script.
#[derive(Clone, Debug, serde::Deserialize)]
pub struct LibraryVm {
    pub id: String,
    pub name: String,
    pub os: String,
    pub size: String,
    #[serde(default)]
    pub page: String,
    #[serde(default)]
    pub notes: String,
    /// Filled in by `vbox_library()` (not in the JSON): is a VM of this name
    /// already registered in VirtualBox on this machine?
    #[serde(skip)]
    pub installed: bool,
}

#[derive(serde::Deserialize)]
struct LibraryManifest {
    vms: Vec<LibraryVm>,
}

/// Parse the VM library manifest, marking each entry `installed` if a VM of that
/// display name is already registered locally (`list_vms`). The catalog is the
/// same for everyone; `installed` is per-machine, so this works identically for a
/// host or any room member — VirtualBox VMs are always local to the caller.
pub fn vbox_library() -> Result<Vec<LibraryVm>> {
    let text = std::fs::read_to_string(VBOX_LIBRARY_JSON)
        .with_context(|| format!("reading VM library manifest ({VBOX_LIBRARY_JSON})"))?;
    let manifest: LibraryManifest =
        serde_json::from_str(&text).context("parsing vbox-library.json")?;
    let have = list_vms().unwrap_or_default();
    Ok(manifest
        .vms
        .into_iter()
        .map(|mut v| {
            v.installed = have.iter().any(|n| n == &v.name);
            v
        })
        .collect())
}

/// Look up a single library entry by its id (with `installed` resolved).
pub fn library_vm(id: &str) -> Option<LibraryVm> {
    vbox_library().ok()?.into_iter().find(|v| v.id == id)
}

/// Build a library VM locally by driving scripts/vbox-library.sh `--install`.
/// Blocking and potentially slow (large download); run off the UI thread. No
/// sudo is needed — VBoxManage create/import run as the user. `iso` supplies a
/// locally-downloaded installer for entries with no auto-download (e.g. Windows).
/// Returns the script's final status line(s).
pub fn vbox_library_install(id: &str, iso: Option<String>) -> Result<String> {
    let mut cmd = Command::new("bash");
    cmd.arg(VBOX_LIBRARY_SH).args(["--install", id, "--yes"]);
    if let Some(path) = iso.as_deref() {
        cmd.args(["--iso", path]);
    }
    let out = cmd
        .output()
        .context("running vbox-library.sh (is bash available?)")?;
    let stderr = String::from_utf8_lossy(&out.stderr);
    if !out.status.success() {
        // The script narrates on stderr and ends with a ✖ reason / pointer.
        let reason = stderr
            .lines()
            .rev()
            .find(|l| !l.trim().is_empty())
            .unwrap_or("build failed")
            .trim();
        anyhow::bail!("{reason}");
    }
    // Surface the final ✓/ⓘ status line.
    let last = stderr
        .lines()
        .rev()
        .find(|l| {
            let t = l.trim_start();
            t.starts_with('✓') || t.starts_with('ⓘ')
        })
        .unwrap_or("done")
        .trim();
    Ok(last.to_string())
}

/// A running hypervisor that holds the CPU's VT-x/VMX root mode. While one is
/// live, VirtualBox can't boot a *hardware* VM (it aborts with
/// `VERR_VMX_IN_VMX_ROOT_MODE`). We only mark holders we know how to stop
/// cleanly (`stoppable`); an unrecognised qemu/kvm process is reported but never
/// killed — the caller aborts and lets the user deal with it.
#[derive(Clone, Debug)]
pub struct VtxHolder {
    /// Human label, e.g. "Docker Desktop" or "multipass VM 'example-vm'".
    pub label: String,
    /// "docker" | "multipass" | "unknown" — selects the stop strategy.
    pub kind: String,
    /// Instance name to `multipass stop`; empty for non-multipass holders.
    pub instance: String,
    /// Whether we know a clean, reversible way to stop it (restartable after).
    pub stoppable: bool,
}

/// Is a KVM kernel module loaded? (Cheap early-out before scanning processes.)
fn kvm_loaded() -> bool {
    std::fs::read_to_string("/proc/modules")
        .map(|s| {
            s.lines()
                .any(|l| l.starts_with("kvm_intel") || l.starts_with("kvm_amd"))
        })
        .unwrap_or(false)
}

/// Detect running KVM-accelerated hypervisors that hold VT-x and would block a
/// VirtualBox hardware VM. Pure detection — stops nothing. Empty vec = clear.
pub fn vtx_holders() -> Vec<VtxHolder> {
    if !kvm_loaded() {
        return Vec::new();
    }
    let out = match Command::new("ps").args(["-eo", "args="]).output() {
        Ok(o) => o,
        Err(_) => return Vec::new(),
    };
    classify_vtx_holders(&String::from_utf8_lossy(&out.stdout))
}

/// Pure classifier: turn `ps -eo args=` output into the VT-x holders we know how
/// to (or refuse to) stop. Split out from `vtx_holders` so it can be unit-tested
/// without a live process table. Only KVM-accelerated qemu lines count; Docker
/// Desktop collapses to a single holder; multipass is named; anything else is an
/// unknown, non-stoppable holder (we won't kill what we can't cleanly restart).
fn classify_vtx_holders(text: &str) -> Vec<VtxHolder> {
    let mut holders = Vec::new();
    let mut docker_seen = false;
    for args in text.lines() {
        let is_qemu = args.contains("qemu-system");
        let uses_kvm = args.contains("--enable-kvm")
            || args.contains("-accel kvm")
            || args.contains("accel=kvm");
        if !(is_qemu && uses_kvm) {
            continue;
        }
        if args.contains("docker-desktop") || args.contains("/.docker/") {
            // Docker Desktop runs one linuxkit VM; collapse to a single holder.
            if !docker_seen {
                docker_seen = true;
                holders.push(VtxHolder {
                    label: "Docker Desktop".into(),
                    kind: "docker".into(),
                    instance: String::new(),
                    stoppable: true,
                });
            }
        } else if let Some(name) = multipass_instance(args) {
            holders.push(VtxHolder {
                label: format!("multipass VM '{name}'"),
                kind: "multipass".into(),
                instance: name,
                stoppable: true,
            });
        } else {
            holders.push(VtxHolder {
                label: "an unknown KVM/QEMU virtual machine".into(),
                kind: "unknown".into(),
                instance: String::new(),
                stoppable: false,
            });
        }
    }
    holders
}

/// Pull a multipass instance name out of a qemu command line. Multipass keeps
/// each disk under `.../multipassd/vault/instances/<name>/...`.
fn multipass_instance(args: &str) -> Option<String> {
    if !args.contains("multipass") {
        return None;
    }
    let marker = "/instances/";
    let i = args.find(marker)? + marker.len();
    let rest = &args[i..];
    let end = rest.find('/').unwrap_or(rest.len());
    let name = &rest[..end];
    (!name.is_empty()).then(|| name.to_string())
}

/// Stop one VT-x holder with a clean, *reversible* command so it can be
/// restarted later. Refuses to touch holders marked `stoppable=false`.
pub fn stop_vtx_holder(h: &VtxHolder) -> Result<()> {
    match h.kind.as_str() {
        "docker" => {
            let out = Command::new("systemctl")
                .args(["--user", "stop", "docker-desktop"])
                .output()
                .context("stopping Docker Desktop")?;
            if !out.status.success() {
                let err = String::from_utf8_lossy(&out.stderr);
                anyhow::bail!(
                    "could not stop Docker Desktop: {}",
                    err.lines().last().unwrap_or("").trim()
                );
            }
            Ok(())
        }
        "multipass" => {
            let out = Command::new("multipass")
                .args(["stop", &h.instance])
                .output()
                .context("stopping multipass VM")?;
            if !out.status.success() {
                let err = String::from_utf8_lossy(&out.stderr);
                anyhow::bail!(
                    "could not stop multipass '{}': {}",
                    h.instance,
                    err.lines().last().unwrap_or("").trim()
                );
            }
            Ok(())
        }
        _ => anyhow::bail!("refusing to stop {} automatically", h.label),
    }
}

/// Which sandbox to summon. Multipass = strong isolation (default for real use),
/// Podman = daemonless/rootless containers (no sudo), Docker = fast, Local = no
/// isolation (dev/testing only).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Backend {
    Local,
    Docker,
    Podman,
    Multipass,
    /// A physical device reached over SSH (e.g. the Pineapple Pager). No image,
    /// no provisioning, no snapshots — `command_for` just opens `ssh -tt <alias>`
    /// and the alias rides in the sandbox `name`. Reuses the whole PTY stream +
    /// keystroke relay + driver-ACL unchanged; container-only steps are no-ops
    /// (same shape as `Local`).
    Device,
}

impl Backend {
    pub fn parse(s: &str) -> Option<Backend> {
        match s {
            "local" => Some(Backend::Local),
            "docker" => Some(Backend::Docker),
            "podman" => Some(Backend::Podman),
            "multipass" => Some(Backend::Multipass),
            // A physical device over SSH. `device` takes an explicit alias
            // positional; `pager` is sugar for the WiFi Pineapple Pager alias.
            "device" | "pager" => Some(Backend::Device),
            _ => None,
        }
    }
    pub fn label(self) -> &'static str {
        match self {
            Backend::Local => "local-shell",
            Backend::Docker => "docker",
            Backend::Podman => "podman",
            Backend::Multipass => "multipass",
            Backend::Device => "device",
        }
    }
    /// Default image/release when the user doesn't specify one.
    pub fn default_image(self) -> &'static str {
        match self {
            Backend::Multipass => "24.04",
            // Docker defaults to Parrot OS (Security). Debian/apt-based, so the
            // apt-only sandbox-bootstrap.sh path works unchanged. `parrotsec/core`
            // is the minimal base (bootstrap fills the toolchain); swap for
            // `parrotsec/security` per-launch if you want the full pentest set.
            Backend::Docker => "docker.io/parrotsec/core",
            // Podman defaults to Kali (rolling). Still Debian/apt-based, so the
            // apt-only sandbox-bootstrap.sh path works unchanged; base is minimal
            // (no pentest metapackages pulled by default — keep first launch fast).
            Backend::Podman => "docker.io/kalilinux/kali-rolling",
            Backend::Local => "",
            Backend::Device => "",
        }
    }
    /// The exec family a co-located agent uses to run a command *inside* this
    /// sandbox — `docker`/`podman exec`, `multipass exec`, or host `local`.
    /// Advertised in the `_sbx:status` frame (distinct from `label`, whose Local
    /// value is the cosmetic "local-shell") so the bridge can build the right
    /// `<engine> exec` invocation for the backend that holds the sandbox.
    pub fn engine(self) -> &'static str {
        match self {
            Backend::Local => "local",
            Backend::Docker => "docker",
            Backend::Podman => "podman",
            Backend::Multipass => "multipass",
            Backend::Device => "device",
        }
    }
}

/// The container-engine binary for an OCI backend. Docker and Podman share the
/// same CLI grammar (`run/exec/commit/save/rm/images`), so the container code
/// path is written once and parameterised by this. Non-container backends have
/// no engine binary; calling this on them is a logic error (they never reach the
/// container arms), so we default to "docker" rather than panic.
fn engine_bin(backend: Backend) -> &'static str {
    match backend {
        Backend::Podman => "podman",
        _ => "docker",
    }
}

/// One-time setup before the PTY shell is spawned. Blocking — run off the UI
/// thread (Multipass boots a real VM, ~20-30s). Idempotent: reuses an instance
/// that already exists.
pub fn prepare(
    backend: Backend,
    name: &str,
    image: &str,
    start_daemon: bool,
    password: Option<String>,
) -> Result<()> {
    match backend {
        Backend::Local => Ok(()),
        // A device is already up (its own OS); nothing to prepare.
        Backend::Device => Ok(()),
        Backend::Multipass => {
            let exists = Command::new("multipass")
                .args(["info", name])
                .output()
                .map(|o| o.status.success())
                .unwrap_or(false);
            if !exists {
                // Capture output so it can't bleed onto the TUI surface; surface
                // the failure reason through the returned error instead.
                let out = Command::new("multipass")
                    .args([
                        "launch", "--name", name, "--cpus", "1", "--memory", "1G", "--disk", "5G",
                        image,
                    ])
                    .output()
                    .context("multipass launch (is multipass installed?)")?;
                if !out.status.success() {
                    let err = String::from_utf8_lossy(&out.stderr);
                    anyhow::bail!(
                        "multipass launch failed: {}",
                        err.lines().last().unwrap_or("").trim()
                    );
                }
            } else {
                let _ = Command::new("multipass")
                    .args(["start", name])
                    .stdout(Stdio::null())
                    .stderr(Stdio::null())
                    .status();
            }
            Ok(())
        }
        Backend::Docker | Backend::Podman => {
            let engine = engine_bin(backend);
            // Docker needs a running daemon before any call; rather than fail with
            // a raw connection error, start it (the caller confirmed via
            // `/sbx launch docker --start`). Podman is daemonless — skip the check
            // and the sudo modal entirely; a rootless container launches as-is.
            if backend == Backend::Docker && !docker_daemon_up() {
                if start_daemon {
                    start_docker_daemon(password).context("starting docker daemon")?;
                } else {
                    anyhow::bail!(
                        "docker daemon is not running — retry with `/sbx launch docker --start`"
                    );
                }
            }
            // Reclaim sandboxes abandoned by hack-house sessions that died
            // without running `teardown` (SIGKILL/panic) before adding our own.
            sweep_stale(backend);
            // Persistent container so we can exec in to provision users + shells.
            let _ = Command::new(engine)
                .args(["rm", "-f", name])
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .status();
            // Capture output so a failure can't paint over the TUI; the reason is
            // surfaced through the returned error (shown in the error popup).
            let mut run = Command::new(engine);
            // `--init` puts a real init (catatonit) at PID 1 instead of the
            // `sleep infinity` below. `sleep` only ever calls nanosleep() — it
            // never wait()s — so every process orphaned inside the sandbox
            // reparented to it and stayed an unreapable zombie for the life of
            // the container. catatonit reaps them.
            run.args(["run", "-d", "--init", "--name", name, "--hostname", name, "-w", "/root"]);
            // Ownership labels: `teardown` handles every clean exit path, but a
            // SIGKILL/panic can't run it. The labels let `sweep_stale` (below)
            // reclaim containers whose owning hack-house is gone.
            let owner = format!("{OWNER_PID_LABEL}={}", std::process::id());
            run.args(["--label", &format!("{SANDBOX_LABEL}=1"), "--label", &owner]);
            // The native harness runs the model host-side and only execs commands
            // into the container, so the sandbox no longer needs to reach host
            // Ollama. The old in-container Ollama gateway (Docker host-gateway /
            // Podman slirp4netns host-loopback) is therefore gone — and with it the
            // rootless-Podman loopback bug it used to work around.
            // Isolation hardening — argv-identical to cmd_chat/operator/sandbox.py
            // `_hardening_flags`. A safe DoS bound (`--pids-limit`, env-tunable) is
            // ALWAYS on and never affects normal use. `HH_SBX_HARDEN=strict` adds
            // the full escape-resistant posture (cap-drop=ALL, no-new-privileges,
            // read-only, network=none) — that breaks apt/pip/git/su, so it is for
            // EMPTY or locked-down/escape-sensitive rooms only. See the hh-redteam skill.
            let pids = std::env::var("HH_SBX_PIDS").unwrap_or_else(|_| "4096".into());
            run.args(["--pids-limit", &pids]);
            let mem = std::env::var("HH_SBX_MEMORY").unwrap_or_default();
            let mem = mem.trim();
            if !mem.is_empty() {
                run.args(["--memory", mem, "--memory-swap", mem]);
            }
            let hardened = matches!(
                std::env::var("HH_SBX_HARDEN").unwrap_or_default().trim().to_lowercase().as_str(),
                "strict" | "escape" | "max"
            );
            if hardened {
                run.args([
                    "--cap-drop=ALL", "--security-opt=no-new-privileges", "--read-only",
                    "--tmpfs", "/tmp", "--tmpfs", "/root:rw,exec", "--network=none",
                ]);
            }
            // Egress control (HH_SBX_EGRESS) — same posture ladder as the Python operator
            // (cmd_chat/operator/egress.py). Gateway modes shell to the single-source script
            // scripts/hh-sbx-egress.py. Skipped when hardened (already --network=none).
            let egress = std::env::var("HH_SBX_EGRESS").unwrap_or_default().trim().to_lowercase();
            // Default `auto`: Tor exit if Tor is available on this host, else local — and
            // if the Tor gateway can't come up, fall back to local. A launch is never
            // refused for egress reasons (no false-negative connections). Stricter
            // postures (guard/tor/none/…) stay available via HH_SBX_EGRESS.
            let egress = if egress.is_empty() { "auto".to_string() } else { egress };
            let auto = egress == "auto";
            let mut mode = if auto {
                if tor_available() { "tor".to_string() } else { "local".to_string() }
            } else {
                egress.clone()
            };
            let mut egress_gw_mode: Option<String> = None;
            if !hardened {
                match mode.as_str() {
                    "none" => { run.args(["--network=none"]); }
                    "open" => {}
                    "guard" => {
                        if !route_tunneled() {
                            anyhow::bail!("HH_SBX_EGRESS=guard: sandbox egress is not tunneled \
                                (VPN/Tor down) — refusing networked launch; HH_SBX_EGRESS=open to allow");
                        }
                    }
                    "local" | "scope" | "tor" => match egress_gateway_up(engine, name, &mode) {
                        Ok(netarg) => { run.arg(netarg); egress_gw_mode = Some(mode.clone()); }
                        Err(e) => {
                            if auto && mode == "tor" {
                                // auto: Tor gateway unavailable — fall back to local.
                                mode = "local".to_string();
                                match egress_gateway_up(engine, name, &mode) {
                                    Ok(netarg) => { run.arg(netarg); egress_gw_mode = Some(mode.clone()); }
                                    Err(e2) => anyhow::bail!("egress gateway (local fallback) refused: {e2}"),
                                }
                            } else {
                                anyhow::bail!("egress gateway ({mode}) refused: {e}");
                            }
                        }
                    },
                    other => anyhow::bail!("unknown HH_SBX_EGRESS='{other}'"),
                }
            }
            run.args([image, "sleep", "infinity"]);
            let out = run
                .output()
                .with_context(|| format!("{engine} run (is {engine} installed?)"))?;
            if !out.status.success() {
                if let Some(m) = &egress_gw_mode { egress_gateway_down(engine, name); let _ = m; }
                let err = String::from_utf8_lossy(&out.stderr);
                anyhow::bail!("{engine} run failed: {}", err.lines().last().unwrap_or("").trim());
            }
            if let Some(m) = egress_gw_mode { egress_postjoin(engine, name, &m); }
            Ok(())
        }
    }
}

/// Label marking a container as an hh sandbox we're allowed to reclaim.
pub const SANDBOX_LABEL: &str = "hh.sandbox";
/// Label carrying the PID of the hack-house process that created it.
pub const OWNER_PID_LABEL: &str = "hh.owner-pid";

/// The single-source sandbox-egress gateway CLI (shared with the Python operator).
const HH_SBX_EGRESS_SCRIPT: &str =
    concat!(env!("CARGO_MANIFEST_DIR"), "/../scripts/hh-sbx-egress.py");

/// Whether a Tor egress gateway can plausibly be brought up — used to resolve the
/// `auto` default toward Tor. Cheap PATH probe (mirrors egress.py `tor_available`);
/// the definitive test is bringing the gateway up, and `auto` falls back to `local`
/// if that fails, so a false positive here costs one fallback, not a failed launch.
fn tor_available() -> bool {
    std::process::Command::new("sh")
        .args(["-c", "command -v tor >/dev/null 2>&1"])
        .status()
        .map(|s| s.success())
        .unwrap_or(false)
}

/// Is the host's default route a VPN/Tor tunnel? Mirrors egress.py's VPN_IFACE_RE.
fn route_tunneled() -> bool {
    let out = match Command::new("ip").args(["route", "get", "1.1.1.1"]).output() {
        Ok(o) => o,
        Err(_) => return false,
    };
    let s = String::from_utf8_lossy(&out.stdout);
    if let Some(idx) = s.find(" dev ") {
        let dev = s[idx + 5..].split_whitespace().next().unwrap_or("");
        return ["proton", "wg", "tun", "tap", "tailscale", "ppp", "nordlynx", "mullvad", "ipsec"]
            .iter()
            .any(|p| dev.starts_with(p));
    }
    false
}

/// Create the egress gateway via the shared script; return the `--network=container:...`
/// flag to add to `podman run`, or an error (fail-closed — caller must refuse the launch).
fn egress_gateway_up(engine: &str, name: &str, mode: &str) -> Result<String> {
    let out = Command::new("python3")
        .args([HH_SBX_EGRESS_SCRIPT, "up", name, "--egress", mode, "--engine", engine])
        .output()
        .with_context(|| "running hh-sbx-egress up")?;
    let so = String::from_utf8_lossy(&out.stdout);
    for line in so.lines() {
        if let Some(flag) = line.strip_prefix("NETARG=") {
            return Ok(flag.to_string());
        }
    }
    let reason = so.lines().last().unwrap_or("").trim();
    anyhow::bail!("{}", if reason.is_empty() { "gateway up failed" } else { reason });
}

/// Sandbox-side setup after it joined the gateway netns (tor: resolver). Best-effort.
fn egress_postjoin(engine: &str, name: &str, mode: &str) {
    let _ = Command::new("python3")
        .args([HH_SBX_EGRESS_SCRIPT, "postjoin", name, "--egress", mode, "--engine", engine])
        .output();
}

/// Remove the sandbox's egress gateway (best-effort; no-op if none).
fn egress_gateway_down(engine: &str, name: &str) {
    let _ = Command::new("python3")
        .args([HH_SBX_EGRESS_SCRIPT, "down", name, "--engine", engine])
        .output();
}

/// Reclaim sandbox containers whose owning hack-house process is gone.
///
/// `teardown` covers every *clean* exit (quit, SIGTERM, SIGHUP), but a SIGKILL,
/// a panic, or a yanked terminal leaves the container running `sleep infinity`
/// forever — and with it a pile of orphaned processes. Containers are stamped
/// with the creator's PID at launch, so a container whose PID no longer exists
/// in /proc is unambiguously abandoned. Called before each launch, so a crashed
/// session is cleaned up by the next one. Best-effort and silent: never block a
/// launch on cleanup. Returns how many were removed.
pub fn sweep_stale(backend: Backend) -> usize {
    let engine = match backend {
        Backend::Docker | Backend::Podman => engine_bin(backend),
        _ => return 0,
    };
    let out = match Command::new(engine)
        .args([
            "ps",
            "-a",
            "--filter",
            &format!("label={SANDBOX_LABEL}=1"),
            "--format",
            &format!("{{{{.Names}}}} {{{{index .Labels \"{OWNER_PID_LABEL}\"}}}}"),
        ])
        .output()
    {
        Ok(o) if o.status.success() => o,
        _ => return 0,
    };
    let mut swept = 0;
    for line in String::from_utf8_lossy(&out.stdout).lines() {
        let (name, pid) = match line.trim().split_once(' ') {
            Some((n, p)) if !n.is_empty() => (n, p.trim()),
            // No PID label (or a stray blank line): not ours to judge — leave it.
            _ => continue,
        };
        // An unparseable PID means we can't prove the owner is dead. Leave it.
        let Ok(pid) = pid.parse::<u32>() else { continue };
        if pid == std::process::id() || std::path::Path::new(&format!("/proc/{pid}")).exists() {
            continue; // owner still alive — hands off
        }
        if Command::new(engine)
            .args(["rm", "-f", name])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .map(|s| s.success())
            .unwrap_or(false)
        {
            swept += 1;
        }
    }
    swept
}

/// Destroy ephemeral resources after stop. Multipass instance is purged;
/// the Docker container is removed; Local is a no-op.
pub fn teardown(backend: Backend, name: &str) {
    match backend {
        Backend::Multipass => {
            // Multipass snapshots live *inside* the instance, so `delete --purge`
            // would destroy any saved state. If the user has snapshots worth
            // keeping (so `/sbx load <label>` can restore them), just *stop* the
            // instance — it persists, powered off, ready to be restored. With no
            // snapshots there's nothing to preserve, so purge to stay clean.
            let has_snaps = list_snapshots(Backend::Multipass, name)
                .map(|s| !s.is_empty())
                .unwrap_or(false);
            let args: &[&str] = if has_snaps {
                &["stop", name]
            } else {
                &["delete", name, "--purge"]
            };
            let _ = Command::new("multipass")
                .args(args)
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .status();
        }
        Backend::Docker | Backend::Podman => {
            let eng = engine_bin(backend);
            let _ = Command::new(eng)
                .args(["rm", "-f", name])
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .status();
            egress_gateway_down(eng, name);  // remove the sandbox's egress gateway, if any
        }
        Backend::Local => {}
        // The ssh session just closes; the device stays up. The ControlMaster
        // persists briefly (ControlPersist) and reaps itself.
        Backend::Device => {}
    }
}

/// Local Docker image repo under which sandbox snapshots are committed
/// (`hh-snap:<label>`). Owner-side only — never pushed anywhere.
pub const SNAP_REPO: &str = "hh-snap";

/// Directory (relative to the working dir) where portable snapshot artifacts
/// land when the saver opts into a local file copy (`/sbx save … --local`).
/// Created on demand. These are standalone files the owner keeps — they survive
/// even if the in-backend image/snapshot is later pruned.
pub const SNAP_DIR: &str = "hh-snapshots";

/// Ensure `hh-snapshots/` exists and return it. Blocking.
fn snap_dir() -> Result<std::path::PathBuf> {
    let dir = std::path::PathBuf::from(SNAP_DIR);
    std::fs::create_dir_all(&dir).with_context(|| format!("creating {SNAP_DIR}/"))?;
    Ok(dir)
}

/// Snapshot the running sandbox's filesystem state to a named artifact on the
/// owner's machine. Blocking — run off the UI thread.
///
/// - **Docker:** `docker commit` into `hh-snap:<label>` — instant, captures the
///   live container (including provisioned users), and survives `/sbx stop`
///   because the image is independent of the container.
/// - **Multipass:** `multipass snapshot <name> --name <label>`. Multipass
///   requires the instance be **stopped** first; if it isn't, multipass's own
///   error is surfaced verbatim.
/// - **Local:** no backing VM/container, so nothing to save.
///
/// `local` adds a portable, standalone copy under `hh-snapshots/` (a file the
/// owner fully controls) on top of the in-backend snapshot:
/// - **Docker:** `docker save` the committed image to a `.tar`.
/// - **Multipass:** snapshots already live on this host under multipass's own
///   data dir; the CLI has no single-file export, so we note that and skip the
///   extra file rather than fake one.
///
/// Returns a short human description of what was written on success.
pub fn save_state(backend: Backend, name: &str, label: &str, local: bool) -> Result<String> {
    match backend {
        Backend::Docker | Backend::Podman => {
            let engine = engine_bin(backend);
            let tag = format!("{SNAP_REPO}:{label}");
            let out = Command::new(engine)
                .args(["commit", name, &tag])
                .output()
                .with_context(|| format!("{engine} commit (is {engine} installed?)"))?;
            if !out.status.success() {
                let err = String::from_utf8_lossy(&out.stderr);
                anyhow::bail!("{engine} commit failed: {}", err.lines().last().unwrap_or("").trim());
            }
            if local {
                let path = snap_dir()?.join(format!("hh-snap-{label}.tar"));
                let out = Command::new(engine)
                    .args(["save", &tag, "-o"])
                    .arg(&path)
                    .output()
                    .with_context(|| format!("{engine} save"))?;
                if !out.status.success() {
                    let err = String::from_utf8_lossy(&out.stderr);
                    anyhow::bail!(
                        "image {tag} committed, but local export failed: {}",
                        err.lines().last().unwrap_or("").trim()
                    );
                }
                return Ok(format!("image {tag} + local file {}", path.display()));
            }
            Ok(format!("image {tag}"))
        }
        Backend::Multipass => {
            // Multipass only snapshots a *stopped* instance (unlike docker, which
            // commits a live container). Power it off first — the caller has
            // already torn down the shared shell, so this is the save *and* stop.
            // The instance stays registered (we don't purge), so `/sbx load` can
            // restore the snapshot later.
            let _ = Command::new("multipass")
                .args(["stop", name])
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .status();
            let out = Command::new("multipass")
                .args(["snapshot", name, "--name", label])
                .output()
                .context("multipass snapshot (is multipass installed?)")?;
            if !out.status.success() {
                let err = String::from_utf8_lossy(&out.stderr);
                anyhow::bail!(
                    "multipass snapshot failed: {}",
                    err.lines().last().unwrap_or("").trim()
                );
            }
            if local {
                // multipass stores snapshots on this host already; no portable
                // single-file export exists in the CLI, so be honest about it.
                return Ok(format!(
                    "snapshot {name}.{label} (already stored locally by multipass; no portable file)"
                ));
            }
            Ok(format!("snapshot {name}.{label}"))
        }
        Backend::Local => {
            anyhow::bail!("the local shell has no VM state to save — launch a docker or multipass sandbox first")
        }
        Backend::Device => {
            anyhow::bail!("a device has no snapshot state — pull its loot with the device bridge (@<device> pull) instead")
        }
    }
}

/// Export an already-committed `hh-snap:<label>` image to a portable `.tar` under
/// `hh-snapshots/` — the standalone file a peer can receive over `/send`. This is
/// the `--local` half of `save_state` made reusable for `/sbx publish`, so an
/// image saved *without* `--local` can still be made tradeable later. Idempotent:
/// returns the existing file if it's already there. Blocking.
pub fn export_image(backend: Backend, label: &str) -> Result<std::path::PathBuf> {
    let engine = match backend {
        Backend::Docker | Backend::Podman => engine_bin(backend),
        _ => anyhow::bail!("only docker/podman snapshots export to a portable .tar"),
    };
    let path = snap_dir()?.join(format!("hh-snap-{label}.tar"));
    if path.exists() {
        return Ok(path);
    }
    let tag = format!("{SNAP_REPO}:{label}");
    let out = Command::new(engine)
        .args(["save", &tag, "-o"])
        .arg(&path)
        .output()
        .with_context(|| format!("{engine} save"))?;
    if !out.status.success() {
        let err = String::from_utf8_lossy(&out.stderr);
        anyhow::bail!(
            "exporting {tag} failed: {}",
            err.lines().last().unwrap_or("").trim()
        );
    }
    Ok(path)
}

/// Pull the `(tag, label)` of a loaded snapshot out of `<engine> load` stdout.
/// docker prints `Loaded image: hh-snap:<label>`; podman prints
/// `Loaded image: localhost/hh-snap:<label>` (registry-qualified). We locate the
/// `hh-snap:` marker anywhere in a line and normalise to the bare `hh-snap:<label>`
/// tag — podman resolves the unqualified form back to `localhost/…` on use, and
/// docker stores it bare, so the bare tag is the portable handle. Pure (no engine
/// call) so the dual-engine output shapes stay under test.
fn parse_loaded_tag(stdout: &str) -> Option<(String, String)> {
    let marker = format!("{SNAP_REPO}:");
    let tag = stdout
        .lines()
        .find_map(|l| l.find(&marker).map(|i| l[i..].trim().to_string()))?;
    let label = tag.split_once(':').map(|(_, l)| l.to_string()).unwrap_or_default();
    Some((tag, label))
}

/// Load a received `hh-snap-<label>.tar` image archive into the local engine —
/// the receive-side inverse of `export_image`. Tries docker first, then podman
/// (a docker `save` archive loads into either). Returns `(engine, image_tag,
/// label)` parsed from the engine's `Loaded image:` line. Blocking.
pub fn import_image_archive(path: &std::path::Path) -> Result<(String, String, String)> {
    let mut last_err = String::new();
    for engine in ["docker", "podman"] {
        let out = match Command::new(engine).args(["load", "-i"]).arg(path).output() {
            Ok(o) => o,
            Err(_) => continue, // engine not installed — try the next
        };
        if !out.status.success() {
            last_err = String::from_utf8_lossy(&out.stderr).trim().to_string();
            continue;
        }
        let stdout = String::from_utf8_lossy(&out.stdout);
        let (tag, label) = parse_loaded_tag(&stdout)
            .with_context(|| format!("{engine} load: no {SNAP_REPO}:… tag in output"))?;
        return Ok((engine.to_string(), tag, label));
    }
    anyhow::bail!(
        "couldn't load image archive (no docker/podman, or load failed: {})",
        if last_err.is_empty() { "engine missing" } else { &last_err }
    )
}

/// Run a throwaway container from `image` to read its `.hh-agent` manifest, so a
/// just-imported snapshot can be registered with its purpose/status/todo without
/// booting it into the room. `<engine> run --rm <image> cat …`; None if the image
/// carries no manifest. Blocking.
pub fn read_image_manifest(engine: &str, image: &str) -> Option<String> {
    let out = Command::new(engine)
        .args(["run", "--rm", image, "cat", "/root/.hh-agent/manifest.yaml"])
        .stderr(Stdio::null())
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    let text = String::from_utf8_lossy(&out.stdout).into_owned();
    if text.trim().is_empty() {
        None
    } else {
        Some(text)
    }
}

/// Restore a Multipass instance to a saved snapshot — the load-side inverse of
/// the multipass branch of `save_state`. `multipass restore <name>.<label>`
/// requires the instance to exist and be **stopped**, which is exactly the
/// state `teardown` leaves it in when snapshots are present. Blocking; the
/// caller boots it afterwards via the normal `prepare` (existing instance →
/// `multipass start`) path. The leading `--destructive` skips the interactive
/// "overwrite current state?" prompt — safe here because restoring *is* the
/// intent and any unsaved drift since the snapshot is what the user wants gone.
pub fn mp_restore(name: &str, label: &str) -> Result<String> {
    let target = format!("{name}.{label}");
    let out = Command::new("multipass")
        .args(["restore", "--destructive", &target])
        .output()
        .context("multipass restore (is multipass installed?)")?;
    if !out.status.success() {
        let err = String::from_utf8_lossy(&out.stderr);
        anyhow::bail!(
            "multipass restore failed: {}",
            err.lines().last().unwrap_or("").trim()
        );
    }
    Ok(format!("restored multipass instance to snapshot '{label}'"))
}

/// Snapshot a VirtualBox VM's state. VirtualBox isn't a brokered `Backend` (its
/// GUI runs locally, never relayed), so it has its own save path. Blocking.
///
/// `VBoxManage snapshot <vm> take <label>` works whether the VM is running
/// (live snapshot) or powered off. `local` additionally exports the whole VM to
/// a portable `.ova` appliance under `hh-snapshots/` — the standalone artifact
/// you'd hand to someone else (or re-import yourself) via `/sbx gui`.
pub fn vm_save_state(vm: &str, label: &str, local: bool) -> Result<String> {
    let out = Command::new("VBoxManage")
        .args(["snapshot", vm, "take", label])
        .output()
        .context("VBoxManage snapshot take (is VirtualBox installed?)")?;
    if !out.status.success() {
        let err = String::from_utf8_lossy(&out.stderr);
        anyhow::bail!(
            "VBoxManage snapshot failed: {}",
            err.lines().last().unwrap_or("").trim()
        );
    }
    if local {
        let path = snap_dir()?.join(format!("{vm}-{label}.ova"));
        let out = Command::new("VBoxManage")
            .args(["export", vm, "-o"])
            .arg(&path)
            .output()
            .context("VBoxManage export")?;
        if !out.status.success() {
            let err = String::from_utf8_lossy(&out.stderr);
            anyhow::bail!(
                "snapshot '{label}' taken, but OVA export failed: {}",
                err.lines().last().unwrap_or("").trim()
            );
        }
        return Ok(format!(
            "snapshot '{label}' + local appliance {}",
            path.display()
        ));
    }
    Ok(format!("snapshot '{label}' of {vm}"))
}

/// Restore a VirtualBox VM to a snapshot — the inverse of `vm_save_state`.
/// `label` picks a named snapshot; `None` restores the VM's *current* snapshot.
/// VirtualBox refuses to restore a live machine, so the VM must be powered off.
/// Blocking; the caller boots the GUI afterwards.
pub fn vm_restore(vm: &str, label: Option<&str>) -> Result<String> {
    if vm_running(vm) {
        anyhow::bail!("‘{vm}’ is running — power it off first, then restore the snapshot");
    }
    let args: Vec<&str> = match label {
        Some(l) => vec!["snapshot", vm, "restore", l],
        None => vec!["snapshot", vm, "restorecurrent"],
    };
    let out = Command::new("VBoxManage")
        .args(&args)
        .output()
        .context("VBoxManage snapshot restore (is VirtualBox installed?)")?;
    if !out.status.success() {
        let err = String::from_utf8_lossy(&out.stderr);
        anyhow::bail!(
            "VBoxManage snapshot restore failed: {}",
            err.lines().last().unwrap_or("").trim()
        );
    }
    Ok(match label {
        Some(l) => format!("restored ‘{vm}’ to snapshot '{l}'"),
        None => format!("restored ‘{vm}’ to its current snapshot"),
    })
}

/// Snapshot names for a VirtualBox VM (`VBoxManage snapshot <vm> list
/// --machinereadable` → `SnapshotName*="..."` lines). Blocking. An empty list
/// (exit 1 "does not have any snapshots") is reported as no snapshots, not an
/// error.
pub fn vm_snapshots(vm: &str) -> Result<Vec<String>> {
    let out = Command::new("VBoxManage")
        .args(["snapshot", vm, "list", "--machinereadable"])
        .output()
        .context("VBoxManage snapshot list (is VirtualBox installed?)")?;
    if !out.status.success() {
        let err = String::from_utf8_lossy(&out.stderr);
        if err.contains("does not have any snapshots") {
            return Ok(Vec::new());
        }
        anyhow::bail!(
            "VBoxManage snapshot list failed: {}",
            err.lines().last().unwrap_or("").trim()
        );
    }
    Ok(String::from_utf8_lossy(&out.stdout)
        .lines()
        .filter_map(|l| {
            let v = l.strip_prefix("SnapshotName")?;
            let q = v.split_once('=')?.1.trim();
            Some(q.trim_matches('"').to_string())
        })
        .filter(|s| !s.is_empty())
        .collect())
}

/// List saved snapshot labels for a backend (Docker image tags under
/// `hh-snap`, or multipass snapshots of the instance). Blocking.
pub fn list_snapshots(backend: Backend, name: &str) -> Result<Vec<String>> {
    match backend {
        Backend::Docker | Backend::Podman => {
            let engine = engine_bin(backend);
            let out = Command::new(engine)
                .args(["images", SNAP_REPO, "--format", "{{.Tag}}"])
                .output()
                .with_context(|| format!("{engine} images"))?;
            Ok(String::from_utf8_lossy(&out.stdout)
                .lines()
                .map(str::trim)
                .filter(|s| !s.is_empty() && *s != "<none>")
                .map(str::to_string)
                .collect())
        }
        Backend::Multipass => {
            // `multipass list --snapshots` columns: Instance Snapshot Parent Comment.
            let out = Command::new("multipass")
                .args(["list", "--snapshots"])
                .output()
                .context("multipass list --snapshots")?;
            Ok(String::from_utf8_lossy(&out.stdout)
                .lines()
                .skip(1) // header row
                .filter_map(|l| {
                    let mut cols = l.split_whitespace();
                    let inst = cols.next()?;
                    let snap = cols.next()?;
                    (inst == name).then(|| snap.to_string())
                })
                .collect())
        }
        Backend::Local => Ok(Vec::new()),
        Backend::Device => Ok(Vec::new()),
    }
}

/// Where a `/sbx load <label>` snapshot lives, so the loader knows which backend
/// to restore through. A bare label is ambiguous (a docker image tag *and* a
/// multipass snapshot could share it), so we probe and prefer whichever exists.
#[derive(Clone, Copy, PartialEq, Eq)]
pub enum SnapKind {
    Docker,
    Podman,
    Multipass,
    None,
}

/// Resolve a snapshot label to the backend that holds it. Probes multipass first
/// (its snapshots are instance-scoped and rarer), then the OCI engines' `hh-snap`
/// repo (docker, then podman). Blocking — run off the UI thread. A backend whose
/// CLI is absent simply reports no match rather than erroring, so a missing
/// multipass doesn't block a docker load and vice-versa.
pub fn locate_snapshot(name: &str, label: &str) -> SnapKind {
    if list_snapshots(Backend::Multipass, name)
        .map(|s| s.iter().any(|l| l == label))
        .unwrap_or(false)
    {
        return SnapKind::Multipass;
    }
    if list_snapshots(Backend::Docker, name)
        .map(|s| s.iter().any(|l| l == label))
        .unwrap_or(false)
    {
        return SnapKind::Docker;
    }
    if podman_installed()
        && list_snapshots(Backend::Podman, name)
            .map(|s| s.iter().any(|l| l == label))
            .unwrap_or(false)
    {
        return SnapKind::Podman;
    }
    SnapKind::None
}

/// Build the shell command for a backend, running as unix user `run_user`
/// (empty = backend default). The container/VM is already up (see `prepare`).
fn command_for(backend: Backend, name: &str, run_user: &str) -> CommandBuilder {
    match backend {
        Backend::Local => {
            let mut c = CommandBuilder::new("bash");
            c.arg("-i");
            c
        }
        Backend::Docker | Backend::Podman => {
            let user = if run_user.is_empty() {
                "root"
            } else {
                run_user
            };
            let mut c = CommandBuilder::new(engine_bin(backend));
            c.args(["exec", "-it", "-u", user, name, "bash", "-il"]);
            c
        }
        Backend::Multipass => {
            let mut c = CommandBuilder::new("multipass");
            if run_user.is_empty() {
                c.args(["exec", name, "--", "bash", "-il"]);
            } else {
                // Login shell as the provisioned owner account (a real sudoer).
                c.args(["exec", name, "--", "sudo", "-u", run_user, "-i"]);
            }
            c
        }
        Backend::Device => {
            // `name` IS the ssh alias (the Copy enum can't carry it). Multiplex
            // over the same ControlMaster the Python ssh_conn opens, so a device
            // already dialled by the bridge is reused; `-tt` forces a remote PTY
            // even without a local tty, so the PTY loop drives it like any shell.
            let ctl = format!("{}/.ssh/cm-hh-{}.sock",
                std::env::var("HOME").unwrap_or_else(|_| "/root".into()), name);
            let mut c = CommandBuilder::new("ssh");
            c.args([
                "-tt",
                "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=accept-new",
                "-o", "ControlMaster=auto",
                "-o", &format!("ControlPath={ctl}"),
                "-o", "ControlPersist=300",
                "-o", "ConnectTimeout=12",
                name,
            ]);
            c
        }
    }
}

/// Sanitize a clergy display name into a safe unix username.
pub fn unix_name(name: &str) -> String {
    let s: String = name
        .to_lowercase()
        .chars()
        .filter(|c| c.is_ascii_alphanumeric() || *c == '_' || *c == '-')
        .take(31)
        .collect();
    s.trim_start_matches(['-', '_']).to_string()
}

fn mp(name: &str, args: &[&str]) {
    let mut a = vec!["exec", name, "--"];
    a.extend_from_slice(args);
    // Null stdio so provisioning chatter never bleeds onto the TUI surface.
    let _ = Command::new("multipass")
        .args(a)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status();
}
fn dk(engine: &str, name: &str, args: &[&str]) {
    let mut a = vec!["exec", name];
    a.extend_from_slice(args);
    let _ = Command::new(engine)
        .args(a)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status();
}

#[derive(serde::Deserialize)]
struct ToolsCfg {
    packages: Vec<String>,
}

/// The space-joined apt package list every Docker sandbox installs at launch.
/// Read from `scripts/sandbox-tools.json` so it's editable without code changes;
/// `vim`+`curl` are always present, names are sanitized to a safe charset, and
/// duplicates collapse. Missing/invalid JSON degrades gracefully to vim+curl.
fn sandbox_pkgs() -> String {
    let mut out: Vec<String> = vec!["vim".into(), "curl".into()];
    if let Ok(raw) = std::fs::read_to_string(SBX_TOOLS_JSON) {
        if let Ok(cfg) = serde_json::from_str::<ToolsCfg>(&raw) {
            for p in cfg.packages {
                // Keep only valid apt package-name chars so a stray edit can't
                // smuggle shell metacharacters into the install command.
                let p: String = p
                    .trim()
                    .chars()
                    .filter(|c| c.is_ascii_alphanumeric() || "._+-".contains(*c))
                    .collect();
                if !p.is_empty() && !out.contains(&p) {
                    out.push(p);
                }
            }
        }
    }
    out.join(" ")
}

/// Run the sandbox bootstrap inside the container, piping the script to `bash -s`
/// on stdin (keeps it out of argv) with the resolved package list in the env.
/// Blocking — provision() is already off the UI thread.
fn dk_bootstrap(engine: &str, name: &str) {
    let script = std::fs::read_to_string(SBX_BOOTSTRAP).unwrap_or_else(|_| {
        // Degraded fallback if the script file is missing: still refresh the
        // index and install whatever the env carries (at minimum vim+curl).
        "apt-get update -qq && apt-get install -y --no-install-recommends $HH_SBX_PKGS".into()
    });
    let pkgs_env = format!("HH_SBX_PKGS={}", sandbox_pkgs());
    let child = Command::new(engine)
        .args([
            "exec", "-i", "-e", &pkgs_env, name, "bash", "-s",
        ])
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn();
    if let Ok(mut c) = child {
        if let Some(mut stdin) = c.stdin.take() {
            let _ = stdin.write_all(script.as_bytes());
        }
        let _ = c.wait();
    }
}

/// Install the baseline dev toolchain (scripts/sandbox-tools.json; vim+curl
/// guaranteed) inside a Multipass VM. It already ships apt + sudo with a
/// populated index, so this is a direct install. The package list is sanitized
/// by `sandbox_pkgs`, so interpolating it into the shell command is safe.
/// Blocking — provision() is off the UI thread.
fn mp_bootstrap(name: &str) {
    let cmd = format!(
        "export DEBIAN_FRONTEND=noninteractive; apt-get update -qq && \
         apt-get install -y --no-install-recommends {} || true",
        sandbox_pkgs()
    );
    mp(name, &["sudo", "bash", "-c", &cmd]);
}

/// Grant a Multipass user real *passwordless* sudo (group + sudoers.d drop-in)
/// so they're a usable superuser non-interactively. `u` is already unix-safe.
fn mp_grant_sudo(name: &str, u: &str) {
    let script = format!(
        "usermod -aG sudo {u}; printf '{u} ALL=(ALL) NOPASSWD:ALL\\n' > /etc/sudoers.d/90-{u}; chmod 440 /etc/sudoers.d/90-{u}"
    );
    mp(name, &["sudo", "bash", "-c", &script]);
}
fn mp_revoke_sudo(name: &str, u: &str) {
    let script = format!("gpasswd -d {u} sudo 2>/dev/null; rm -f /etc/sudoers.d/90-{u}");
    mp(name, &["sudo", "bash", "-c", &script]);
}

/// Provision a real unix account per clergy member inside the VM/container and
/// make the owner a superuser (sudoer). Returns the unix user the shared shell
/// should run as. Blocking — call off the UI thread.
pub fn provision(backend: Backend, name: &str, owner: &str, members: &[String]) -> String {
    let run = unix_name(owner);
    match backend {
        Backend::Multipass => {
            for m in members {
                let u = unix_name(m);
                if !u.is_empty() {
                    mp(name, &["sudo", "useradd", "-m", "-s", "/bin/bash", &u]);
                }
            }
            if !run.is_empty() {
                mp_grant_sudo(name, &run); // owner = passwordless superuser
            }
            mp_bootstrap(name); // same baseline dev toolchain as the docker sandbox
            run
        }
        Backend::Docker | Backend::Podman => {
            let engine = engine_bin(backend);
            // Install the baseline dev toolchain (editable list in
            // scripts/sandbox-tools.json; vim+curl guaranteed) so a fresh
            // sandbox comes up usable instead of bare. The script also refreshes
            // the apt index (base images ship without /var/lib/apt/lists) and is
            // idempotent + sentinel-guarded, so re-provisions stay fast.
            dk_bootstrap(engine, name);
            for m in members {
                let u = unix_name(m);
                if !u.is_empty() {
                    dk(engine, name, &["useradd", "-m", "-s", "/bin/bash", &u]);
                }
            }
            // Base images usually lack the sudo package; the shared shell runs as
            // root (superuser) and drive-grant is the delegation mechanism.
            "root".to_string()
        }
        Backend::Local => String::new(),
        // No unix accounts to provision on a remote device — its shell logs in
        // as whatever the ssh alias configures. The empty run-user is correct.
        Backend::Device => String::new(),
    }
}

/// Grant/revoke real sudo for a member's unix account (Multipass; sudo is
/// preinstalled). No-op for Docker (no sudo pkg) / Local.
pub fn set_sudo(backend: Backend, name: &str, user: &str, enable: bool) {
    let u = unix_name(user);
    if u.is_empty() || backend != Backend::Multipass {
        return;
    }
    if enable {
        mp_grant_sudo(name, &u);
    } else {
        mp_revoke_sudo(name, &u);
    }
}

/// The unix user the shared shell runs as for a backend — mirrors `provision`'s
/// return (owner on Multipass, root on Docker, none on Local) but side-effect
/// free, so callers like file-push can resolve the destination home without
/// threading extra state through the broker.
pub fn run_user_for(backend: Backend, owner: &str) -> String {
    match backend {
        Backend::Multipass => unix_name(owner),
        Backend::Docker | Backend::Podman => "root".to_string(),
        Backend::Local => String::new(),
        Backend::Device => String::new(),
    }
}

/// Inject a local file/dir into the running sandbox, dropping it under the
/// shared shell user's home and leaving it owned by that user. The path is
/// streamed in as a tar (built + size-capped by `ft::tar_path`) and unpacked by
/// `tar` inside the container/VM — uniform for files and directories, and no
/// shell interpolation of the path. Blocking — run off the UI thread. Returns
/// the in-sandbox destination path on success.
pub fn push(
    backend: Backend,
    name: &str,
    run_user: &str,
    local: &std::path::Path,
) -> Result<String> {
    let (base, tar) = crate::ft::tar_path(local)?;
    match backend {
        Backend::Local => anyhow::bail!(
            "local sandbox shares the host filesystem — {} is already reachable",
            local.display()
        ),
        Backend::Docker | Backend::Podman => {
            // Shell runs as root with $HOME=/root (see `prepare`'s `-w /root`).
            let mut c = Command::new(engine_bin(backend));
            c.args(["exec", "-i", name, "tar", "-C", "/root", "-xf", "-"]);
            extract_tar(c, &tar)?;
            Ok(format!("/root/{base}"))
        }
        Backend::Multipass => {
            let home = format!("/home/{run_user}");
            let mut c = Command::new("multipass");
            c.args(["exec", name, "--", "sudo", "tar", "-C", &home, "-xf", "-"]);
            extract_tar(c, &tar)?;
            // Hand ownership to the account whose login shell the clergy share
            // (the tar unpacked as root via sudo).
            let owns = format!("{run_user}:{run_user}");
            let target = format!("{home}/{base}");
            mp(name, &["sudo", "chown", "-R", &owns, &target]);
            Ok(target)
        }
        Backend::Device => anyhow::bail!(
            "file push over /sbx isn't supported for a device — use the device bridge (@<device> push)"
        ),
    }
}

/// Feed `tar` bytes to a `tar -x` process over stdin, surfacing the extractor's
/// own last error line on failure.
fn extract_tar(mut cmd: Command, tar: &[u8]) -> Result<()> {
    cmd.stdin(Stdio::piped())
        .stdout(Stdio::null())
        .stderr(Stdio::piped());
    let mut child = cmd
        .spawn()
        .context("spawn tar extractor (is the backend CLI installed?)")?;
    {
        let mut si = child.stdin.take().context("tar stdin")?;
        si.write_all(tar).context("stream tar to sandbox")?;
    } // drop closes stdin → tar sees EOF and finishes
    let out = child.wait_with_output().context("await tar extractor")?;
    anyhow::ensure!(
        out.status.success(),
        "extract failed: {}",
        String::from_utf8_lossy(&out.stderr)
            .lines()
            .last()
            .unwrap_or("")
            .trim()
    );
    Ok(())
}

pub struct Sandbox {
    // Held for the PTY's lifetime (dropping it closes the terminal) + resize.
    #[allow(dead_code)]
    master: Box<dyn MasterPty + Send>,
    child: Box<dyn Child + Send + Sync>,
    writer: Box<dyn Write + Send>,
    #[allow(dead_code)]
    pub backend: Backend,
}

impl Sandbox {
    /// Spawn the backend in a PTY. A reader thread pushes raw output bytes onto
    /// `out`; the caller relays them (encrypted) to the clergy.
    pub fn launch(
        backend: Backend,
        name: &str,
        run_user: &str,
        rows: u16,
        cols: u16,
        out: mpsc::Sender<Vec<u8>>,
    ) -> Result<Sandbox> {
        let pty = native_pty_system();
        let pair = pty
            .openpty(PtySize {
                rows,
                cols,
                pixel_width: 0,
                pixel_height: 0,
            })
            .context("openpty")?;

        let cmd = command_for(backend, name, run_user);
        let child = pair
            .slave
            .spawn_command(cmd)
            .with_context(|| format!("spawn {} sandbox", backend.label()))?;
        drop(pair.slave); // close our handle so EOF propagates on exit

        let mut reader = pair.master.try_clone_reader().context("clone pty reader")?;
        let writer = pair.master.take_writer().context("take pty writer")?;

        std::thread::spawn(move || {
            let mut buf = [0u8; 8192];
            loop {
                match reader.read(&mut buf) {
                    Ok(0) | Err(_) => break,
                    Ok(n) => {
                        if out.send(buf[..n].to_vec()).is_err() {
                            break; // broker gone
                        }
                    }
                }
            }
        });

        Ok(Sandbox {
            master: pair.master,
            child,
            writer,
            backend,
        })
    }

    pub fn write_input(&mut self, data: &[u8]) -> Result<()> {
        self.writer.write_all(data)?;
        self.writer.flush()?;
        Ok(())
    }

    #[allow(dead_code)] // wired up with PTY-resize sync (P3b)
    pub fn resize(&self, rows: u16, cols: u16) -> Result<()> {
        self.master
            .resize(PtySize {
                rows,
                cols,
                pixel_width: 0,
                pixel_height: 0,
            })
            .context("pty resize")
    }

    pub fn stop(&mut self) {
        let _ = self.child.kill();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{Duration, Instant};

    #[test]
    fn parse_loaded_tag_handles_docker_and_podman() {
        // docker: bare tag.
        assert_eq!(
            parse_loaded_tag("Loaded image: hh-snap:kali-recon\n"),
            Some(("hh-snap:kali-recon".to_string(), "kali-recon".to_string()))
        );
        // podman: registry-qualified — must strip `localhost/` to the bare tag.
        assert_eq!(
            parse_loaded_tag("Loaded image: localhost/hh-snap:kali-recon\n"),
            Some(("hh-snap:kali-recon".to_string(), "kali-recon".to_string()))
        );
        // no snapshot tag in the output → None (caller surfaces a clear error).
        assert_eq!(parse_loaded_tag("Loaded image: alpine:latest\n"), None);
    }

    /// Proves the PTY pipeline: spawn a real shell, send a command, read its
    /// output back off the channel. (Local backend — no container needed.)
    #[test]
    fn local_shell_pty_roundtrip() {
        let (tx, rx) = mpsc::channel();
        let mut sb =
            Sandbox::launch(Backend::Local, "test", "", 24, 80, tx).expect("launch local shell");
        sb.write_input(b"echo HELLO_PTY_42\n").unwrap();

        let mut acc = String::new();
        let deadline = Instant::now() + Duration::from_secs(4);
        while Instant::now() < deadline {
            if let Ok(chunk) = rx.recv_timeout(Duration::from_millis(200)) {
                acc.push_str(&String::from_utf8_lossy(&chunk));
                if acc.contains("HELLO_PTY_42") {
                    break;
                }
            }
        }
        sb.stop();
        assert!(
            acc.contains("HELLO_PTY_42"),
            "pty output missing marker; got: {acc:?}"
        );
    }

    // --- VT-x holder classification (pure, no live process table) ------------

    #[test]
    fn multipass_instance_parsed_from_qemu_args() {
        let args = "/usr/bin/qemu-system-x86_64 ... -drive file=/var/snap/multipass/common/data/multipassd/vault/instances/example-vm/ubuntu.img ... --enable-kvm";
        assert_eq!(multipass_instance(args).as_deref(), Some("example-vm"));
    }

    #[test]
    fn multipass_instance_none_without_marker() {
        assert_eq!(multipass_instance("qemu-system-x86_64 --enable-kvm"), None);
        // has the path marker but no multipass keyword → not a multipass holder
        assert_eq!(multipass_instance("/instances/foo/x"), None);
    }

    #[test]
    fn classify_ignores_non_kvm_and_non_qemu() {
        // qemu WITHOUT kvm accel (TCG) does not hold VT-x; a random process is ignored.
        let text = "qemu-system-x86_64 -accel tcg disk.img\n/usr/bin/firefox\nbash";
        assert!(classify_vtx_holders(text).is_empty());
    }

    #[test]
    fn classify_collapses_docker_to_single_stoppable_holder() {
        // Docker Desktop spawns its linuxkit qemu (sometimes >1 matching line);
        // we want exactly one "docker" holder, marked stoppable.
        let text = "\
qemu-system-x86_64 --enable-kvm -name docker-desktop ...\n\
/Applications/Docker.app/.docker/qemu --enable-kvm ...";
        let h = classify_vtx_holders(text);
        assert_eq!(h.len(), 1, "docker must collapse to one holder");
        assert_eq!(h[0].kind, "docker");
        assert!(h[0].stoppable);
    }

    #[test]
    fn classify_names_multipass_and_marks_stoppable() {
        let text = "qemu-system-x86_64 --enable-kvm -drive file=/x/multipassd/vault/instances/lab-vm/disk.img";
        let h = classify_vtx_holders(text);
        assert_eq!(h.len(), 1);
        assert_eq!(h[0].kind, "multipass");
        assert_eq!(h[0].instance, "lab-vm");
        assert!(h[0].stoppable);
        assert!(h[0].label.contains("lab-vm"));
    }

    #[test]
    fn classify_unknown_kvm_vm_is_not_stoppable() {
        // A raw qemu+kvm VM we don't recognize: detected, but we refuse to stop
        // it (no clean, reversible restart path) → stoppable == false.
        let text = "qemu-system-x86_64 -enable-kvm -accel kvm -hda mystery.qcow2";
        let h = classify_vtx_holders(text);
        assert_eq!(h.len(), 1);
        assert_eq!(h[0].kind, "unknown");
        assert!(!h[0].stoppable);
    }

    #[test]
    fn classify_recognizes_all_kvm_accel_spellings() {
        for accel in ["--enable-kvm", "-accel kvm", "accel=kvm"] {
            let text = format!("qemu-system-aarch64 {accel} -hda d.img");
            assert_eq!(
                classify_vtx_holders(&text).len(),
                1,
                "missed accel: {accel}"
            );
        }
    }
}

#[cfg(test)]
mod relay_tests {
    use super::*;
    use std::sync::mpsc;
    use std::time::{Duration, Instant};

    /// End-to-end (headless) sandbox relay: launch a local sandbox, run a
    /// command, encode the PTY output exactly as the broker sends it over the
    /// encrypted channel (`{"_sbx":"data","b64":...}`), then decode it back the
    /// way a remote client does and feed it to a vt100 screen — asserting the
    /// command's output lands on the rendered terminal.
    #[test]
    fn sandbox_output_reaches_a_vt100_screen_via_frames() {
        use base64::engine::general_purpose::STANDARD;
        use base64::Engine;

        let (tx, rx) = mpsc::channel();
        let mut sb = Sandbox::launch(Backend::Local, "test", "", 24, 80, tx).expect("launch");
        sb.write_input(b"echo RELAY_MARKER_7\n").unwrap();

        // Remote client side: a vt100 parser fed from decoded data frames.
        let mut screen = vt100::Parser::new(24, 80, 0);
        let deadline = Instant::now() + Duration::from_secs(4);
        let mut hit = false;
        while Instant::now() < deadline {
            if let Ok(chunk) = rx.recv_timeout(Duration::from_millis(200)) {
                // broker: encode → (server relay) → client: decode
                let frame = serde_json::json!({"_sbx":"data","b64": STANDARD.encode(&chunk)});
                let b64 = frame["b64"].as_str().unwrap();
                let decoded = STANDARD.decode(b64).unwrap();
                screen.process(&decoded);
                if screen.screen().contents().contains("RELAY_MARKER_7") {
                    hit = true;
                    break;
                }
            }
        }
        sb.stop();
        assert!(hit, "command output never reached the rendered terminal");
    }
}
