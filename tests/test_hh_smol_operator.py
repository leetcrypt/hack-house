"""Offline tests for scripts/hh-smol-operator.py's leaked-tool-call guard.

Regression test for the fixerr fixture bug (2026-09-06, re-verified + fixed
2026-09-20): a model wrote a bridge-tool call (read_file/write_file/exec_shell)
into a file meant to run standalone via python3 in the sandbox, where those
names don't exist. Loaded via importlib since the hyphenated filename isn't a
valid module name; only pure functions are exercised, no smolagents/socket I/O.
"""

import importlib.util
from pathlib import Path

_PATH = Path(__file__).parent.parent / "scripts" / "hh-smol-operator.py"
_spec = importlib.util.spec_from_file_location("hh_smol_operator", _PATH)
hh_smol_operator = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hh_smol_operator)


def test_leaked_read_file_call_is_detected():
    content = "n = int(read_file('/root/n.txt'))\nprint(100 / n)"
    assert hh_smol_operator._leaked_tool_calls(content) == ["read_file"]


def test_stdlib_open_is_not_flagged():
    content = "with open('/root/n.txt') as f:\n    n = int(f.read())\nprint(100 / n)"
    assert hh_smol_operator._leaked_tool_calls(content) == []


def test_self_defined_shadowing_function_is_not_flagged():
    content = "def read_file(p):\n    return open(p).read()\nprint(read_file('/root/n.txt'))"
    assert hh_smol_operator._leaked_tool_calls(content) == []


def test_multiple_leaked_calls_are_all_detected():
    content = "exec_shell('ls'); write_file('/x', 'y')"
    assert set(hh_smol_operator._leaked_tool_calls(content)) == {"exec_shell", "write_file"}


# Regression tests for the parse-loop bug (fixerr-fix regression check, 2026-09-21):
# CAPABILITIES.md's CLI-verb syntax (`watch --for ... --in events`, `spawn "..." --go`)
# leaked into generated Python and caused a SyntaxError / burned 3x tokens in a
# parser-retry loop. The code-as-action prompt must never contain that syntax.

def test_codeact_capabilities_strips_cli_verb_syntax():
    trimmed = hh_smol_operator._capabilities(for_codeact=True)
    assert "watch --for" not in trimmed
    assert "spawn \"" not in trimmed
    assert "## Verbs" not in trimmed
    assert "## Recursion budget" not in trimmed


def test_codeact_capabilities_keeps_the_general_framing():
    trimmed = hh_smol_operator._capabilities(for_codeact=True)
    assert "## The loop: read → think → act" in trimmed
    assert "## Stop conditions" in trimmed


def test_native_capabilities_unaffected():
    # for_codeact=False (the native/Claude-skill path) must be byte-identical to
    # before -- this is real, accurate CLI syntax for those callers.
    full = hh_smol_operator._capabilities(for_codeact=False)
    assert "watch --for" in full
    assert "## Verbs" in full
