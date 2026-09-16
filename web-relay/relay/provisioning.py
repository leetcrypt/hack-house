"""Where the room-provisioning secret comes from.

The gate on `POST /api/rooms` has always existed and always worked; it was simply
off unless someone set `RELAY_PROVISION_SECRET`. That is the wrong default for
software handed to strangers, and it stops being merely theoretical the moment an
onboarding page documents the endpoint: the protection room creation actually
leaned on was that nobody knew it was there.

So the default inverts. Unset now means "generate one and keep it", not "let
anyone in" — and there is no value of the variable that reopens the endpoint,
including the empty string, which is what a half-finished `export` leaves behind.

⚠ Why this cannot instead key off the caller being local: every tunnel terminates
on the box and forwards from 127.0.0.1, so a loopback peer says nothing about who
sent the request. `cloudflared` at least adds a real-IP header, but `tailscale
funnel` — which the onboarding page recommends — adds nothing, making a remote
request indistinguishable from a local one. Any "trust localhost" shortcut here
is a gate that passes the whole internet.

The secret is persisted, unlike anything else this relay holds. That is a
deliberate exception, not an erosion of the RAM-only rule: room state and its
tokens must never touch disk because they are the session, while this is a
long-lived operator credential closer to an SSH host key. Persisting it is also
what lets the publisher find it without configuration, and what keeps a relay
restart from locking out the publisher that survived it.
"""
from __future__ import annotations

import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

SECRET_FILENAME = "relay-secret"


def default_state_dir() -> Path:
    """`$RELAY_STATE_DIR`, else the XDG state dir, else `~/.local/state`."""
    override = os.environ.get("RELAY_STATE_DIR")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".local" / "state"
    return root / "hack-house"


@dataclass(frozen=True)
class Provisioning:
    """The resolved secret plus where it came from, so startup can say so."""
    secret: str
    origin: str          # env | state | generated | ephemeral
    path: Path | None = None
    error: str | None = None

    @property
    def persisted(self) -> bool:
        return self.origin in ("state", "generated")


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except Exception:
        return ""


def _write(path: Path, secret: str) -> str | None:
    """Write `0600`, creating parents. Returns an error string, or None on success."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Create with the right mode from the outset rather than chmod-ing after:
        # a world-readable window, however brief, is the whole thing we are
        # avoiding. `O_EXCL` keeps two relays starting together from interleaving.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IRUSR | stat.S_IWUSR)
        try:
            os.write(fd, (secret + "\n").encode())
        finally:
            os.close(fd)
        return None
    except FileExistsError:
        # Someone won the race; their secret is authoritative.
        return "exists"
    except Exception as e:  # noqa: BLE001
        return str(e)


def resolve(state_dir: Path | None = None) -> Provisioning:
    """Settle on the provisioning secret for this process.

    Order: explicit env, then the state file, then generate and persist one. A
    state directory that cannot be written yields a working secret that lives
    only in RAM — the endpoint stays shut, which matters more than convenience,
    and the caller is expected to print it so the operator can pass it on.
    """
    from_env = os.environ.get("RELAY_PROVISION_SECRET", "").strip()
    if from_env:
        return Provisioning(from_env, "env")

    directory = state_dir or default_state_dir()
    path = directory / SECRET_FILENAME

    existing = _read(path)
    if existing:
        return Provisioning(existing, "state", path)

    secret = secrets.token_urlsafe(32)
    err = _write(path, secret)
    if err == "exists":
        # Lost the race — adopt whatever landed, so both processes agree.
        adopted = _read(path)
        if adopted:
            return Provisioning(adopted, "state", path)
        err = "unreadable after create"
    if err:
        return Provisioning(secret, "ephemeral", path, error=err)
    return Provisioning(secret, "generated", path)


def load_local_secret(state_dir: Path | None = None) -> str:
    """Read the secret a same-box relay left behind, or "" if there is none.

    For the publisher and local tooling: same machine, same user, so the file is
    the handshake and nothing needs configuring. Never generates — a caller that
    is not the relay must not invent a credential the relay has not heard of.
    """
    from_env = os.environ.get("HH_WEB_PROVISION_SECRET", "").strip()
    if from_env:
        return from_env
    return _read((state_dir or default_state_dir()) / SECRET_FILENAME)
