#!/usr/bin/env python3
"""Live bench/smoke for the native `!task` harness (docs/spec-native-harness.md, Phase 3).

Drives the real `AgentBridge._run_native` against a live Ollama model and the
`local` sandbox backend (host shell, scoped to a fresh temp workdir), with no chat
server or TUI in the loop. For each canonical task it reports: wall-clock latency,
model turns, tool calls, whether the expected artifact landed, and the final
answer. Also records a single chat-completion latency as the `simple`-harness
model-cost baseline (simple's execution needs the broker PTY, so only its model
call is comparable headlessly).

Usage:  .venv/bin/python hh/scripts/bench-native-harness.py [--model qwen2.5:3b]
                                                            [--max-turns 5] [--threads 4]
Env:    OLLAMA_HOST (default http://localhost:11434)
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

# Make the repo importable when run from anywhere.
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from cmd_chat.agent.bridge import AgentBridge  # noqa: E402
from cmd_chat.agent.providers import (  # noqa: E402
    Msg,
    OllamaProvider,
    ToolsUnsupported,
)

PER_TASK_TIMEOUT = 300.0  # hard ceiling so a runaway loop can't hang the bench

# (label, task text, check(workdir) -> bool)
TASKS = [
    (
        "write+read",
        "create a file hello.txt containing exactly the text 'hello world', "
        "then show its contents",
        lambda d: (d / "hello.txt").is_file()
        and "hello world" in (d / "hello.txt").read_text(),
    ),
    (
        "script+run",
        "write a python script add.py that prints the sum of 2 and 3, then run it",
        lambda d: (d / "add.py").is_file(),
    ),
    (
        "mkdir+list",
        "make a directory named data and then list the files in the current directory",
        lambda d: (d / "data").is_dir(),
    ),
]


class CountingOllama(OllamaProvider):
    """OllamaProvider that tallies tool-calling turns for the report."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.tool_turns = 0

    def complete_with_tools(self, system, messages, tools):
        self.tool_turns += 1
        return super().complete_with_tools(system, messages, tools)


def make_bridge(provider, workdir: str):
    """A headless AgentBridge wired to the local backend, with all network sends
    stubbed to capture room output instead of encrypting to a websocket."""
    b = AgentBridge(
        "localhost", 0, name="bench", provider=provider,
        no_tls=True, code_provider=provider, harness="native",
        max_turns=provider_max_turns,
    )
    b.granted = True
    b.sbx_engine = "local"   # exec on the host shell (this process's CWD = workdir)
    b.sbx_name = ""
    b._chat: list[str] = []

    async def cap_chat(ws, text):
        b._chat.append(text)

    async def noop(*a, **k):
        pass

    b._send_chat = cap_chat
    b._send_typing = noop
    b._send_stream = noop

    async def cap_inject(ws, cmds):  # simple-harness path (unused here, kept honest)
        b._chat.append("[inject] " + " ; ".join(cmds))

    b._inject = cap_inject
    return b


provider_max_turns = 5  # set in main()


async def run_task(provider, label, task, check) -> dict:
    workdir = Path(tempfile.mkdtemp(prefix=f"hh-bench-{label}-"))
    cwd = os.getcwd()
    os.chdir(workdir)
    provider.tool_turns = 0
    bridge = make_bridge(provider, str(workdir))
    t0 = time.monotonic()
    timed_out = False
    err = None
    try:
        await asyncio.wait_for(
            bridge._run_native(None, task, "andre"), timeout=PER_TASK_TIMEOUT)
    except asyncio.TimeoutError:
        timed_out = True
    except Exception as e:  # noqa: BLE001 — record, don't abort the suite
        err = f"{type(e).__name__}: {e}"
    finally:
        os.chdir(cwd)
    elapsed = time.monotonic() - t0
    ok = False
    try:
        ok = bool(check(workdir)) and not timed_out and err is None
    except Exception:  # noqa: BLE001
        ok = False
    final = next((c for c in reversed(bridge._chat) if "(native) for" in c), "")
    return {
        "label": label, "ok": ok, "elapsed": elapsed, "turns": provider.tool_turns,
        "timed_out": timed_out, "err": err, "workdir": str(workdir),
        "chat": bridge._chat, "final": final,
    }


async def main() -> int:
    global provider_max_turns
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="qwen2.5:3b")
    ap.add_argument("--max-turns", type=int, default=5)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--num-ctx", type=int, default=4096)
    ap.add_argument("--verbose", action="store_true", help="print full chat per task")
    args = ap.parse_args()
    provider_max_turns = args.max_turns

    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    print(f"== native harness bench ==  model={args.model}  host={host}")
    print(f"   max_turns={args.max_turns}  threads={args.threads}  num_ctx={args.num_ctx}\n")

    provider = CountingOllama(
        model=args.model, num_ctx=args.num_ctx, num_thread=args.threads, num_predict=512)

    # 0. Wire preflight: does the model accept the `tools` field at all?
    print("[preflight] probing tool support…", flush=True)
    t0 = time.monotonic()
    try:
        text, calls = await asyncio.to_thread(
            provider.complete_with_tools,
            "You are a test.",
            [{"role": "user", "content": "Call run_shell to echo hi."}],
            __import__("cmd_chat.agent.bridge", fromlist=["NATIVE_TOOLS"]).NATIVE_TOOLS,
        )
    except ToolsUnsupported as e:
        print(f"  ✖ model rejects tools: {e}\n  → native would degrade to simple. Stopping.")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"  ✖ preflight error: {type(e).__name__}: {e}")
        return 2
    print(f"  ✓ tools accepted in {time.monotonic()-t0:.1f}s "
          f"(calls={len(calls)}, supports_tools={provider.supports_tools()})\n")

    # Baseline: one plain chat completion (the only model cost simple pays).
    t0 = time.monotonic()
    try:
        await asyncio.to_thread(provider.complete, "You are concise.",
                                [Msg("user", "say ok")])
        base = time.monotonic() - t0
        print(f"[baseline] one chat completion (≈ simple's model cost): {base:.1f}s\n")
    except Exception as e:  # noqa: BLE001
        print(f"[baseline] chat completion failed: {e}\n")

    results = []
    for label, task, check in TASKS:
        print(f"[task:{label}] {task}", flush=True)
        r = await run_task(provider, label, task, check)
        tag = "PASS" if r["ok"] else ("TIMEOUT" if r["timed_out"] else "FAIL")
        print(f"  → {tag}  {r['elapsed']:.1f}s  turns={r['turns']}"
              + (f"  err={r['err']}" if r["err"] else ""))
        if r["final"]:
            print(f"    final: {r['final'].splitlines()[-1][:160]}")
        if args.verbose:
            for c in r["chat"]:
                print("      | " + c.replace("\n", " ")[:160])
        print(flush=True)
        results.append(r)

    # Summary table.
    print("== summary ==")
    print(f"{'task':<14}{'result':<9}{'secs':>7}{'turns':>7}")
    for r in results:
        tag = "PASS" if r["ok"] else ("TIMEOUT" if r["timed_out"] else "FAIL")
        print(f"{r['label']:<14}{tag:<9}{r['elapsed']:>7.1f}{r['turns']:>7}")
    passes = sum(1 for r in results if r["ok"])
    print(f"\n{passes}/{len(results)} passed")
    return 0 if passes == len(results) else 3


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
