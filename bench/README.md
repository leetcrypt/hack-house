# Native-harness benchmark

A small, ground-truth-graded benchmark for the `/ai <model> !<task>` native
tool-calling loop (`cmd_chat/agent/bridge.py:_run_native`). Use it to compare
harness changes (and models) **systematically** instead of eyeballing one-off
runs.

## Why ground truth

The weak CPU model routinely *claims* success it didn't achieve ("written, made
executable, and run successfully" after a 126; "created notes.txt" while the
file actually contains `echo 'hello'`). So every task is graded by a POSIX-sh
`verify` snippet run **inside the sandbox** via `podman exec`, exit 0 == PASS —
never by the model's own summary. See
`docs/findings-native-harness-2026-06-10.md`.

Tasks run in the agent's real cwd (the container HOME, `/root`) with **bare
filenames** — pinning an absolute `/root/bench/...` path instead just measures
whether the weak model honours absolute paths (it ~never does for `run_shell`
redirects) and drowns out every other signal. Only the suite's known artifacts
are wiped between tasks; the home dir itself is never `rm -rf`d.

## The matrix

Four categories × easy / medium / hard (`bench/tasks.py`):

| category | tools exercised | easy → hard |
|---|---|---|
| `shell` | run_shell | whoami→file · nested mkdir · count `/etc/*.conf` |
| `code`  | write_file + run_shell | py `2+3` · **chmod +x script** · 10× Fibonacci |
| `git`   | run_shell + network | clone · clone+read README · clone+report branch |
| `multi` | chained steps / recovery | mkdir→write→list · reverse-sort · word-count script |

`code-medium` (write → `chmod +x` → `./run`) and `multi-easy` are the
historically hardest cases — they're the nudge loop's main targets, so they
double as regression canaries.

## Prerequisites

Same setup as a manual live test:

1. Server + Rust client running in a tmux window (default `hh-house:2`).
2. An agent online with drive: `/ai start <model>` then `/grant ai`.
3. The sandbox container running (default `hack-house`) with `python3`, `git`,
   and outbound network (the `git` tier needs it).

The runner only *sends* tasks and *reads* state — it never starts/stops the
agent, so a failure leaves your session intact.

## Running

```bash
.venv/bin/python bench/run.py --model qwen2.5:3b      # full suite
.venv/bin/python bench/run.py --tier easy             # one tier
.venv/bin/python bench/run.py --no-net                # skip git/network tasks
.venv/bin/python bench/run.py --only code-medium,multi-easy
.venv/bin/python bench/run.py --dry-run               # print the matrix, run nothing
```

Useful flags: `--container`, `--target` (tmux window), `--user` (for @mention
detection), `--timeout` (per-task wallclock cap, default 300 s).

## Output

A PASS/FAIL table with per-task latency, plus a JSON record under
`bench/results/` (gitignored — these are runtime artifacts). Commit a curated
baseline explicitly (e.g. `--out bench/results/baseline-<model>.json` then
`git add -f`) when you want one tracked for comparison.

## Tracked baselines (2026-06-10, CPU-only, shared room)

Original-harness baselines (default sampling, structured `tool_calls` only):

| model | size | PASS | avg/med s |
|---|---|---|---|
| qwen2.5:0.5b | 397 MB | 1/12 | 55 / 49 |
| qwen2.5-coder:1.5b | 986 MB | 1/12 | 67 / 53 |
| **qwen2.5:3b** | 1.9 GB | 1/12 | 51 / 46 |
| qwen2.5-coder:7b | 4.7 GB | 2/12 | 141 / 106 |

After the harness work (split-tag recovery + `temperature:0` for the tool loop +
fenced-prose recovery — see `docs/findings-native-harness-2026-06-10.md`):

| model | size | PASS (2 runs) | avg s | note |
|---|---|---|---|---|
| **qwen2.5:0.5b** | 397 MB | **4/12, 3/12** | ~50 | split-tag leak dominates → 3× lift |
| qwen2.5:3b | 1.9 GB | 2/12, 0/12 | ~50 | emits proper calls → unchanged |
| llama3.2:3b | 2.0 GB | 1/12 | 55 | leaks positional prose (`run_shell "x"`) — unparseable |
| qwen2.5-coder:3b | 1.9 GB | 1/12 | 53 | coder gains nothing on these tasks |

Takeaways: the parser fixes lift exactly the **weakest** model (0.5b), which leaks
malformed text instead of structured calls; a 3B-class model emits proper calls and
fails on capability/content, which no parser can fix, so it stays in the 0–2/12 noise
band. **qwen2.5:3b stays the default** (best capability/latency); 0.5b is now a viable
floor option for trivial tasks. Treat absolute scores as a *relative* regression
yardstick, not a grade.

Usable models: qwen2.5(-coder) 0.5b–7b, llama3.2:3b (all tool-capable). Rejected:
`deepseek-r1` and `westenfelder/NL2SH` reject Ollama's `tools` field;
`granite3.1-dense:2b` errors (`model not supported by your version of Ollama` — needs
a newer Ollama).
