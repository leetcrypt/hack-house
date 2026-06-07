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
/// Detect-first VirtualBox installer (ships in hh/scripts/).
const ENSURE_VBOX: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/scripts/ensure-vbox.sh");

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

/// Start the Docker daemon via `ensure-docker.sh --yes`, waiting until it's
/// ready. Returns the script's last error line on failure (e.g. needs sudo).
fn start_docker_daemon() -> Result<()> {
    let out = Command::new("bash")
        .arg(ENSURE_DOCKER)
        .arg("--yes")
        .output()
        .context("running ensure-docker.sh")?;
    if !out.status.success() {
        let err = String::from_utf8_lossy(&out.stderr);
        let last = err
            .lines()
            .last()
            .unwrap_or("could not start the docker daemon");
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
/// Returns the script's last error line on failure (e.g. needs sudo).
pub fn ensure_vbox_install() -> Result<()> {
    let out = Command::new("bash")
        .arg(ENSURE_VBOX)
        .arg("--yes")
        .output()
        .context("running ensure-vbox.sh")?;
    if !out.status.success() {
        let err = String::from_utf8_lossy(&out.stderr);
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

/// A running hypervisor that holds the CPU's VT-x/VMX root mode. While one is
/// live, VirtualBox can't boot a *hardware* VM (it aborts with
/// `VERR_VMX_IN_VMX_ROOT_MODE`). We only mark holders we know how to stop
/// cleanly (`stoppable`); an unrecognised qemu/kvm process is reported but never
/// killed — the caller aborts and lets the user deal with it.
#[derive(Clone, Debug)]
pub struct VtxHolder {
    /// Human label, e.g. "Docker Desktop" or "multipass VM 'molt-mania-vm'".
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
/// Docker = fast, Local = no isolation (dev/testing only).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Backend {
    Local,
    Docker,
    Multipass,
}

impl Backend {
    pub fn parse(s: &str) -> Option<Backend> {
        match s {
            "local" => Some(Backend::Local),
            "docker" => Some(Backend::Docker),
            "multipass" => Some(Backend::Multipass),
            _ => None,
        }
    }
    pub fn label(self) -> &'static str {
        match self {
            Backend::Local => "local-shell",
            Backend::Docker => "docker",
            Backend::Multipass => "multipass",
        }
    }
    /// Default image/release when the user doesn't specify one.
    pub fn default_image(self) -> &'static str {
        match self {
            Backend::Multipass => "24.04",
            Backend::Docker => "ubuntu:24.04",
            Backend::Local => "",
        }
    }
}

/// One-time setup before the PTY shell is spawned. Blocking — run off the UI
/// thread (Multipass boots a real VM, ~20-30s). Idempotent: reuses an instance
/// that already exists.
pub fn prepare(backend: Backend, name: &str, image: &str, start_daemon: bool) -> Result<()> {
    match backend {
        Backend::Local => Ok(()),
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
        Backend::Docker => {
            // The daemon must be up before any `docker` call. Rather than fail
            // with a raw connection error, start it (the caller confirmed via
            // `/sbx launch docker --start`).
            if !docker_daemon_up() {
                if start_daemon {
                    start_docker_daemon().context("starting docker daemon")?;
                } else {
                    anyhow::bail!(
                        "docker daemon is not running — retry with `/sbx launch docker --start`"
                    );
                }
            }
            // Persistent container so we can exec in to provision users + shells.
            let _ = Command::new("docker")
                .args(["rm", "-f", name])
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .status();
            // Capture output so a failure can't paint over the TUI; the reason is
            // surfaced through the returned error (shown in the error popup).
            let out = Command::new("docker")
                .args([
                    "run",
                    "-d",
                    "--name",
                    name,
                    "--hostname",
                    name,
                    "-w",
                    "/root",
                    image,
                    "sleep",
                    "infinity",
                ])
                .output()
                .context("docker run (is docker installed?)")?;
            if !out.status.success() {
                let err = String::from_utf8_lossy(&out.stderr);
                anyhow::bail!(
                    "docker run failed: {}",
                    err.lines().last().unwrap_or("").trim()
                );
            }
            Ok(())
        }
    }
}

/// Destroy ephemeral resources after stop. Multipass instance is purged;
/// the Docker container is removed; Local is a no-op.
pub fn teardown(backend: Backend, name: &str) {
    match backend {
        Backend::Multipass => {
            let _ = Command::new("multipass")
                .args(["delete", name, "--purge"])
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .status();
        }
        Backend::Docker => {
            let _ = Command::new("docker")
                .args(["rm", "-f", name])
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .status();
        }
        Backend::Local => {}
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
        Backend::Docker => {
            let tag = format!("{SNAP_REPO}:{label}");
            let out = Command::new("docker")
                .args(["commit", name, &tag])
                .output()
                .context("docker commit (is docker installed?)")?;
            if !out.status.success() {
                let err = String::from_utf8_lossy(&out.stderr);
                anyhow::bail!(
                    "docker commit failed: {}",
                    err.lines().last().unwrap_or("").trim()
                );
            }
            if local {
                let path = snap_dir()?.join(format!("hh-snap-{label}.tar"));
                let out = Command::new("docker")
                    .args(["save", &tag, "-o"])
                    .arg(&path)
                    .output()
                    .context("docker save")?;
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
    }
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
        Backend::Docker => {
            let out = Command::new("docker")
                .args(["images", SNAP_REPO, "--format", "{{.Tag}}"])
                .output()
                .context("docker images")?;
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
    }
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
        Backend::Docker => {
            let user = if run_user.is_empty() {
                "root"
            } else {
                run_user
            };
            let mut c = CommandBuilder::new("docker");
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
fn dk(name: &str, args: &[&str]) {
    let mut a = vec!["exec", name];
    a.extend_from_slice(args);
    let _ = Command::new("docker")
        .args(a)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status();
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
            run
        }
        Backend::Docker => {
            // Refresh the apt index once so `apt-get install <pkg>` just works —
            // base images ship without /var/lib/apt/lists, so installs otherwise
            // fail with "Unable to locate package" until the user runs update.
            dk(name, &["apt-get", "update"]);
            for m in members {
                let u = unix_name(m);
                if !u.is_empty() {
                    dk(name, &["useradd", "-m", "-s", "/bin/bash", &u]);
                }
            }
            // Base images usually lack the sudo package; the shared shell runs as
            // root (superuser) and drive-grant is the delegation mechanism.
            "root".to_string()
        }
        Backend::Local => String::new(),
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
        Backend::Docker => "root".to_string(),
        Backend::Local => String::new(),
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
        Backend::Docker => {
            // Shell runs as root with $HOME=/root (see `prepare`'s `-w /root`).
            let mut c = Command::new("docker");
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
        let args = "/usr/bin/qemu-system-x86_64 ... -drive file=/var/snap/multipass/common/data/multipassd/vault/instances/molt-mania-vm/ubuntu.img ... --enable-kvm";
        assert_eq!(multipass_instance(args).as_deref(), Some("molt-mania-vm"));
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
