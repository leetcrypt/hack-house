//! Snapshot indexing + publishing — shared between the interactive TUI (`app.rs`,
//! the `/sbx save`/`/sbx publish` commands) and the headless `hack-house sbx`
//! subcommand (`main.rs`, used by the autonomous `/loop` runner). Both paths call
//! these same functions so a VM saved by an operator agent lands a byte-identical
//! registry entry to one saved by a human in the room — no schema drift between
//! the two save/publish surfaces.

use crate::registry;
use crate::sbx;

/// Size in bytes of an OCI image (`<engine> image inspect … --format {{.Size}}`),
/// or None if the engine/image isn't available. Blocking.
pub fn oci_image_size(engine: &str, image: &str) -> Option<u64> {
    let out = std::process::Command::new(engine)
        .args(["image", "inspect", image, "--format", "{{.Size}}"])
        .stderr(std::process::Stdio::null())
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    String::from_utf8_lossy(&out.stdout).trim().parse::<u64>().ok()
}

/// Record a freshly-saved snapshot in the host-global VM registry, caching the
/// `.hh-agent` manifest summary read out of the (still-running, for OCI backends)
/// container. Best-effort — never panics, never fails the save. Blocking; call
/// from inside `spawn_blocking` (TUI) or directly (headless CLI).
pub fn register_saved_snapshot(be: sbx::Backend, name: &str, label: &str, created_by: &str) {
    let (artifact_kind, artifact_ref, size_bytes, manifest) = match be {
        sbx::Backend::Docker | sbx::Backend::Podman => {
            let engine = be.engine();
            let image = format!("{}:{}", sbx::SNAP_REPO, label);
            let size = oci_image_size(engine, &image);
            let manifest = registry::read_container_manifest(engine, name, "/root");
            ("image".to_string(), image, size, manifest)
        }
        // Multipass snapshots live in multipass's own store; the instance is
        // powered off by save time, so there's no container to read a manifest
        // from. Record the pointer; reconcile leaves it (no image to probe).
        sbx::Backend::Multipass => ("snapshot".to_string(), label.to_string(), None, None),
        sbx::Backend::Local => return, // nothing persistent to index
    };
    let (purpose, status, todo) = manifest
        .as_deref()
        .map(registry::scan_manifest)
        .unwrap_or_default();
    let entry = registry::Entry {
        label: label.to_string(),
        backend: be.engine().to_string(),
        artifact_kind,
        artifact_ref,
        size_bytes,
        created_unix: registry::now_unix(),
        created_by: created_by.to_string(),
        repo: registry::cwd_repo(),
        purpose,
        status,
        todo,
        ..Default::default()
    };
    if let Err(e) = registry::upsert(entry) {
        eprintln!("registry upsert failed for '{label}': {e}");
    }
}

/// Mark a saved snapshot shareable and ensure it has a portable, sendable file.
/// File-backed snaps (`.tar`/`.ova`) are already tradeable; image-backed snaps
/// are exported to `hh-snapshots/hh-snap-<label>.tar` first so a peer can receive
/// them over `/send`. Blocking — run off the UI thread. Returns a status line.
pub fn publish_snapshot(label: &str, tags: &[String]) -> anyhow::Result<String> {
    use anyhow::Context;
    let entry = registry::get(label)
        .with_context(|| format!("no saved VM labelled '{label}' — `/sbx browse` to list"))?;
    let share_path = match entry.artifact_kind.as_str() {
        // Already a standalone file the owner controls — send it as-is.
        "file" => entry.artifact_ref.clone(),
        // Lives only in the engine image store — export a portable copy.
        "image" => {
            let backend = sbx::Backend::parse(&entry.backend)
                .with_context(|| format!("unknown backend '{}' for '{label}'", entry.backend))?;
            sbx::export_image(backend, label)?
                .to_string_lossy()
                .into_owned()
        }
        other => anyhow::bail!(
            "'{label}' is a {other} snapshot — only file/image snapshots are tradeable"
        ),
    };
    let published = registry::publish(label, &share_path, tags)?;
    let tagnote = if published.tags.is_empty() {
        String::new()
    } else {
        format!(" [{}]", published.tags.join(", "))
    };
    Ok(format!(
        "✓ published '{label}'{tagnote} — shareable artifact at {share_path}; peers can `/sbx pull @<you> {label}`"
    ))
}
