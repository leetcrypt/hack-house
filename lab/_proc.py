"""Spawn lab children that cannot outlive the lab.

Every lab starts servers with `subprocess.Popen` and cleans them up in a
`finally:`. That covers the normal exit and an exception — but not the ways a
lab actually dies in practice: `timeout 400 python lab/verify_p3.py` sends the
lab SIGTERM, which terminates the interpreter *without unwinding*, so `finally:`
never runs. Same for SIGKILL, an OOM kill, or a closed terminal. The children
are reparented to init and stay listening forever.

That is where this box's thirty-five abandoned chat servers came from, some of
them fifty days old and bound to `0.0.0.0` with a known test password. And a
leftover server is not merely untidy: `_ports.py` exists because one of them
answered for a lab and the lab graded it instead of the code under test.

`PR_SET_PDEATHSIG` moves the guarantee into the kernel. The child asks to be
signalled when *its parent* dies, whatever the manner of death, so no cleanup
code has to run for the tree to collapse. Cleanup handlers stay — they shut
things down politely — but they are no longer the only thing standing between a
crashed lab and a process that outlives the week.
"""
from __future__ import annotations

import ctypes
import platform
import signal

_PR_SET_PDEATHSIG = 1


def die_with_parent():
    """A `preexec_fn` for `Popen` that makes the kernel reap this child when the
    spawning process dies. Returns None off Linux, where the call is a no-op and
    the caller falls back to its own cleanup."""
    if platform.system() != "Linux":
        return None

    def _preexec():
        # Runs in the forked child, between fork and exec.
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(
            _PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0)
        # A parent that died between our fork and this line leaves the signal
        # already spent, so check rather than trust it.
        import os
        if os.getppid() == 1:
            os._exit(1)

    return _preexec
