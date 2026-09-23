# hack-house → Podman backend + Goose harness — Spec

> ⚠️ **SUPERSEDED (harness portion only).** The Goose harness described here was
> stripped on 2026-06-08 and replaced by the lightweight, host-side, Ollama-native
> harness — see **`docs/spec-native-harness.md`**. **The Podman backend introduced
> by this spec still stands** (it is not Goose-specific). Read this document only
> for the Podman backend rationale; ignore every Goose section below.

> **Status:** Draft v1 · **Date:** 2026-06-07
> **Scope:** Add **Podman** as an additional sandbox backend (Docker stays), and
> make **Goose** (block/goose) the default agentic harness for the `/ai !task`
> path — strictly contained to spawned `/sbx` sessions, output rendered to chat.
> **Baseline reviewed:** `cmd_chat/agent/bridge.py`, `hh/src/sbx.rs`,
> `hh/src/app.rs`, `hh/src/net.rs` @ `main`.
> **Builds on:** `spec-collaborative-sandbox.md`, `spec-agent-bridge.md`.
> **See also:** `spec-sandbox-distros-gui.md` (Podman default = Kali, Docker
> default = Parrot, the `/sbx <type> <option>` grammar, and the container GUI).
> Grammar note: examples below use `/sbx <backend>` (e.g. `/sbx podman`); the
> older `/sbx launch <backend>` is kept as a transitional alias.

---

## 0. Decisions locked (from product owner)

| # | Decision | Choice |
|---|----------|--------|
| A | Podman vs Docker | **Both.** Podman is an *additional* `Backend`, not a replacement. `/sbx podman` and `/sbx docker` both work. |
| B | Why Podman | Daemonless + rootless ⇒ no root daemon, **no sudo modal**, stronger userns isolation for untrusted shared code. |
| C | Default harness | **Goose** for the `!task` (sandbox-action) path. Opt-out to the legacy one-shot injector (`simple`). |
| D | Goose containment | Goose is **never a host-side process.** It runs only *inside* a spawned `/sbx` session. Container/VM backends fully isolate it; the only host path is `/sbx local` (host shell) — the explicit, warned exception. |
| E | Capability gate | `/grant` is the switch. **Granted** → full Goose shell *inside the opened container*. **Not granted** → advisory, **chat-only**, never touches the sandbox. |
| F | Output | Goose output is rendered to **chat**, not the terminal pane. |
| G | Tools | Goose may use confined tool calls (web search/fetch, approved scripts) — all inside the sandbox namespace. No host fs (no bind mounts). |

---

## 1. Podman backend

### 1.1 Model
`Backend` gains a `Podman` variant alongside `{Local, Docker, Multipass}`. Docker
and Podman share the same OCI CLI grammar (`run/exec/commit/save/rm/images`), so
the two collapse onto one **container code path** parameterised by an engine
binary (`engine_bin(backend) -> "docker" | "podman"`). Only two things differ:

| Concern | Docker | Podman |
|---|---|---|
| Daemon | needs `docker info` up; may need sudo to start | **daemonless** — skip the daemon check + sudo modal entirely |
| Host gateway (for in-container Ollama) | add `--add-host=host.docker.internal:host-gateway` at `run` | `host.containers.internal` is native (rootless pasta/slirp) |

Everything else — provisioning per-member unix accounts, the `sandbox-bootstrap`
toolchain install, `commit`/`save` snapshots, `push` (file bridge), `exec` shell
— is identical and reuses the existing logic via the engine binary.

### 1.2 Snapshots
`podman commit` → `hh-snap:<label>` and `podman save -o …tar` mirror Docker 1:1
(rootless-friendly). `SnapKind` gains a `Podman` arm so `/sbx load <label>`
restores through the engine that holds the image. **CRIU checkpoint/restore is
out of scope** (rootless Podman generally can't); filesystem snapshots via
`commit` remain the save/load mechanism.

### 1.3 Install
`ensure-podman.sh` (apt) + a rootless preflight note (subuid/subgid). No daemon
to start. `/sbx podman install` opts into installing it.

---

## 2. Goose harness

### 2.1 Containment invariant
The bridge already **only ever emits keystroke/chat frames** — it never executes
on the host. Goose preserves this: it runs as a command **inside the spawned
sandbox**, located via the engine + container name the broker advertises. Goose
therefore inherits exactly the backend's isolation:

| Backend | Goose runs in | Host access |
|---|---|---|
| `podman` / `docker` | container namespace (rootless userns ideal) | none — no host fs (no `-v`), separate net ns |
| `multipass` | guest VM | none |
| `local` (`/sbx local`) | host shell | **yes — the one explicit, warned exception** |

### 2.2 Two tiers, gated by `/grant`

```
/ai <agent> !<task>
   ├─ granted (drive ACL)  → run Goose IN the sandbox  → stream stdout → chat
   └─ not granted          → advisory answer only       → chat (no sandbox touch)
```

- **Granted:** the bridge runs, per backend:
  - `podman|docker exec -i <container> goose run -t "<task>" --no-session -q --max-turns N`
  - `multipass exec <name> -- goose run …`
  - `local` → `goose run …` on the host **with a loud warning**
  Goose's full agentic loop (read output → edit files → run approved scripts →
  web search → self-correct) executes inside that sandbox. stdout/stderr is
  captured, throttled, capped, and posted to chat as the agent.
- **Not granted:** the task is answered by the lightweight provider as plain
  chat — explicitly told it has no drive and must advise, not assume execution.

### 2.3 Harness selection & opt-out
- Default harness = `goose`. `/ai start … plain` (or `--harness simple`) selects
  the legacy one-shot injector. `models.toml` may carry `harness = "goose"|"simple"`.
- **Graceful degrade:** if the harness is `goose` but the binary isn't runnable
  in the sandbox, the bridge says so once and falls back to the simple injector —
  Goose is default but never load-bearing. This is also the "remove Goose" path.

### 2.4 Tool surface (confined)
Configured in a **container-side** `~/.config/goose/config.yaml` baked in by
`sandbox-bootstrap.sh` (so the policy travels with the isolation):
- `GOOSE_PROVIDER=ollama`, `GOOSE_MODEL=…`, `OLLAMA_HOST` → host gateway.
- Enabled extensions: developer/shell (scoped to the container), a web
  fetch/search extension, and a curated approved-scripts dir (`/opt/hh/bin`).
- **No host bind mounts**, ever. The only host-ward connection is Goose → host
  Ollama (a model API call, not shell reach).

---

## 3. Wire conventions

The broker advertises where the sandbox lives so the (co-located) agent can run
Goose in it. The existing `_sbx:status` frame gains two fields:

```json
{"_sbx":"status","state":"ready","backend":"podman","engine":"podman","name":"hh-sandbox","rows":40,"cols":120}
```

- `engine` ∈ `{docker, podman, multipass, local}` — the exec family.
- `name` — the container/instance handle (empty for `local`).

The bridge reads these from the frame (extending `_handle_control`) and uses them
to build the `goose run` invocation. Grant state continues to ride the existing
`_perm:acl` frame (`drivers`).

---

## 4. Trust model honesty
- **Containment is hard:** Goose's reach == the sandbox namespace. Container/VM
  backends give real isolation; `local` is host by explicit user choice (warned).
- **Grant tier is app-layer** for the Goose path (the co-located bridge runs the
  exec), the same trust class as the operator ACL in `spec-agent-bridge.md` §3.
  A well-behaved bridge honours `self.granted`; a hostile process with the room
  password is out of scope (it already has the password). The keystroke-injection
  path remains hard-gated by the broker.
- **Prompt injection:** room text is untrusted; the sandbox is the blast radius;
  output is capped + throttled.

---

## 5. Bootstrap touch points
| Script | Change |
|---|---|
| `bootstrap.sh` | add `podman`, `goose` to the optional prereq probe |
| `bootstrap-ai.sh` | install Goose on the host (default on; `--no-goose` to skip); write host `~/.config/goose/config.yaml` |
| `sandbox-bootstrap.sh` | install the Goose binary **into the container** (it's a release binary, not apt — can't ride `sandbox-tools.json`); write the container-side goose config pointing `OLLAMA_HOST` at the host gateway |
| `ensure-podman.sh` | new detect-then-install helper (apt) |

---

## 6. Phasing
| Phase | Scope |
|---|---|
| **1** | Podman backend (launch/exec/provision/teardown/save/load parity); `_sbx:status` carries engine+name. |
| **2** | Goose harness in the bridge: grant-gated two tiers, container exec, stdout→chat, simple-injector fallback; `--harness` flag + `/ai start … plain`. |
| **3** | Bootstrap: host + in-container Goose install, ensure-podman, confined tool config. |
| **4** | Polish: structured Goose event rendering, approved-script allowlist UI, web-egress allowlist. |
