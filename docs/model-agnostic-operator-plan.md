# Model-Agnostic Operator — Implementation Plan

**Status:** Draft / proposed
**Author:** drafted with Claude (operator session)
**Date:** 2026-06-28
**Scope:** Make *any* AI model able to connect as a hack-house operator (drive a
room + shared sandbox, hand off work, spawn nested operators) — not just Claude
Code. Unify the two divergent AI integration paths under one Provider core, and
lift role/coordination context out of bespoke shell scripts into reusable presets.

---

## 1. Motivation — the asymmetry we are closing

hack-house has **two independent AI integration paths** with opposite degrees of
model-agnosticism:

| | `cmd_chat/agent/` — the `/ai` chat agent | `cmd_chat/operator/` — the operator bridge / spawn |
|---|---|---|
| Model coupling | **Fully pluggable** | **Hardcoded to Claude Code** |
| Mechanism | `providers.py` `Provider` Protocol + `module:Class` plugins; `profiles.py` `models.toml` presets | `bootstrap.build_run_argv` → `["claude","-p",directive]` |
| Tool calling | `complete_with_tools` + capability probe + native harness loop (`agent/bridge.py` `NATIVE_TOOLS`) | delegated to Claude Code's own loop |
| Token accounting | real `prompt_eval_count`/`eval_count`, budgeted against true tokens | none (the operator *is* the model's own session) |
| Install / creds | n/a (HTTP) | installs `@anthropic-ai/claude-code`, carries `~/.claude/.credentials.json` |

The brain that **drives the room + sandbox and spawns nested operators is
Claude-only**, while the **in-room chat brain is already any-model**. We already
own every primitive needed to close the gap: `providers.py`, `profiles.py`,
`bootstrap.Budget`, the native harness loop, the `.hh-agent` manifest, and the
`watch` stop-condition engine. This plan wires them together.

### Goals
- Any function-calling model (Ollama-local, Groq, GPT-4o, vLLM, Anthropic API)
  can act as a first-class operator with **no CLI install and no creds carry**.
- `spawn` is generalized beyond `claude -p` to a small **runner registry**
  (claude / codex / gemini / generic) while keeping the existing budget + gated
  creds machinery.
- One **portable capabilities prompt** (Layer-1) is the single source of truth,
  delivered by-skill to Claude and by-injection to everyone else.
- Role behaviours (planner/builder/tester) become **declarative presets**, not
  copy-pasted shell scripts.

### Non-goals
- Not replacing Claude Code subprocess operators — they stay as one runner kind.
- Not building a new agent framework; we borrow patterns (LiteLLM/Continue for
  providers, Swarm/CrewAI for handoffs+roles) but keep the room-as-bus +
  shared-VM-as-workspace model that already differentiates hack-house.
- No secrets in any committed file (presets name `api_key_env`, never the key).

---

## 2. Target architecture

An operator = **(a brain) + (the hack-house toolset) + (a read→think→act loop)**.
The brain is supplied two ways; both are first-class:

```
                       ┌──────────────────────────────┐
                       │   cmd_chat/ai/ (Provider core)│  ← hoisted, shared
                       │   providers.py · profiles.py  │
                       └───────────────┬──────────────┘
                  ┌────────────────────┼─────────────────────┐
                  ▼                    ▼                     ▼
        /ai chat agent        HARNESS-MODE OPERATOR     CLI-SUBPROCESS OPERATOR
        (agent/bridge.py)     (native loop + operator    (bootstrap runner
                               toolset; any Provider)     registry: claude|codex|
                                                          gemini|cmd)
                  └──────────── operator bridge daemon (operator/bridge.py) ──────┘
                          verbs: say keys write exec get screen watch manifest spawn
```

Context is layered (one minimal prompt, behaviour overlaid):

- **Layer 1 — Capabilities** (`CAPABILITIES.md` + tool schema): room model,
  read→think→act loop, verb table, `/grant` drive model, stop-vocabulary,
  `.hh-agent` manifest. ~1 screen. Single source; two delivery channels.
- **Layer 2 — Role presets** (`roles/*.toml`): objective frame, tool bias,
  budget, stop conditions, review-loop gates. planner / builder / tester / …
- **Layer 3 — Coordination protocol**: blackboard = manifest + room events;
  gates = `watch --for … --in events` (never sleeps); recursion = `Budget` +
  (new) per-agent token ceiling.

---

## 3. Phased plan

Phases are ordered by dependency and sized to be independently reviewable.
Each lists **what**, **files**, **design notes**, **acceptance**, **risk**.

### Phase 1 — Hoist the Provider core to `cmd_chat/ai/`
**What:** Move `agent/providers.py` + `agent/profiles.py` to a shared
`cmd_chat/ai/` so operator and `/ai` agent consume one core.

**Files:**
- New: `cmd_chat/ai/__init__.py`, `cmd_chat/ai/providers.py`, `cmd_chat/ai/profiles.py`
- Shim: `cmd_chat/agent/providers.py`, `cmd_chat/agent/profiles.py` → re-export
  from `cmd_chat.ai` (`from cmd_chat.ai.providers import *`) so existing imports
  and the native-harness bench keep working unchanged.
- Update `agent/bridge.py`, `agent/__main__.py` imports to `cmd_chat.ai`.

**Design notes:** Pure refactor — **no behaviour change**. The re-export shims
make it backward-compatible and let the move land before any consumer is
rewritten. `OllamaEmbedder` moves too (it is provider-adjacent).

**Acceptance:** `pytest` green; native-harness bench unchanged; `/ai start
qwen2.5:3b` still answers in a room.

**Risk:** Low. Import churn only. Mitigated by shims.

---

### Phase 2 — Harness-mode operator (the real agnostic win)
**What:** A new operator brain that runs the native tool-calling loop (from
`agent/bridge.py`) but with the **operator toolset** instead of the chat
`NATIVE_TOOLS`, driving the operator bridge daemon.

**Files:**
- New: `cmd_chat/operator/harness.py` — the loop adapter.
- Reuse: the loop body around `agent/bridge.py:1094-1209` (`complete_with_tools`,
  token estimate, transcript trim) — extract the model-agnostic loop into
  `cmd_chat/ai/harness.py` so both chat and operator share it; chat passes its
  `NATIVE_TOOLS`, operator passes `OPERATOR_TOOLS`.
- New tool schema `OPERATOR_TOOLS`: `say`, `keys`, `write_file`, `exec`,
  `get_file`, `screen`, `watch`, `manifest`, `spawn` — each mapping to the
  operator bridge verb it already implements (`operator/bridge.py`).
- Wire `operator/__main__.py`: a new verb (e.g. `operate --provider … --model …
  --role …`) that joins via the bridge and runs the harness loop.

**Design notes:**
- Tool handlers call the existing operator bridge unix-socket verbs — no new
  side-effect surface, just a new *driver*.
- `ToolsUnsupported` already exists; if a model can't function-call, degrade to
  a constrained text-protocol or refuse with a clear message (don't silently
  fall through to chat-style prose).
- Per-agent **token ceiling** is enforceable here (we own the loop and
  `complete_with_tools` returns real usage) — add it as a stop condition.
- Grant/drive: harness operator must respect `/grant` exactly like the CLI path
  (no driving the PTY before granted).

**Acceptance:** an Ollama model (qwen2.5:3b) joins a room as `operate`, is
granted drive, writes a file + runs a command + reports in-room — verified by a
tmux-demo scenario mirroring the vmhub flow but with a local model as the builder.

**Risk:** Medium. Weak local models leak tool calls as text — the existing
`_extract_text_tool_calls` recovery in `providers.py` already mitigates this and
should be reused verbatim. CPU latency is real; keep `num_ctx` modest.

---

### Phase 3 — Runner registry in `bootstrap`
**What:** Generalize the three Claude-hardcoded spots into a registry keyed by
runner kind.

**Files:** `cmd_chat/operator/bootstrap.py`
- `build_run_argv` (currently `["claude","-p",directive]` ~L203) → look up an
  argv template by runner.
- `install_plan` (`CLAUDE_NPM_PACKAGE` ~L35/128) → per-runner install spec.
- `creds_source` (`~/.claude/.credentials.json` ~L148) + `child_env`
  (`CLAUDE_CONFIG_DIR`) → per-runner creds path + config-dir env var.

**Design (sketch):**
```python
RUNNERS = {
  "claude": Runner(argv=lambda d: ["claude","-p",d,"--dangerously-skip-permissions"],
                   install=["npm","i","-g","@anthropic-ai/claude-code"],
                   creds="~/.claude/.credentials.json", config_env="CLAUDE_CONFIG_DIR"),
  "codex":  Runner(argv=lambda d: ["codex","exec",d], creds="~/.codex/auth.json",
                   config_env="CODEX_HOME", install=[...]),
  "gemini": Runner(argv=lambda d: ["gemini","-p",d,"--yolo"], ...),
  "cmd":    Runner(argv=template_from("$HH_OPERATOR_CMD"), ...),  # generic
}
```
- `spawn` gains `--runner {claude|codex|gemini|cmd}` (default `claude`,
  back-compat).
- Keep `Budget`, `plan_creds` (gated), `detect_system`, `install_plan` structure
  intact — they are already model-neutral.

**Design notes:** Runner argv templates are the ONLY model-specific knowledge;
everything else (budget, gating, detection) stays shared. Generic `cmd` runner
covers aider/cursor-agent/opencode without first-class entries.

**Acceptance:** `spawn --runner cmd` with a stub script launches and joins;
`--runner claude` is byte-identical to today. Pure unit tests (no side effects)
cover argv/install/creds selection per runner.

**Risk:** Low–Medium. Non-Claude CLIs vary in flags; the generic `cmd` runner is
the safety valve. Only `claude` is validated end-to-end initially.

---

### Phase 4 — Portable `CAPABILITIES.md` + tool schema (Layer-1)
**What:** Extract the minimal-but-complete capabilities prompt into one portable
artifact; deliver it two ways.

**Files:**
- New: `cmd_chat/operator/CAPABILITIES.md` (prose, ~1 screen) + a machine
  schema (reuse `OPERATOR_TOOLS` from Phase 2 as the canonical tool list).
- `~/.claude/skills/hh-operator/SKILL.md` → reference/include CAPABILITIES.md
  (single source of truth) rather than duplicating doctrine.
- Phase-2 harness injects CAPABILITIES.md as the system prompt for non-Claude
  operators; `compose_directive` (for CLI runners) points at it too.

**Design notes:** Keep it minimal — verbs, the read→think→act loop, grant/drive,
stop-vocabulary, manifest. No Claude-isms (no "use the skill" for injected
models; that line stays only in the Claude delivery path).

**Acceptance:** Claude operator behaviour unchanged (skill still works); a
non-Claude harness operator behaves correctly with only the injected
CAPABILITIES.md as system context.

**Risk:** Low. Mostly docs + a delivery switch.

---

### Phase 5 — `roles/` preset library (Layer-2)
**What:** Replace the demo's bespoke `vmhub-{builder,tester}-task.sh` behaviour
with declarative role presets.

**Files:**
- New: `cmd_chat/operator/roles/{planner,builder,tester,reviewer}.toml`
- Loader mirroring `profiles.py` lookup order (`$HH_ROLES_DIR`, `./roles/`,
  `~/.config/hh/roles/`).
- `spawn`/`operate` gain `--role <name>` → compose Layer-1 + role overlay + join.

**Role schema (sketch):**
```toml
[tester]
objective_frame = "verify the work; report GREEN/RED in-room; add regressions on review"
tool_bias       = ["write_file","keys","watch","manifest"]
budget          = { depth = 0, fanout = 0 }
stop            = ["suite green + planner signed off","owner says stop"]
review_gate     = { watch_for = "pull-count regression", channel = "events" }
```

**Design notes:** Roles are thin overlays on Layer-1, not full prompts. The
review-loop **event-gating** (watch-for-event, never fixed sleeps) is encoded
here — the hard-won lesson from the vmhub reel becomes a reusable primitive.

**Acceptance:** the vmhub three-Claude flow is reproducible from
`spawn --role builder` / `--role tester` with no task-specific shell scripts;
demo scenario updated to use roles.

**Risk:** Low–Medium. Behaviour parity with the existing scripts must be proven
by re-running the vmhub capture.

---

### Phase 6 — MCP server exposing the verbs (stretch, biggest reach)
**What:** Publish the operator toolset as an MCP server so any MCP-capable client
(Claude, Cursor, Cline, Continue, Agents-SDK app) is an operator with zero
hack-house-specific glue.

**Files:** New `cmd_chat/operator/mcp_server.py` exposing `hh.say`, `hh.keys`,
`hh.write`, `hh.exec`, `hh.get`, `hh.screen`, `hh.watch`, `hh.manifest`,
`hh.spawn` over the MCP stdio/SSE transport, backed by the operator bridge.

**Design notes:** Complementary to Phases 2–3, not a replacement: harness-mode
serves raw-HTTP models that aren't MCP clients; MCP serves the rest for free.
Same bridge verbs underneath, so no new side-effect surface.

**Acceptance:** an MCP client lists + calls the `hh.*` tools and drives a room.

**Risk:** Medium. New transport + protocol surface; gate behind explicit enable.
Defer until 1–5 land.

---

## 4. Dependency graph & sequencing

```
P1 (hoist) ──► P2 (harness operator) ──► P5 (roles)
   │                  ▲                     ▲
   │                  │                     │
   └──► P4 (CAPABILITIES + schema) ─────────┘
P3 (runner registry) ── independent of P1/P2; pairs with P5 for spawn --role --runner
P6 (MCP) ── independent stretch; after P1–P5 are stable
```

Recommended merge order: **P1 → P3 → P4 → P2 → P5 → P6.** P3 and P4 are small,
low-risk, independently valuable, and can land early to de-risk the big P2.

---

## 5. Git considerations

**Branching.** Cut a dedicated feature branch from `main`:
`feat/model-agnostic-operator`. The current working branch
(`feat/native-harness-working-memory`) carries unrelated in-flight work; do not
pile this on top of it.

> ⚠️ **Concurrent-session caveat (known):** multiple Claude Code sessions run
> against this single checkout, so commits land on whatever branch is *currently
> checked out*, regardless of which session authored them. Before starting work
> on this plan, confirm the intended branch is checked out, and avoid running
> parallel sessions that commit to different logical features at the same time.

**One PR per phase.** Each phase is sized to be independently reviewable and
(except P2→P5) independently mergeable:
- **P1** is a pure refactor — land it first behind re-export shims so nothing
  else breaks; review is "imports moved, tests green, zero behaviour delta."
- **P3** and **P4** are small and additive; safe to land in parallel with P1.
- **P2** is the large one; keep it focused (loop extraction + operator toolset +
  one `operate` verb), defer roles to P5.
- **P6** behind an explicit feature gate; do not enable by default.

**Backward compatibility.**
- Keep `cmd_chat/agent/providers.py` / `profiles.py` as thin re-export shims for
  at least one release after P1; add a deprecation note, remove later.
- `spawn` default runner stays `claude` so existing demos/scripts are unaffected.
- The Claude operator path (skill + `compose_directive`) must remain
  byte-identical in behaviour after P4 — verify with the existing vmhub capture.

**Commit hygiene.** Conventional-commit style matching the repo
(`feat(operator): …`, `refactor(ai): …`, `docs(operator): …`). Reference this
spec in the body. Do **not** squash the refactor (P1) into a feature commit —
keep the no-behaviour-change move isolated so a bisect can trust it.

**Testing gates (every PR must keep green).**
1. `pytest` unit suite (bootstrap logic is already pure/side-effect-free — keep
   it that way; add runner-registry + role-loader unit tests).
2. The native-harness benchmark (regression guard for the loop extraction in P2).
3. The **vmhub tmux-demo capture** as the integration smoke for P2/P5
   (Claude-spawns-Claude still works; then the local-model variant).

**Secrets.** No keys in `roles/*.toml`, `models.toml`, `CAPABILITIES.md`, or
runner specs — only `api_key_env` names. Add a CI/pre-commit grep for accidental
key patterns if one isn't already present.

**Docs.** Update `CLAUDE.md` (key-components table + operator section) and the
`hh-operator` skill to reference `cmd_chat/ai/` and the new `operate` verb /
runner registry / roles once P1–P5 land. Add a memory note on the new module
layout.

---

## 6. Open questions
- **Loop extraction boundary (P2):** how much of `agent/bridge.py`'s loop is
  genuinely model-agnostic vs chat-specific (typing/stream frames, transcript
  semantics)? Spike before committing the extraction shape.
- **Non-Claude CLI auth:** codex/gemini creds/login flows differ from Claude's
  `~/.claude/.credentials.json` — first-class entries need per-runner validation;
  until then route them through the generic `cmd` runner.
- **Degrade policy when `ToolsUnsupported`:** refuse vs constrained text-protocol
  fallback for the harness operator. Leaning refuse-with-clear-message to avoid a
  weak model "operating" by prose.
- **Per-agent token ceiling:** enforce as a hard stop, or as a compaction
  trigger first then stop? Likely both, configurable per role.
- **MCP transport (P6):** stdio for local clients vs SSE for remote — pick based
  on the first target client.
```
