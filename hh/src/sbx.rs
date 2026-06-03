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

/// Helper that ensures the Docker daemon is running (ships beside this source).
const ENSURE_DOCKER: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/ensure-docker.sh");
/// Detect-first VirtualBox installer (ships beside this source).
const ENSURE_VBOX: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/ensure-vbox.sh");

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
/// Returns a short human description of what was written on success.
pub fn save_state(backend: Backend, name: &str, label: &str) -> Result<String> {
    match backend {
        Backend::Docker => {
            let tag = format!("{SNAP_REPO}:{label}");
            let out = Command::new("docker")
                .args(["commit", name, &tag])
                .output()
                .context("docker commit (is docker installed?)")?;
            if !out.status.success() {
                let err = String::from_utf8_lossy(&out.stderr);
                anyhow::bail!("docker commit failed: {}", err.lines().last().unwrap_or("").trim());
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
                anyhow::bail!("multipass snapshot failed: {}", err.lines().last().unwrap_or("").trim());
            }
            Ok(format!("snapshot {name}.{label}"))
        }
        Backend::Local => {
            anyhow::bail!("the local shell has no VM state to save — launch a docker or multipass sandbox first")
        }
    }
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
