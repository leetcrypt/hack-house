# hack-house → Native lightweight harness (strip Goose) — Spec

> **Status:** Draft v1 · **Date:** 2026-06-08
> **Scope:** Replace the **Goose** agentic harness on the `/ai <name> !<task>`
> path with a **lightweight, host-side, Ollama-native harness**, and **strip all
> Goose integration** from the codebase + bootstrap scripts.
> **Baseline reviewed:** `cmd_chat/agent/bridge.py`, `cmd_chat/agent/__main__.py`,
> `cmd_chat/agent/providers.py`, `hh/src/sbx.rs`, `hh/src/app.rs`,
> `hh/scripts/bootstrap.sh`, `hh/scripts/bootstrap-ai.sh`,
> `hh/scripts/sandbox-bootstrap.sh` @ `main` (f93c8c5).
> **Supersedes:** `docs/spec-goose-harness.md` (Goose harness portion only — the
> Podman backend it also introduced **stays**).
> **Related (do NOT touch):** `headroom/` references Goose for an unrelated CLI
> wrapper experiment; out of scope here.

---

## 0. Why

| Problem with Goose | Consequence on this project |
|---|---|
| Agentic loop = **N sequential model calls** (`--max-turns 15`) | On a CPU-only box (i5-8350U, no GPU) each call is already slow; Goose multiplies per-call latency by N → "takes forever". |
| Heavyweight external **Rust binary** baked into every sandbox | Extra install in `sandbox-bootstrap.sh` + host install in `bootstrap-ai.sh`; a moving release dependency. |
| In-container Goose must reach **host Ollama** | The documented **slirp4netns loopback bug**: rootless Podman's `host.containers.internal` resolves to the host LAN IP and can't reach a `127.0.0.1`-bound Ollama. Forced the `--network=slirp4netns:allow_host_loopback=true` + `OLLAMA_HOST=10.0.2.2` workaround in `sbx.rs`. |
| Opaque: we relay raw stdout, no control of the tool schema | Hard to bound, hard to render, hard to reason about. |

**What we already have that works decently** (commit `47019dd`, still alive today
as `_run_simple`): a **one-shot keystroke injector** — *one* model call → a
```` ```sh ```` block → typed into the shared sandbox PTY via the existing
`_sbx:input` frames, guarded by a destructive-command regex + blast-radius caps,
echoed to the room first as an audit trail. It is fast (single call), needs **no
external binary**, and reuses transport that already exists. Its only real
limitation: it types **blind** — it never reads command output back.

**Decision:** keep the proven injector as the floor, optionally add a **bounded,
host-side, Ollama-native tool-calling loop** as the ceiling, and remove Goose
entirely.

---

## 1. Native harness design

### 1.1 Key architectural shift: host-side model, container-side execution

Goose ran the *whole* loop **inside** the sandbox (model calls + command
execution), which is why the container needed to reach host Ollama. The native
harness splits these:

- **Model call → host.** The bridge (co-located with the broker, already on the
  host) calls Ollama on the host directly via `providers.py`. No container→host
  Ollama hop.
- **Command execution → sandbox.** Commands run in the sandbox via the engine the
  broker advertises (`_sbx:status` → `engine` + `name`), using the same
  `<engine> exec <name> …` plumbing Goose used for its probe.

**Consequence:** the entire in-container Ollama gateway surface
(`--add-host=host.docker.internal:host-gateway`, `--network=slirp4netns:allow_host_loopback=true`,
in-container `OLLAMA_HOST`) becomes **dead** and is removed — **the slirp4netns
loopback bug ceases to exist.**

### 1.2 Two harness levels (both host-side model, grant-gated)

The grant tiers from the Goose spec are unchanged (`/grant` is the switch):
**not granted → advisory chat-only**; **granted → act in the sandbox**. What
changes is *how* the granted path acts:

| Level | What it does | Reads output? | Model calls | Default |
|---|---|---|---|---|
| **`simple`** | One model call → ```` ```sh ```` block → type into the shared PTY (`_sbx:input`). The proven `47019dd` injector. | No (blind) | 1 | fallback |
| **`native`** | Bounded tool-calling loop: Ollama `/api/chat` with a small `tools` schema; bridge execs each tool call in the sandbox, captures stdout, feeds it back; cap at **3–5 turns**. | Yes | ≤ turns | **default** |

`native` degrades to `simple` automatically when the model doesn't advertise tool
support (so it is default but never load-bearing — same safety property Goose
had, minus the binary).

### 1.3 The `native` loop (Ollama-native function calling)

`qwen2.5` (our default) supports function calling through Ollama's `/api/chat`
`tools` field — we just aren't using it yet (`providers.py:104/119` send no
`tools`). Add an opt-in tool-calling completion to `OllamaProvider`:

```
POST /api/chat
{ "model": …, "messages": [...], "tools": [ <schema below> ], "stream": false }
→ message.tool_calls: [ {function:{name, arguments}}, … ]
```

**Minimal tool schema** (the whole surface — deliberately tiny):

| Tool | Args | Maps to (sandbox exec) |
|---|---|---|
| `run_shell` | `command: string` | `<engine> exec <name> sh -c "<command>"`, capture stdout+stderr+rc |
| `write_file` | `path: string, content: string` | heredoc write via exec (no host fs) |
| `read_file`  | `path: string` | `cat` via exec |

Loop (per `!task`, granted):
1. Seed messages with `SANDBOX_SYSTEM` + the task + recent transcript window.
2. Call Ollama with `tools`. If `tool_calls` present → run each through the
   **same guards** (`DESTRUCTIVE` regex + `/confirm`, `MAX_COMMANDS`/`MAX_BYTES`),
   exec in the sandbox, append a `tool` role message with captured output.
3. Repeat until the model returns a plain answer or the **turn cap** (3–5) is hit.
4. Stream progress to chat (reuse `_send_stream`); post the final line; append a
   capped summary to the transcript.

**Containment unchanged from the Goose spec §2.1:** execution reach == the
sandbox namespace. Container/VM backends isolate it; `local` is the host by
explicit, warned choice. Output is byte-capped + throttled. Room text stays
untrusted; the sandbox is the blast radius.

### 1.4 Why this fixes "takes forever"

- **We own the turn budget** (3–5, not 15) → bounded worst case.
- **Model runs host-side** with the CPU-tuned `OllamaProvider` knobs already in
  place (`num_ctx`/`num_thread`/`num_predict`, plus the `qwen2.5-coder` code path).
- A trivial task that the model nails in one tool call costs ~1 round-trip — same
  ballpark as the old one-shot injector.

---

## 2. Nomenclature

Drop the Goose-implicit grammar (where `plain` *disabled* Goose and there was no
positive keyword). New, symmetric harness selector:

```
/ai start [profile|model] [native|simple] [allow]
```

- **harness word** (optional): `native` (default) or `simple`. `plain` kept as a
  back-compat alias for `simple`.
- **`allow`** (unchanged): pre-grant sandbox drive on spawn (owner only).
- CLI: `--harness {native,simple}` on `python -m cmd_chat.agent`
  (replaces `{goose,simple}`); drop `--goose-max-turns`, add
  `--max-turns` (default 5) for the `native` loop.
- `models.toml`: `harness = "native"|"simple"` (was `"goose"|"simple"`).

---

## 3. Strip plan — file by file

> `headroom/**` is a different project; **excluded**.

| File | Change |
|---|---|
| `cmd_chat/agent/bridge.py` | Remove `_run_goose`, `_goose_argv`, `_goose_present`, `_goose_present_cache`, and the `GOOSE_*` consts. Rewrite `_run_in_sandbox`: granted → `native` loop (new `_run_native`) with `simple` fallback; not granted → `_advise` (unchanged). Default `self.harness = "native"`. Keep `_run_simple`, `_advise`, `_inject`, `_extract_commands`, `_confirm_pending`, the destructive gate, and blast caps. Keep `_handle_control` reading `_sbx:status` `engine`+`name` (the `native` exec path uses them). |
| `cmd_chat/agent/providers.py` | Add tool-calling completion to `OllamaProvider` (`complete_with_tools(system, messages, tools) -> (text, tool_calls)`); add a capability probe (does the model accept `tools`). Chat/`stream` paths unchanged. |
| `cmd_chat/agent/__main__.py` | `--harness {native,simple}` (was `{goose,simple}`); remove `--goose-max-turns` + `GOOSE_MAX_TURNS` import; add `--max-turns`. Default harness `native`. |
| `hh/src/app.rs` | Update the `/ai start` parser (≈2628–2651): accept `native|simple` (+ `plain` alias) instead of just `plain`; pass `--harness native|simple`. Refresh the comment at 3248. |
| `hh/src/sbx.rs` | Remove the in-container Ollama gateway: the `--add-host=host.docker.internal:host-gateway` / `--network=slirp4netns:allow_host_loopback=true` block (≈780–792) and the in-container `OLLAMA_HOST` set in `dk_bootstrap` (≈1249). Drop the Goose-reaches-host comments (688, 780, 1249). **Verify** nothing else depends on the gateway before deleting. |
| `hh/scripts/bootstrap.sh` | Remove `goose` from the prereq probe loop (line 52). |
| `hh/scripts/bootstrap-ai.sh` | Remove the entire Goose install block + `--no-goose` flag + `GOOSE_INSTALLER_URL` + host `~/.config/goose/config.yaml` writer + the `goose_bin()` helper and its status lines. Update header/usage. |
| `hh/scripts/sandbox-bootstrap.sh` | Remove the Goose section (≈53–80): binary install + container-side `~/.config/goose/config.yaml`. (Mirrors the noVNC strip already done.) |
| `models.toml` | `harness` values doc: `goose` → `native`. |
| `docs/spec-goose-harness.md` | Add a banner: **Harness section superseded by `spec-native-harness.md`; the Podman backend portion still stands.** |

**Keep (not Goose-specific):** Podman backend, `_sbx:status` `engine`+`name`
fields (the `native` exec path needs them), the grant/ACL gate, `_advise`,
`_run_simple`, destructive gate + blast caps, CPU tuning + `qwen2.5-coder` path.

---

## 4. Phasing

| Phase | Scope |
|---|---|
| **0 (done)** | Fix the agent disconnect bug (reconnect loop + per-frame shield in `bridge.py`). |
| **1** | Strip Goose: `bridge.py`/`__main__.py` harness plumbing, `app.rs`/`sbx.rs`, bootstrap scripts, `models.toml`, supersede banner. Default falls back to `simple` until Phase 2 lands. `cargo check` + `py_compile` clean. |
| **2** | Implement the `native` tool-calling loop: `OllamaProvider.complete_with_tools` + tool probe; `_run_native` with the 3-tool schema, turn cap, guards, exec-capture, stream→chat, `simple` fallback. |
| **3** | Bench `native` vs the old `simple`/Goose on this box (latency, success on a few canonical `!task`s); tune turn cap + tool schema; update help/usage + memory. |

---

## 5. Open questions

1. **Tool schema breadth:** start with `run_shell` only (closest to the proven
   injector, simplest), or ship `write_file`/`read_file` from the start?
2. **Turn cap default:** 3 (snappy) vs 5 (more self-correction) — decide after the
   Phase 3 bench.
3. **`native` for non-Ollama providers** (Anthropic/OpenAI also do tool calls):
   in scope now, or Ollama-only first and treat cloud as a later generalization?
4. **`simple` fate:** keep indefinitely as the zero-dependency fallback (recommended),
   or retire once `native` is proven?
