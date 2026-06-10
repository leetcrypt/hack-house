# Native harness — live command-entry findings + nudge-loop design

**Date:** 2026-06-10
**Context:** Evaluating the `native` tool-calling harness (`cmd_chat/agent/bridge.py`,
`_run_native`) on its ability to drive shell commands into the shared podman/Kali
sandbox, on a CPU-only box (no GPU). Two prior fixes were live-validated here and
committed in `a0aa14d`: §3 PTY-sentinel (`_run_shell_in_pty`) and `NATIVE_CONTEXT=4`.

## Test setup

- Fresh room, podman sandbox (`hack-house` container), `/grant ai` drive.
- Models compared: `qwen2.5:3b` (1.9 GB, prior default) and `qwen2.5-coder:7b`
  (4.7 GB, pulled for this test). `deepseek-r1:latest` and `westenfelder/NL2SH`
  were ruled out — both **reject Ollama's `tools` field** (`HTTP 400 "does not
  support tools"`), so they degrade to the simple injector and can't exercise the
  native loop.
- Each task verified against **ground truth** (podman exec into the container),
  not just the chat/PTY surface.

## Results: qwen2.5:3b vs qwen2.5-coder:7b

| Behaviour | qwen2.5:3b | qwen2.5-coder:7b |
|---|---|---|
| Single command (`whoami`) | PASS | PASS |
| write+run (`hello.sh` date+hostname) | PASS, accurate summary | PASS |
| **Multi-step** (mkdir→write→list) | **FAIL** — stalled after step 1 ("ok, let's proceed to the next step") | PASS — chained make_dir→write→read, completed |
| **Recover from mid-task error** | FAIL — gives up | INCONSISTENT — recovered from `[unknown tool make_dir]`, but on `greet.sh` skipped chmod, hit exit 126, then **stopped** |
| Tool-schema discipline | clean | hallucinated a `make_dir` tool (not in schema) — wasted a turn (`[unknown tool make_dir]`, bridge.py:597) |
| Final summary quality | vague ("I will proceed…") | empty → `(done)` fallback, or raw advice text |
| Latency (CPU, no GPU) | ~20–40 s/task | ~2–4× slower, 30–90 s/turn |

### Failure modes observed (both sizes)

1. **Early termination on multi-step** (3B dominant): the model emits filler text
   with no tool call mid-task; the loop reads "text + no tool call = done" and stops.
   The remaining steps never run.
2. **Give-up on unresolved non-zero exit** (both sizes): after a failing command
   (exit 126/127), the model emits a summary/advice instead of fixing the cause.
   greet.sh left at mode 644 (never chmod'd) despite the task saying "make it
   executable".
3. **Tool hallucination** (7B): invented `make_dir`; the schema only has
   run_shell/write_file/read_file.

### Root cause in code

`_run_native`, bridge.py:739–741:
```python
if not calls:
    final = (text or "").strip()
    break        # ANY text-without-toolcall ends the task
```
NATIVE_SYSTEM (line 111–113) *also* tells the model to signal completion this exact
way. So one signal — "text, no tool call" — is overloaded to mean BOTH "done" and
"stalling/thinking". Disambiguating it is the fix.

**Takeaway:** a bigger model buys multi-step persistence (the 3B's main flaw) but
does NOT eliminate the give-up-on-error mode, and costs heavy latency. A nudge loop
that keys off **exit codes** (not just filler text) earns its keep at both sizes.

## Research: how Goose & opencode structure loop termination

(Source read directly from clones; key files cited.)

- **Goose** (`crates/goose/src/agents/agent.rs`): tracks `no_tools_called`
  (line 1801). When true it does NOT just stop (lines 2232–2316) — in
  structured/goal modes it injects a continuation nudge
  (`FINAL_OUTPUT_CONTINUATION_MESSAGE` = "You MUST call the final_output tool
  NOW…") and loops. Completion is an **explicit terminal tool**
  (`recipe__final_output`, `final_output_tool.rs`). Vanilla chat with no
  recipe/goal does fall through to text-as-done, but every structured path
  overrides that. Bounded by `max_turns`.
- **opencode** (`packages/opencode/src/session/prompt.ts:1156–1183`): exits only
  when finish is a real stop reason **AND** it independently re-derives that there
  are no pending tool calls from the parsed message parts — comment: *"Some
  providers return 'stop' even when the assistant message contains tool calls."*
  Hard step cap (`maxSteps`) with a wind-down message (`MAX_STEPS`,
  `prompt/max-steps.txt`); structured mode forces `toolChoice: "required"`.
- **Canonical / Cline-Roo**: weak-model harnesses favour an explicit completion
  action (`attempt_completion`/`submit`) over trusting "empty tool_calls = done".
- **Recommendation from research:** go structural, not filler-heuristic ("a losing
  arms race"); nudge on text-without-completion; a hard cap is the only
  unconditional exit; derive "did a tool get called" from parsed parts, not
  `finish_reason` (qwen on CPU is unreliable there — it leaks tool calls as
  `<tool_call>` text in `content`; already handled by
  `OllamaProvider._extract_text_tool_calls`).

## Design: dynamic nudge loop (output-aware, structural terminator)

Structural termination via a **`DONE:` text sentinel** rather than a 4th tool.
Divergence from the research's "add a done tool", justified for THIS model:
qwen-on-CPU already mis-emits structured tool calls (leaks as `<tool_call>` text;
the 7B even hallucinated `make_dir`). A 4th tool invites the same malformation and
competes for attention; a `DONE:` prefix disambiguates the existing text channel at
zero schema cost. This is opencode's "require an explicit marker, don't trust the
implicit stop", applied to the text channel.

Changes in `_run_native` (bridge.py):

1. **NATIVE_SYSTEM** (line 111–113): replace "reply with a summary and DO NOT call
   a tool" with → *"When and ONLY when the task is fully done, reply with one line
   starting `DONE:` and a one-sentence result. Otherwise you MUST call a tool."*
2. **Track loop state:** `did_action` (any tool ran), `last_rc` (parse the `exit=`
   already formatted by `_run_shell_in_pty`), `nudges=0`.
3. **Replace the bare `break` (739–741) with a verdict gate:**
   - text starts with `DONE:` → **done** (fast path, no nudge)
   - `last_rc` ∉ {0, None} and no `DONE:` → **stalled** (unresolved error)
   - no `did_action` → **stalled** (pure talk)
   - filler regex (`let's proceed`, `next step`, `i will`, trailing colon) and no
     `DONE:` → **stalled**
   - else (substantive text, action happened, no error) → **accept as done** — the
     single heuristic, on the SAFE side, so finished tasks aren't nudged every time
     (protects CPU latency, the cost a pure-structural form would incur here).
4. **On `stalled`:** append an output-aware, Goose-style nudge and `continue`,
   bounded by `MAX_NUDGES = 2` (separate from the productive `max_turns` so real
   multi-step work isn't starved):
   > *[if last_rc≠0:]* "The last command exited {rc} (failure) — fix that first
   > (e.g. `chmod +x` before running a script)." + "You haven't signalled
   > completion. If `{task}` is fully done reply `DONE: …`; otherwise call the next
   > tool now — act, don't describe."
5. **Wind-down:** on the final turn, inject "reply `DONE:` with your result now"
   (opencode's `MAX_STEPS` pattern).
6. **Keep** `_extract_text_tool_calls` (already present) — opencode's "derive tool
   calls from parsed parts, not finish_reason" lesson, which this model needs.

Fixes mapped to observed failures: 3B early-stall → nudged to continue; give-up on
error (both sizes) → nudged with the exit-code-aware message; over-nudging finished
tasks → avoided by the `DONE:` fast path + safe-side accept.

## Live validation (qwen2.5:3b, post-implementation)

Ran the two prior failure cases against the freshly-built loop; ground truth via
`podman exec hack-house`.

- **Multi-step (`mkdir proj3 → write notes.txt → list`): PASS — fixed.** Previously
  stalled after step 1; now chained write_file → `ls -1 proj3` to completion.
  Ground truth: `proj3/notes.txt` exists, contents `hello world`. The 3B's dominant
  failure mode is resolved by the loop alone (no model upgrade needed).
- **Nudge fires on non-zero exit: confirmed.** On the `greet.sh` case the PTY mirror
  showed `▸ (nudge: finish the task or reply DONE:)` after a 126, i.e. the
  output-aware re-prompt triggered structurally as designed.
- **Give-up-on-error is model-bound, not loop-bound.** The 3B still can't *recover*
  the greet.sh task even when nudged: across runs it wrote self-referential content
  (`echo "Hello, world!" > greet.sh`), its `chmod +x` didn't stick (file left 644),
  and in one run it leaked a bare `write_file proj3/greet.sh …` tool call **as
  summary text** (not the `<tool_call>` JSON form `_extract_text_tool_calls`
  handles). These are 3B capability limits, consistent with the 3B-vs-7B table —
  the loop's job is to detect and report them honestly, not to make a weak model
  competent.

### Honesty hardening added after live test

The weak 3B routinely *claims* success it didn't achieve ("written, made executable,
and run successfully" after a 126; "Created greet.sh… ran it" with nothing on disk).
Echoing that prose as the final summary is actively misleading, so the nudge-budget
exhaustion path no longer trusts it blindly:

- never ran a tool → `[stopped — model described the task but never ran a tool]`
- left a non-zero exit → `[stopped — last command exited {rc}; task likely
  incomplete] …`
- clean, action-backed turn → the model's summary (unchanged).

Offline unit coverage: 23 assertions on `_completion_verdict` / `_parse_exit` /
`_strip_done` / `_nudge_message`, all pass.

## Benchmark suite + first baseline (`bench/`)

To compare harness/model changes **systematically** instead of eyeballing one-off
runs, added a ground-truth-graded benchmark: `bench/tasks.py` (a 4-category ×
easy/medium/hard matrix — shell, code, git, multi) and `bench/run.py` (drives the
live TUI via tmux, grades each task by a `podman exec` verify snippet, exit 0 ==
PASS). Tasks run in the agent's real cwd (`/root`) with bare filenames; pinning an
absolute path instead just measured the weak model's absolute-path discipline and
drowned out every other signal.

**Runner gotcha (cost real time):** the TUI is a full-screen (alt-screen) app, so
`tmux capture-pane -S` does NOT yield chat history — only the current viewport.
The first detector diffed a `@user` reply count and hung once old replies scrolled
out of view. Fixed by keying completion off the **`is thinking…` footer** (engage →
sustained-absence), which is viewport-independent.

**Prompt-fairness fix (found via the benchmark):** "…in your home directory" made
literal-minded models create a `home/` subdir (`/root/home/a/b/c/marker.txt`) or use
`~/who.txt`; dropped the phrase — bare filenames land in the agent's cwd (`/root`).

### Baselines — four local CPU models (all tool-capable; fixed prompts)

Every candidate was probed first: `qwen2.5(-coder)` at 0.5b/1.5b/3b/7b all accept
Ollama's `tools` field; `deepseek-r1` and `westenfelder/NL2SH` reject it (HTTP 400 →
degrade to the simple harness, useless for the native loop), `llava` is vision-only.
One run each, shared room, CPU-only box:

| model | size | PASS | avg / median s | passed |
|---|---|---|---|---|
| qwen2.5:0.5b | 397 MB | 1/12 | 55 / 49 | shell-hard |
| qwen2.5-coder:1.5b | 986 MB | 1/12 | 67 / 53 | shell-medium |
| **qwen2.5:3b** | 1.9 GB | 1/12 | **51 / 46** | code-easy |
| qwen2.5-coder:7b | 4.7 GB | 2/12 | 141 / 106 | shell-easy, multi-easy |

**Takeaways.** All four cluster at **1–2/12 with high variance** (which task passes
flips run-to-run) — none reliably drives multi-step sandbox tasks in a shared room.
The **7B doubles nothing**: same pass band at ~3× the latency, so it is not worth it
on this CPU box. **qwen2.5:3b is the sweet spot** (best speed, no worse quality) and
stays the default. The low absolute scores are inflated downward by self-contamination
(each task's `NATIVE_CONTEXT=4` window pulls the previous task's command/summary) — a
real deployment effect, but it means the suite's job is *relative* regression
detection, not an absolute capability grade.

Dominant failure modes the summaries exposed, and where they point next:

| failure mode | example | harness-addressable? |
|---|---|---|
| bare tool-call-as-text | `write_file ./who.txt`, `mkdir proj`, `git clone …` (esp. 0.5b — nearly every turn) | **yes** — extend `_extract_text_tool_calls` beyond `<tool_call>` JSON |
| `<native>` / `<tools>` tag leak in summary | `<native> Cloned repository…`, `<tools>` | yes — strip the tag |
| path hallucination | `/home/qwen253b/conf_count.txt`, `~/` unexpanded | partly (model) |
| content fabrication | wrote literal `dell` instead of running whoami | no (model capability) |

The first row was hypothesised as the top priority — a pure harness parse gap. The
benchmark now exists to prove whether fixing it moves the number.

### Parser fix — what the benchmark actually proved (2026-06-10)

Generalised `OllamaProvider._extract_text_tool_calls` to recover a tool-call JSON
object regardless of wrapper — qwen's `<tool_call>` tags, bare JSON, ```json fences,
alternate tags (`<tools>`, `<function_call>`), OpenAI `{"function":{…}}` nesting, and
`parameters`-vs-`arguments`. Gated on the known tool-name set (derived from the
`tools` schema) so a stray JSON blob in prose — or a hallucinated `make_dir` — can
never be coerced into an action. 11-case unit check: 8 leak shapes recover, 3
negatives (prose / unknown tool / random config JSON) ignored.

**Result: it does NOT move the weak-model pass rate.** Three 3b passes went 2 / 1 / 0
of 12 (prior baseline 1/12 — pure noise); two 0.5b passes went 0 / 0 (prior 1/12).

A direct `/api/chat` probe of 0.5b shows *why* — the hypothesis was wrong about the
**form** of the leak. The weak models do not emit a JSON tool-call as text. They emit
either:

  * **malformed structured `tool_calls`** — e.g. `write_file` with `content: null`
    (the field IS populated; the arguments are just wrong → model capability, not a
    parse gap); or
  * **a fenced shell/code block in prose with NO tool call at all** — e.g. content
    `"…let's proceed:\n```bash\nmkdir -p a/b/c\n```"`, `tool_calls: None`. This is the
    "[stopped — model described the task but never ran a tool]" case that dominates
    the 0.5b table.

So the structured-JSON recovery is a correct, safe hardening (it WILL catch a real
JSON-in-text leak from any model that produces one, and cannot fabricate an action),
but the failure it was meant to fix is not the failure these CPU models actually have.

**The real harness-addressable lever the data now points to:** recover **fenced code
blocks** (```bash … ``` / ```python … ```) as `run_shell` calls when the turn carried
no structured call. That is the genuine form of the weak-model leak. It is a bigger
safety call than JSON recovery (it executes a block the model wrote as prose, which
may be illustrative rather than an action), so it is left as an explicit decision, not
folded in silently.

| failure mode | example | harness-addressable? |
|---|---|---|
| malformed structured args | `write_file` with `content:null` | no (model capability) |
| fenced code in prose, no call | ```` ```bash\nmkdir -p a/b/c\n``` ````, `tool_calls:None` | **yes, but a safety call** — parse fences → run_shell |
| JSON tool-call leaked as text | `<tools>{"name":…}` | yes — **now handled** (no weak-model effect) |
| path hallucination | `/home/qwen253b/…`, `/ai/a/b/c`, `~/` unexpanded | partly (model) |
| content fabrication | wrote literal `octocat`/`dell` instead of running whoami | no (model capability) |

**Other models worth pulling for a non-qwen data point** (different family → different
failure modes, both with strong native tool-calling): `llama3.2:3b` (~2 GB, peer to
the 3B) and `llama3.1:8b` (~4.7 GB, peer to the 7B but general-purpose). Probe the
`tools` field first, then `bench/run.py --model <name>`.

The first two are the clear next harness improvements; the benchmark now exists to
prove whether they move the number.
