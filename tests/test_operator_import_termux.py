"""Guard the operator's lightweight import surface (docs/termux-operator.md P0).

A phone/Termux operator installs only the operator deps (requests, rich,
websockets, cryptography, srp) — never the server stack (sanic, pydantic) — and
`srp` may be unbuildable there (pure-Python fallback). So `python -m
cmd_chat.operator` must import with srp/sanic/pydantic ALL absent. This is easy
to regress: any eager `import srp`/sanic on the operator path breaks it. Run the
check in a subprocess so blocking modules can't pollute the test interpreter.
"""

import subprocess
import sys


_SNIPPET = """
import sys
# Simulate a Termux operator box: no srp C-ext build, no server-only deps.
for missing in ("srp", "sanic", "sanic_ext", "pydantic"):
    sys.modules[missing] = None  # 'import X' now raises ImportError

import cmd_chat.operator                       # must not need srp/sanic/pydantic
assert hasattr(cmd_chat.operator, "OperatorBridge")

import cmd_chat.client.client as c             # client must fall back to the shim
assert c.srp.__name__.endswith("_srp_pure"), c.srp.__name__
print("OK")
"""


def test_operator_imports_without_srp_or_server_deps():
    proc = subprocess.run(
        [sys.executable, "-c", _SNIPPET],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        "operator import failed without srp/sanic/pydantic:\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert proc.stdout.strip().endswith("OK"), proc.stdout
