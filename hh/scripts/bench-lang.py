#!/usr/bin/env python3
"""bench-lang.py — launcher for the multi-language capability benchmark + picker.

This is the third hack-house benchmark, complementing:
  • bench-ai.py        — /ai chat latency/throughput on the real relay path
  • bench-sandbox.py   — /ai !task sandbox code-execution + safety guards

bench-lang answers the capability question MultiPL-E was built for: *can this
model actually write correct code in my language?* across Python, JavaScript,
Go, Rust and Bash — then weights the result by your workflow to recommend a
model. The implementation lives in the `bench/` package next to this file.

Examples:
    .venv/bin/python hh/scripts/bench-lang.py langs
    .venv/bin/python hh/scripts/bench-lang.py run \
        --models qwen2.5-coder:3b qwen2.5:3b --languages python bash --limit 10
    .venv/bin/python hh/scripts/bench-lang.py pick --workflow ops
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make the sibling `bench/` package importable when run as a plain script.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bench.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
