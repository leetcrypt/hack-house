"""Per-language recipes for the capability benchmark.

Each Lang knows four things the harness needs:

  • where its problems live  (HF dataset + config)
  • how to assemble one runnable program from prompt + model completion + tests
  • the filename to write it to
  • the shell command that compiles/runs it (exit 0 == all tests passed)
  • a podman image carrying that toolchain (for the isolated runtime)

Adding a language is a single entry here — nothing else in the harness needs to
change. That is the whole point: the matrix is data, not code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Lang:
    id: str                       # short key used on the CLI ("go", "rust", …)
    dataset: str                  # HF dataset repo
    config: str                   # HF config (humaneval-go, …)
    filename: str                 # file the assembled program is written to
    run: str                      # shell command, run in the work dir
    image: str                    # podman image carrying the toolchain
    # assemble(prompt, completion, row) -> full source text
    assemble: Callable[[str, str, dict], str]


def _concat(prompt: str, completion: str, row: dict) -> str:
    """The MultiPL-E convention: prompt + completion + tests, verbatim."""
    return f"{prompt}{completion}\n{row.get('tests', '')}\n"


def _python(prompt: str, completion: str, row: dict) -> str:
    """Original HumanEval (openai_humaneval): the test is a `check(fn)` def, so
    we append it and then actually call it on the entry point."""
    entry = row.get("entry_point", "")
    return f"{prompt}{completion}\n\n{row.get('test', '')}\n\ncheck({entry})\n"


# MultiPL-E ships no `humaneval-py` (HumanEval is *natively* Python — MultiPL-E
# only translates out of it), so Python pulls from the original dataset instead.
LANGS: dict[str, Lang] = {
    "python": Lang(
        id="python", dataset="openai/openai_humaneval", config="openai_humaneval",
        filename="prog.py", run="python3 prog.py",
        image="docker.io/library/python:3.11-alpine", assemble=_python),
    "javascript": Lang(
        id="javascript", dataset="nuprl/MultiPL-E", config="humaneval-js",
        filename="prog.js", run="node prog.js",
        image="docker.io/library/node:18-alpine", assemble=_concat),
    "bash": Lang(
        id="bash", dataset="nuprl/MultiPL-E", config="humaneval-sh",
        filename="prog.sh", run="bash prog.sh",
        image="docker.io/library/bash:5", assemble=_concat),
    "go": Lang(
        id="go", dataset="nuprl/MultiPL-E", config="humaneval-go",
        # MultiPL-E names the file *_test.go and `go test` needs a module.
        filename="prog_test.go",
        run="go mod init prog >/dev/null 2>&1; go test ./...",
        image="docker.io/library/golang:1.22-alpine", assemble=_concat),
    "rust": Lang(
        id="rust", dataset="nuprl/MultiPL-E", config="humaneval-rs",
        filename="prog.rs", run="rustc -A warnings prog.rs -o prog && ./prog",
        image="docker.io/library/rust:1-alpine", assemble=_concat),
}

ALIASES = {"py": "python", "js": "javascript", "sh": "bash", "rs": "rust"}


def resolve(name: str) -> Lang:
    key = ALIASES.get(name.lower(), name.lower())
    if key not in LANGS:
        raise KeyError(f"unknown language {name!r}; known: {', '.join(LANGS)}")
    return LANGS[key]
