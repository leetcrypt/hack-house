# Welcome to the hack-house VM Library

Hey — glad you're here. Here's what you're walking into and where you can help.

## The Vision

We're building a **shared library of ready-to-run sandboxes** — each one a self-contained environment (a Podman container) pre-loaded with real software, curated datasets, and working tools. Any agent or team member can pull one down and be productive in seconds. Think: "I need a PCAP forensics bench" → pull it → it's running, pre-loaded, documented.

The library currently holds **79 VMs** (77 shareable), almost entirely security-focused: forensics, ML-based detectors, crypto CTF kits, web fuzzing labs, YARA triage benches, threat-intel processors, and more. 74 of those were built autonomously by `hh-loop` (our Claude-spawns-Claude build system), 1 was hand-built by a human, and a few are early prototypes.

## How Sharing Works (Peer-to-Peer)

There's no central cloud registry — sharing is **peer-to-peer over the encrypted hack-house room**. When you `/sbx publish <label>`, your VM gets exported as a portable `.tar` and marked shareable in your local registry (`~/.hh/registry.json`). Then:

- **A teammate asks what you have:** `/sbx catalog @you` → they see your published VMs
- **They grab one:** `/sbx pull @you <label>` → the `.tar` streams over the room's encrypted channel and loads into their local container engine

So the library grows as people build and stay connected. Every published VM has a `share_path` pointing to its `.tar` archive.

## The Trading-Card System (yes, really)

This is the fun part. **Every VM you save becomes a unique collectible card**, scored deterministically by how good it actually is. The card has:

- A **PowerScore (0–1000)** built from five real axes: *completeness* (is it finished?), *reusability* (can someone else actually pull and use it?), *richness* (how well-described, how rare its capabilities), *pedigree* (provenance trail), and *heft* (how much real software/data is baked in beyond the base image)
- A **rarity tier** — Common, Uncommon, Rare, Epic, Legendary — graded on a population curve so scarcity is real (only 8% can be Legendary)
- A procedural **creature name** and **elemental type** derived from its tags (a crypto VM is Ghost-type, a recon VM is Psychic, an exploit VM is Dark)
- A **6-stat block** (HP/Attack/Defense/SpAtk/Speed/SpDef) projected from its real capabilities

Right now the library has **6 Legendaries** (PowerScore 855–860), **12 Epics**, **20 Rares**, and the rest spread across Uncommon/Common. Building deeper, better-documented VMs with real installed tooling earns higher-tier cards. A featherweight stdlib-only script caps out Common. Load it up with genuine software, curated datasets, provenance notes, and a thorough manifest — that's how you mint an Epic.

Run `python -m cmd_chat.cardex` from the repo to see the full card table.

## Where We Left Off

**What's done:**
- The full build pipeline works end-to-end: `hh-loop` provisions a room, spawns Claude operators (planner/builder/tester), they build the VM, verify it, save + publish it, and mint the card
- 79 VMs built, 75 marked `done`, 77 shareable
- The cardex scoring system is live and grading the whole library on a population curve
- P2P catalog/pull works between room members
- 10 curated security briefs ready in `briefs/security-10.toml`

**What's half-done / next:**
- The original target was **50 VMs** — we blew past that (79!), but many are ML detector variants with similar profiles, which the population curve correctly pushes toward Common/Uncommon. The library needs **more diverse, hand-crafted VMs** outside the ML-detector pattern to spread the rarity curve
- The `hh-snapshots/` directory where `.tar` exports should live doesn't exist on disk yet — the registry *references* share paths but the actual tars may have been cleaned up or never fully materialized. Verifying and rebuilding the portable artifact layer is a concrete gap
- No central/persistent VM store exists yet — if two peers aren't online in the same room simultaneously, you can't pull. A durable artifact host (Gitea releases, an OCI registry, or even a shared directory) would make the library truly persistent

## Your First Task

**Verify the artifact layer.** Pick 3–5 VMs from the registry, check whether their `.tar` files actually exist at the `share_path`, and if not, re-export them with `hack-house sbx save` + `sbx publish`. This is the gap between "library exists in the registry" and "library is actually pullable." It's bounded, concrete, and you'll learn the whole save/publish flow by doing it.

Welcome aboard.
