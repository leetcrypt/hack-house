#!/usr/bin/env python3
"""Robust primitives for driving the hack-house Rust TUI headlessly via tmux.

The TUI is a full-screen ratatui app whose message box has **no line-editing
shortcuts** — Ctrl-A/U/K arrive as the literal letters a/u/k, so the only way to
clear the input is Backspace. A single `Enter` also doesn't always register, so
every primitive here VERIFIES the on-screen input line and retries instead of
firing keystrokes blind. Shared by `run.py` (task sending) and the agent-restart
CLI below so both stop leaking stale text into the box.

Online/grant detection counts occurrences in the scrollback and waits for the
count to *increase* — never just "is the needle present" — because a previous
`/ai start <same-model>` leaves a stale "online" line that a naive check would
match immediately.

CLI:
    python bench/tui.py restart <model> [--target hh-house:2]
    python bench/tui.py clear            [--target hh-house:2]
    python bench/tui.py send '<text>'    [--target hh-house:2]
"""

import argparse
import re
import subprocess
import sys
import time

DEFAULT_TARGET = "hh-house:2"

# The message box bottom line looks like:  │> some text              │
# Capture the text between the prompt and the box's right border.
_INPUT_RE = re.compile(r"│>\s?(.*)│\s*$")


def tmux(*args):
    return subprocess.run(["tmux", *args], capture_output=True, text=True)


def capture(target, lines=None):
    """The current VISIBLE screen of the TUI pane.

    Deliberately NO `-S`/scrollback: this is a full-screen alt-screen app, so
    `capture-pane -S -N` returns stale or empty buffer frames (it once reported
    an agent in the clergy that the live screen had already dropped). Only the
    live viewport is trustworthy. The `lines` arg is accepted for call-site
    compatibility but ignored."""
    return tmux("capture-pane", "-t", target, "-p").stdout


def input_text(target):
    """Current contents of the message input box (best-effort, '' when empty)."""
    for line in reversed(capture(target, lines=60).splitlines()):
        m = _INPUT_RE.search(line)
        if m:
            return m.group(1).rstrip()
    return ""


def clear_input(target, tries=12, burst=120):
    """Backspace the input box until it reads empty. Harmless when already empty."""
    for _ in range(tries):
        if not input_text(target):
            return True
        tmux("send-keys", "-t", target, *(["BSpace"] * burst))
        time.sleep(0.25)
    return not input_text(target)


def submit(target, tries=5):
    """Press Enter until the input box empties (i.e. the line was actually sent)."""
    for _ in range(tries):
        tmux("send-keys", "-t", target, "Enter")
        time.sleep(0.6)
        if not input_text(target):
            return True
    return not input_text(target)


def send_command(target, text):
    """Clear the box, type `text`, and submit it with verification. Returns bool."""
    clear_input(target)
    tmux("send-keys", "-t", target, "-l", text)
    time.sleep(0.3)
    return submit(target)


def _columns(target):
    """Split the two side-by-side panels. The TUI is a full-screen alt-screen
    app, so `capture-pane` only ever returns the VISIBLE viewport (no
    scrollback) — counting chat events is therefore unreliable, but the clergy
    roster on the right is present-tense and doesn't scroll. Returns
    (chat_text, clergy_text)."""
    chat, clergy = [], []
    for line in capture(target).splitlines():
        parts = line.split("│")
        if len(parts) >= 5:          # │ chat ││ clergy │
            chat.append(parts[1])
            clergy.append(parts[3])
    return "\n".join(chat), "\n".join(clergy)


def agent_online(target, model):
    """True iff `model` is currently listed in the clergy roster panel — the
    one signal that reflects live state instead of scrolled-away history."""
    return model in _columns(target)[1]


def wait_state(target, predicate, timeout, poll=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(poll)
    return False


def restart_agent(target, model, online_timeout=180, start_tries=2):
    """Stop the current agent, start `model`, and grant it drive — each step
    verified against the live clergy roster so a hung spawn is retried rather
    than a healthy one dismissed. Returns True only if the new agent is online
    AND holds drive.

    `online_timeout` is deliberately generous: a cold `/ai start` reloads the
    model into Ollama and routinely takes 60–90 s on the CPU-only box, so a
    tight window would kill a slow-but-healthy spawn and churn it into a stuck
    summon (exactly what the old aggressive retry caused)."""
    send_command(target, "/ai stop")
    wait_state(target, lambda: not agent_online(target, model), 20)

    started = False
    for _ in range(start_tries):
        send_command(target, f"/ai start {model}")
        if wait_state(target, lambda: agent_online(target, model), online_timeout):
            started = True
            break
        # spawn hung — dismiss the stuck summon and try once more
        send_command(target, "/ai stop")
        wait_state(target, lambda: not agent_online(target, model), 20)
    if not started:
        return False

    time.sleep(2)  # let the roster settle before granting
    send_command(target, "/grant ai")
    granted = f"granted drive to all AI agents: {model}"
    return wait_state(target, lambda: granted in _columns(target)[0], 20)


def main():
    ap = argparse.ArgumentParser(description="Drive the hack-house TUI via tmux.")
    ap.add_argument("action", choices=["restart", "clear", "send", "show"])
    ap.add_argument("arg", nargs="?", help="model (restart) or text (send)")
    ap.add_argument("--target", default=DEFAULT_TARGET, help="tmux window of the TUI")
    a = ap.parse_args()

    if a.action == "clear":
        ok = clear_input(a.target)
        print("input clear" if ok else "! input still has text")
        return 0 if ok else 1
    if a.action == "show":
        print(repr(input_text(a.target)))
        return 0
    if a.action == "send":
        if not a.arg:
            print("send needs text", file=sys.stderr)
            return 2
        ok = send_command(a.target, a.arg)
        print("sent" if ok else "! could not submit (box not empty)")
        return 0 if ok else 1
    if a.action == "restart":
        if not a.arg:
            print("restart needs a model", file=sys.stderr)
            return 2
        ok = restart_agent(a.target, a.arg)
        print(f"{a.arg} online+drive" if ok else f"! {a.arg} restart failed")
        return 0 if ok else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
