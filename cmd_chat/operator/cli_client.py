"""Tiny synchronous AF_UNIX client for the operator control socket.

The control protocol is newline-delimited JSON: the CLI sends one request
object terminated by ``\\n`` and reads one response object terminated by ``\\n``.
Kept dependency-free and asyncio-free so the CLI verbs stay fast and simple.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path


class BridgeUnreachable(RuntimeError):
    """The daemon socket is missing or not answering."""


def request(sock_path: str | Path, obj: dict, read_timeout: float = 35.0) -> dict:
    """Send one request, return the decoded response.

    ``read_timeout`` must exceed a long-poll ``read --wait --timeout`` window so
    the client doesn't give up before the daemon replies.
    """
    path = str(sock_path)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(read_timeout)
    try:
        try:
            s.connect(path)
        except (FileNotFoundError, ConnectionRefusedError) as e:
            raise BridgeUnreachable(f"no bridge at {path} ({e.__class__.__name__})") from e
        s.sendall((json.dumps(obj) + "\n").encode())
        buf = bytearray()
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf.extend(chunk)
        line, _, _ = bytes(buf).partition(b"\n")
        if not line:
            raise BridgeUnreachable("bridge closed the connection without replying")
        return json.loads(line.decode())
    finally:
        s.close()
