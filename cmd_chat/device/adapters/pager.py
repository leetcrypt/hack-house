"""WiFi Pineapple Pager adapter.

Reaches the device over the `pager` SSH alias (`~/.ssh/config` → `pp-proxy.sh`
auto-discovers USB 172.16.52.1 / WiFi-AP 172.16.42.1 / LAN; key ~/.ssh/pineapple-pager).
Curated verbs shell to the device with FIXED remote scripts (no chat text is ever
interpolated into a shell string); the one arg'd verb passes its (already
token-validated) name positionally so it lands as "$1", never inside the script body.

Read-only verbs (status/scan/clients/loot/payloads) are open to any room member.
`run` transmits/executes and is ARMED — it fires only after an authorized operator
arms the device via the bridge.
"""
from __future__ import annotations

from typing import AsyncIterator

from .base import DeviceAdapter, Verb, stream_exec

_SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=12",
             "-o", "StrictHostKeyChecking=accept-new"]


def _ssh(alias: str, remote: list[str]) -> list[str]:
    return ["ssh", *_SSH_OPTS, alias, *remote]


class PagerAdapter(DeviceAdapter):
    KIND = "WiFi Pineapple Pager"

    def __init__(self, persona: str = "pager", alias: str = "pager"):
        self.alias = alias
        super().__init__(persona)

    async def health(self) -> tuple[bool, str]:
        online, detail = False, "ssh pager unreachable (USB/WiFi/LAN down?)"
        async for line in stream_exec(
                _ssh(self.alias, ["echo __ok__; uname -sm; uptime 2>/dev/null"]),
                timeout=15):
            if "__ok__" in line:
                online = True
            elif online and line.strip():
                detail = line.strip()
                break
        return online, detail

    def register_verbs(self) -> None:
        self.verb(Verb("status", "kernel, uptime, wifi interfaces, pineapple presence",
                       self._status))
        self.verb(Verb("scan", "recon: nearby access points (read-only active scan)",
                       self._scan, max_args=1))
        self.verb(Verb("clients", "associated wifi clients (read-only)",
                       self._clients))
        self.verb(Verb("loot", "list captured loot on the device",
                       self._loot))
        self.verb(Verb("payloads", "list installed payloads",
                       self._payloads))
        self.verb(Verb("run", "run a payload by name (transmits/executes)",
                       self._run, armed=True, min_args=1, max_args=1))

    # ── runners ──────────────────────────────────────────────────────────────
    async def _status(self, args: list[str]) -> AsyncIterator[str]:
        script = (
            "echo '# kernel:'; uname -a; "
            "echo '# uptime:'; uptime; "
            "echo '# interfaces:'; iw dev 2>/dev/null | awk '/Interface/{print \"  \"$2}'; "
            "echo '# pineapple:'; { [ -x /pineapple/pineapple ] && echo '  /pineapple/pineapple present'; } || echo '  absent'")
        async for line in stream_exec(_ssh(self.alias, [script])):
            yield line

    async def _scan(self, args: list[str]) -> AsyncIterator[str]:
        # Passive-ish recon: iwinfo scan across every wifi interface, keep the
        # ESSID/Signal/Address lines. Active scan (probe requests) is standard and
        # not an attack, so this stays a read-only verb.
        script = (
            "for i in $(iw dev 2>/dev/null | awk '/Interface/{print $2}'); do "
            "iwinfo \"$i\" scan 2>/dev/null; done | "
            "grep -E 'ESSID|Signal|Address|Channel' | head -80")
        async for line in stream_exec(_ssh(self.alias, [script]), timeout=45):
            yield line

    async def _clients(self, args: list[str]) -> AsyncIterator[str]:
        script = (
            "for i in $(iw dev 2>/dev/null | awk '/Interface/{print $2}'); do "
            "d=$(iw dev \"$i\" station dump 2>/dev/null); "
            "[ -n \"$d\" ] && { echo \"# $i:\"; echo \"$d\" | grep -E 'Station|signal:|connected time'; }; "
            "done; :")
        async for line in stream_exec(_ssh(self.alias, [script])):
            yield line

    async def _loot(self, args: list[str]) -> AsyncIterator[str]:
        script = (
            "for d in /root/loot /pineapple/loot /tmp/loot /root/handshakes; do "
            "[ -d \"$d\" ] && { echo \"# $d:\"; ls -1t \"$d\" 2>/dev/null | head -30; }; "
            "done; :")
        async for line in stream_exec(_ssh(self.alias, [script])):
            yield line

    async def _payloads(self, args: list[str]) -> AsyncIterator[str]:
        script = (
            "for d in /root/payloads /pineapple/payloads; do "
            "[ -d \"$d\" ] && { echo \"# $d:\"; find \"$d\" -maxdepth 2 -name payload.sh "
            "2>/dev/null | sed 's#/payload.sh##;s#.*/##' | head -40; }; done; :")
        async for line in stream_exec(_ssh(self.alias, [script])):
            yield line

    async def _run(self, args: list[str]) -> AsyncIterator[str]:
        # ARMED. Resolve <name> to a payload.sh under the standard dirs (name is a
        # validated token; it arrives as $1, never spliced into the script body),
        # then execute it on the device. Refuses if no matching payload exists.
        # name is token-validated (no shell metachars, no quotes) so single-quoting
        # it into the remote script is injection-safe; find -name matches a basename
        # so a '/'-bearing name simply matches nothing.
        name = args[0]
        script = (
            f"n='{name}'; p=''; "
            'for d in /root/payloads /pineapple/payloads; do '
            'f=$(find "$d" -maxdepth 2 -type d -name "$n" 2>/dev/null | head -1); '
            '[ -n "$f" ] && [ -f "$f/payload.sh" ] && { p="$f/payload.sh"; break; }; done; '
            '[ -z "$p" ] && { echo "no payload named $n"; exit 3; }; '
            'echo "# running $p"; sh "$p" 2>&1; echo "# exit $?"')
        async for line in stream_exec(_ssh(self.alias, [script]), timeout=120):
            yield line
