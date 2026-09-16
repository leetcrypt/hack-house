"""Refuse to run a lab against somebody else's process.

Every service these labs start answers on a fixed localhost port, and a leftover
one from an earlier session answers just as cheerfully as the one the lab meant
to spawn. The spawn then fails to bind, silently, and the lab measures the stale
process instead — reporting a green run of code that is not the code under test.

That is not hypothetical. `verify_p1` asserted a room appears in `GET /api/rooms`
long after rooms went unlisted-by-default; it stayed green for weeks because a
pre-hardening relay had been squatting on port 8090 the whole time. The chat port
had the same problem, from a server left over with a different password.

So bind-probe first and die loudly. A lab that cannot get its own ports should
say so, not produce a result.
"""
import socket


def require_free(*ports: tuple[str, str, str]) -> None:
    """Each argument is (port, what_runs_there, env_var_to_override).

    Raises SystemExit naming the override, so a developer with a busy box can
    move the lab rather than kill someone else's session.
    """
    for port, what, var in ports:
        probe = socket.socket()
        # The same option the servers bind with. Without it a TIME_WAIT socket
        # left by the previous run reads as "occupied" and this guard becomes
        # stricter than the thing it is guarding.
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", int(port)))
        except OSError:
            raise SystemExit(
                f"✖ port {port} is already in use — a stale {what} would answer "
                f"for us and this lab would grade it instead. Free it or set {var}.")
        finally:
            probe.close()
