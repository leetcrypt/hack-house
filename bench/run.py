#!/usr/bin/env python3
"""Native-harness benchmark runner.

Drives the live hack-house Rust TUI headlessly (tmux send-keys / capture-pane)
the same way a human tester does, then grades each task by GROUND TRUTH inside
the sandbox container (`podman exec`) — not by the model's self-reported
summary. Produces a PASS/FAIL table with per-task wallclock latency so harness
changes can be compared systematically over time.

Prereqs (set up exactly as for a manual live test — see
docs/findings-native-harness-2026-06-10.md and the tmux-testing memo):
  * a running server + Rust client in a tmux window (default hh-house:2),
  * an AI agent online with drive granted (`/ai start <model>` + `/grant ai`),
  * the sandbox container running (default `hack-house`).

Usage:
  python bench/run.py --model qwen2.5:3b
  python bench/run.py --tier easy --no-net
  python bench/run.py --only multi-easy,code-medium
  python bench/run.py --dry-run            # just print the selected matrix

This runner only SENDS tasks and reads state; it never starts/stops the agent,
so a crash leaves your session intact.
"""

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tasks as T  # noqa: E402
from tui import agent_online, capture, clear_input, send_command  # noqa: E402


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def podman(container, snippet):
    """Run a POSIX-sh snippet in the container; return (rc, output)."""
    r = sh(["podman", "exec", container, "sh", "-c", snippet])
    return r.returncode, (r.stdout + r.stderr).strip()


def is_thinking(buf):
    return "is thinking" in buf


def send_task(target, model, prompt):
    """Robustly clear the input box, then type and submit the /ai command. The
    box is fully backspace-cleared and the Enter is verified (see tui.py), so a
    dropped keystroke can't leave a half-typed prompt to corrupt the next task."""
    send_command(target, f"/ai {model} !{prompt}")


def scrape_summary(target, user):
    """Best-effort: the last `@<user>` line visible in the chat viewport. Only
    cosmetic — grading is by ground truth, never by this text."""
    buf = capture(target, lines=200)
    marker = f"@{user}"
    if marker not in buf:
        return ""
    tail = buf.rsplit(marker, 1)[-1]
    return " ".join(tail.splitlines()[:2]).strip(" │").strip()


def wait_done(target, user, timeout, engage=45.0, quiet=9.0, settle=2.0):
    """Wait for a task to finish using the 'thinking' footer — a signal that is
    independent of the chat viewport (the TUI is a full-screen app, so
    capture-pane scrollback is NOT reliable chat history). Phase 1: wait for the
    agent to engage (thinking appears). Phase 2: consider the task done once
    thinking has stayed absent for `quiet` seconds (bridges the brief
    tool-exec gaps between the loop's model turns). Returns (done, summary)."""
    deadline = time.time() + timeout
    engage_by = time.time() + engage
    saw_thinking = False
    off_since = None
    while time.time() < deadline:
        time.sleep(settle)
        thinking = is_thinking(capture(target, lines=120))
        if thinking:
            saw_thinking = True
            off_since = None
            continue
        if not saw_thinking:
            # not engaged yet; give it a window before treating quiet as 'done'
            if time.time() > engage_by:
                return False, scrape_summary(target, user)
            continue
        off_since = off_since or time.time()
        if time.time() - off_since >= quiet:
            return True, scrape_summary(target, user)
    return saw_thinking, scrape_summary(target, user)


def run_task(args, task):
    container, target, user, model = args.container, args.target, args.user, args.model
    # Per-task isolation: wipe ONLY the suite's known artifacts (never the home
    # dir), then run any seed setup.
    podman(container, T.CLEAN)
    if task.setup:
        podman(container, task.setup)

    # Make sure the agent is idle before sending so we don't read a prior task's
    # 'thinking' as this task's engagement.
    for _ in range(15):
        if not is_thinking(capture(target, lines=120)):
            break
        time.sleep(2)

    t0 = time.time()
    send_task(target, model, task.prompt)
    done, summary = wait_done(target, user, args.timeout)
    elapsed = time.time() - t0

    if not done:
        return {"id": task.id, "category": task.category, "tier": task.tier,
                "result": "TIMEOUT", "secs": round(elapsed, 1),
                "summary": summary[:160], "verify_out": ""}

    rc, out = podman(container, task.verify)
    return {"id": task.id, "category": task.category, "tier": task.tier,
            "result": "PASS" if rc == 0 else "FAIL", "secs": round(elapsed, 1),
            "summary": summary[:160], "verify_out": out[:160]}


def print_table(rows):
    w_id = max(len("task"), *(len(r["id"]) for r in rows))
    print(f"\n{'task':<{w_id}}  {'tier':<6}  {'result':<7}  {'secs':>6}  summary")
    print("-" * (w_id + 40))
    for r in rows:
        print(f"{r['id']:<{w_id}}  {r['tier']:<6}  {r['result']:<7}  "
              f"{r['secs']:>6}  {r['summary'][:70]}")
    n = len(rows)
    p = sum(r["result"] == "PASS" for r in rows)
    print(f"\n{p}/{n} PASS"
          + (f"  ({n - p} not passing)" if p < n else "  — clean sweep"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="qwen2.5:3b")
    ap.add_argument("--container", default="hack-house")
    ap.add_argument("--target", default="hh-house:2", help="tmux window of the TUI client")
    ap.add_argument("--user", default="dell", help="client username (for @mention detection)")
    ap.add_argument("--timeout", type=int, default=300, help="per-task wallclock cap (s)")
    ap.add_argument("--tier", choices=["easy", "medium", "hard"], action="append")
    ap.add_argument("--category", choices=["shell", "code", "git", "multi"], action="append")
    ap.add_argument("--only", help="comma-separated task ids")
    ap.add_argument("--no-net", action="store_true", help="skip git/network tasks")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out", help="write JSON results to this path")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(line_buffering=True)  # flush progress when backgrounded
    except Exception:
        pass

    ids = set(args.only.split(",")) if args.only else None
    selected = T.select(ids=ids, categories=args.category, tiers=args.tier,
                        allow_net=not args.no_net)
    if not selected:
        print("no tasks selected", file=sys.stderr)
        return 2

    if args.dry_run:
        for t in selected:
            net = " [net]" if t.needs_net else ""
            print(f"{t.id:<14} {t.tier:<6} {t.category:<6}{net}  {t.prompt}")
        return 0

    if not agent_online(args.target, args.model):
        print(f"! agent '{args.model}' does not look online in {args.target}; "
              f"start it (/ai start {args.model}) and /grant ai first.", file=sys.stderr)
        return 1

    print(f"running {len(selected)} task(s) against {args.model} "
          f"(container={args.container}, tui={args.target})")
    rows = []
    try:
        for i, task in enumerate(selected, 1):
            print(f"[{i}/{len(selected)}] {task.id} … ", end="", flush=True)
            r = run_task(args, task)
            print(f"{r['result']} ({r['secs']}s)")
            rows.append(r)
    finally:
        # Leave the input box empty so the next action (e.g. an agent restart)
        # isn't corrupted by a lingering prompt.
        clear_input(args.target)

    print_table(rows)

    payload = {"ts": datetime.now().isoformat(timespec="seconds"),
               "model": args.model, "rows": rows}
    out = args.out or f"bench/results/{datetime.now():%Y%m%d-%H%M%S}-{args.model.replace(':', '_')}.json"
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(payload, indent=2))
    print(f"\nresults → {out}")
    return 0 if all(r["result"] == "PASS" for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
