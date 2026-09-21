"""WiFi Pineapple Pager adapter — multiplexed SSH transport + dynamic payloads.

Transport is `SshConn` (one ControlMaster per session over the `pager` alias, which
pp-proxy auto-discovers USB/WiFi/LAN). Read-only verbs (status/scan/clients/loot)
are open to any room member; payload transfer/execution is gated (push is owner-ish
via the bridge, run is ARMED).

Payloads are DISCOVERED, not hardcoded: the host-side c2 library
(`~/coding/hardware/pineapple-pager-c2/payloads/`, minus the hak5 reference mirror) is
the pushable set (name + description parsed from each `# Title:`/`# Description:`
header); the device's `/root/payloads` tree is the installed/runnable set. The merged
catalog IS the allowlist — `push`/`run`/`info` only accept a discovered name. A payload
pushed to the device shows up on the next `payloads --refresh`.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import AsyncIterator

from .base import DeviceAdapter, Verb, safe_args
from .ssh_conn import SshConn

HOST_LIB = os.path.expanduser("~/coding/hardware/pineapple-pager-c2/payloads")
DEVICE_PUSH_DIR = "/root/payloads/user"          # where pushed payloads land
DEVICE_LOOT = "/root/loot"
LOCAL_LOOT = os.path.expanduser("~/loot/pineapple")

_TITLE = re.compile(r"^#\s*Title:\s*(.+)$", re.I | re.M)
_DESC = re.compile(r"^#\s*Description:\s*(.+)$", re.I | re.M)

# Hak5 Pineapple payloads call framework functions (LOG/LED/PROMPT/CONFIRMATION_DIALOG
# /LIST_PICKER/…) that a bare `sh` doesn't define — so they error out or hang, and the
# operator sees nothing. This POSIX-sh shim defines them so a payload actually RUNS under
# the bridge: human-facing messages go to stderr (streamed to the room, but NOT captured
# by a payload's `x=$(CONFIRMATION_DIALOG …)`), and each input picker returns a sensible
# default on stdout (auto-answered; interactive room-prompts are a later enhancement).
HH_SHIM = r"""
TITLE(){ echo "  == $* ==" >&2; }
LOG(){ echo "  . $*" >&2; }
LED(){ :; }
ALERT(){ echo "  ! $*" >&2; }
START_SPINNER(){ :; }
STOP_SPINNER(){ :; }
BATTERY_PERCENT(){ cat /sys/class/power_supply/*/capacity 2>/dev/null | head -1 || echo 100; }
PROMPT(){ echo "  i $*" >&2; }
CONFIRMATION_DIALOG(){ echo "  ? $* -> [auto-YES]" >&2; echo 1; }
LIST_PICKER(){ _t="$1"; shift; echo "  ? $_t -> [auto: $1]" >&2; echo "$1"; }
NUMBER_PICKER(){ echo "  ? $1 -> [auto: ${4:-0}]" >&2; echo "${4:-0}"; }
TEXT_PICKER(){ echo "  ? $1 -> [auto: ${2:-}]" >&2; echo "${2:-}"; }
QUACK(){ :; }
export HH_SHIM=1
"""


class PagerAdapter(DeviceAdapter):
    KIND = "WiFi Pineapple Pager"

    def __init__(self, persona: str = "pager", alias: str = "pager"):
        self.conn = SshConn(alias=alias)
        self._catalog: dict[str, dict] = {}
        super().__init__(persona)

    async def health(self) -> tuple[bool, str]:
        return await self.conn.health()

    async def open_shell(self, rows: int = 40, cols: int = 120):
        return self.conn.open_pty(rows=rows, cols=cols)

    def register_verbs(self) -> None:
        self.verb(Verb("status", "kernel, uptime, wifi interfaces, pineapple presence",
                       self._status))
        self.verb(Verb("scan", "recon: nearby access points (read-only)",
                       self._scan, max_args=1))
        self.verb(Verb("clients", "associated wifi clients (read-only)", self._clients))
        self.verb(Verb("loot", "list captured loot on the device", self._loot))
        self.verb(Verb("payloads", "list payloads (columnar); 'payloads <text>' filters, "
                       "'payloads refresh' rescans", self._payloads, max_args=1))
        self.verb(Verb("info", "show a payload's details", self._info,
                       min_args=1, max_args=1))
        self.verb(Verb("push", "send a host-library payload to the device",
                       self._push, owner_only=True, min_args=1, max_args=1))
        self.verb(Verb("pull", "pull loot from the device to the operator",
                       self._pull, owner_only=True, max_args=1))
        self.verb(Verb("run", "launch an installed payload (transmits/executes)",
                       self._run, armed=True, min_args=1, max_args=1))
        self.verb(Verb("stop", "halt a running payload: stop <name> (or all)",
                       self._stop, owner_only=True, max_args=1))

    # ── discovery ────────────────────────────────────────────────────────────
    def _host_catalog(self) -> dict[str, dict]:
        cat: dict[str, dict] = {}
        base = Path(HOST_LIB)
        if not base.is_dir():
            return cat
        for p in base.rglob("payload.sh"):
            if "hak5-library" in p.parts:      # a reference mirror, not the curated set
                continue
            name = p.parent.name
            try:
                head = p.read_text(errors="replace")[:2000]
            except OSError:
                head = ""
            m = _TITLE.search(head)
            title = m.group(1).strip() if m else name
            d = _DESC.search(head)
            desc = d.group(1).strip() if d else title
            # On a leaf-name collision keep the first; disambiguate the rest by parent.
            key = name if name not in cat else f"{p.parent.parent.name}-{name}"
            cat[key] = {"source": "host", "path": str(p), "title": title, "desc": desc}
        return cat

    async def _device_catalog(self) -> dict[str, dict]:
        cat: dict[str, dict] = {}
        async for line in self.conn.exec(
                ["find /root/payloads -maxdepth 5 -name payload.sh 2>/dev/null"], timeout=25):
            line = line.strip()
            if line.endswith("/payload.sh"):
                name = line.rsplit("/", 2)[-2]
                cat[name] = {"source": "device", "device_path": line,
                             "title": name, "desc": "(installed on device)"}
        return cat

    async def refresh(self) -> dict[str, dict]:
        merged = dict(self._host_catalog())
        for name, v in (await self._device_catalog()).items():
            if name in merged:
                merged[name] = {**merged[name], "source": "both",
                                "device_path": v["device_path"]}
            else:
                merged[name] = v
        self._catalog = merged
        return merged

    async def _catalog_ready(self) -> dict[str, dict]:
        if not self._catalog:
            await self.refresh()
        return self._catalog

    # ── read-only verbs ──────────────────────────────────────────────────────
    async def _status(self, args: list[str]) -> AsyncIterator[str]:
        script = (
            "echo '# kernel:'; uname -a; echo '# uptime:'; uptime; "
            "echo '# interfaces:'; iw dev 2>/dev/null | awk '/Interface/{print \"  \"$2}'; "
            "echo '# pineapple:'; { [ -x /pineapple/pineapple ] && echo '  present'; } || echo '  absent'")
        async for line in self.conn.exec([script]):
            yield line

    async def _scan(self, args: list[str]) -> AsyncIterator[str]:
        script = ("for i in $(iw dev 2>/dev/null | awk '/Interface/{print $2}'); do "
                  "iwinfo \"$i\" scan 2>/dev/null; done | "
                  "grep -E 'ESSID|Signal|Address|Channel' | head -80")
        async for line in self.conn.exec([script], timeout=45):
            yield line

    async def _clients(self, args: list[str]) -> AsyncIterator[str]:
        script = ("for i in $(iw dev 2>/dev/null | awk '/Interface/{print $2}'); do "
                  "d=$(iw dev \"$i\" station dump 2>/dev/null); "
                  "[ -n \"$d\" ] && { echo \"# $i:\"; echo \"$d\" | grep -E 'Station|signal:|connected time'; }; "
                  "done; :")
        async for line in self.conn.exec([script]):
            yield line

    async def _loot(self, args: list[str]) -> AsyncIterator[str]:
        script = (f"[ -d {DEVICE_LOOT} ] && {{ echo '# {DEVICE_LOOT}:'; "
                  f"find {DEVICE_LOOT} -maxdepth 2 -type f 2>/dev/null | head -40; }} || echo 'no loot dir'")
        async for line in self.conn.exec([script]):
            yield line

    # ── payload verbs ────────────────────────────────────────────────────────
    async def _payloads(self, args: list[str]) -> AsyncIterator[str]:
        # `payloads` — full list; `payloads refresh` — rescan; `payloads <text>` — filter.
        flt = ""
        if args:
            if args[0] == "refresh":
                await self.refresh()
            else:
                flt = args[0].lower()
        cat = await self._catalog_ready()
        if not cat:
            yield "no payloads discovered (host library missing + device empty)"
            return

        def sel(sources: tuple) -> list[str]:
            names = sorted(n for n, v in cat.items() if v["source"] in sources)
            return [n for n in names if not flt or flt in n.lower()]

        def columns(names: list[str], marker: str, per: int = 3, w: int = 28):
            # each cell gets a built-in 2-space trailer, so a long name that overruns
            # its column still keeps a gap before the next marker (no abutting).
            for i in range(0, len(names), per):
                cells = [f"{marker} {n}  ".ljust(w) for n in names[i:i + per]]
                yield "  " + "".join(cells).rstrip()

        installed = sel(("device", "both"))
        avail = sel(("host",))
        tag = f" matching '{flt}'" if flt else ""
        if installed:
            yield f"# installed on device ({len(installed)}{tag}) — `run <name>`:"
            for ln in columns(installed, "●"):
                yield ln
        if avail:
            yield f"# available to push ({len(avail)}{tag}) — `push <name>`:"
            for ln in columns(avail, "○"):
                yield ln
        if not installed and not avail:
            yield f"(no payloads{tag})"
        elif not flt:
            yield "tip: filter with `payloads <text>` · details with `info <name>`"

    async def _info(self, args: list[str]) -> AsyncIterator[str]:
        cat = await self._catalog_ready()
        v = cat.get(args[0])
        if not v:
            yield f"no payload named {args[0]!r} — `payloads` to list"
            return
        yield f"name: {args[0]}"
        yield f"source: {v['source']}"
        yield f"title: {v.get('title', '')}"
        yield f"desc: {v.get('desc', '')}"
        if v.get("path"):
            yield f"host: {v['path']}"
        if v.get("device_path"):
            yield f"device: {v['device_path']}"

    async def _push(self, args: list[str]) -> AsyncIterator[str]:
        name = args[0]
        cat = await self._catalog_ready()
        v = cat.get(name)
        if not v or not v.get("path"):
            yield f"no host-library payload named {name!r} — `payloads` lists pushable ones"
            return
        dest_dir = f"{DEVICE_PUSH_DIR}/{name}"
        # name is token-validated; single-quote into the mkdir (no metachars possible).
        async for line in self.conn.exec([f"mkdir -p '{dest_dir}'"]):
            yield line
        yield f"# pushing {name} → {dest_dir}/payload.sh"
        got = False
        async for line in self.conn.push(v["path"], f"{dest_dir}/payload.sh"):
            got = True
            yield line
        # verify it landed
        async for line in self.conn.exec([f"[ -f '{dest_dir}/payload.sh' ] && echo '✓ pushed' || echo '✖ push failed'"]):
            yield line
        self._catalog = {}   # force a rescan so `run` sees it installed

    async def _pull(self, args: list[str]) -> AsyncIterator[str]:
        os.makedirs(LOCAL_LOOT, exist_ok=True)
        remote = f"{DEVICE_LOOT}/{args[0]}" if args else f"{DEVICE_LOOT}/"
        if args:
            safe_args(args)
        yield f"# pulling {remote} → {LOCAL_LOOT}/"
        async for line in self.conn.pull(remote, LOCAL_LOOT + "/", timeout=180):
            yield line
        yield f"✓ loot in {LOCAL_LOOT}/"

    async def _run(self, args: list[str]) -> AsyncIterator[str]:
        name = args[0]
        cat = await self._catalog_ready()
        v = cat.get(name)
        if not v or v["source"] == "host":
            yield (f"{name!r} is not installed on the device — `push {name}` first."
                   if v else f"no payload named {name!r}.")
            return
        import base64 as _b64
        shim_b64 = _b64.b64encode(HH_SHIM.encode()).decode()
        # Launch DETACHED (setsid) so a long-running/monitor payload persists and NEVER
        # blocks the bridge; capture output to a log, tail it for a bounded window, then
        # report completed vs still-running. The shim is deployed via base64 (no quoting
        # hazards). `name` is token-validated → safe to single-quote.
        script = (
            f"n='{name}'\n"
            "p=$(find /root/payloads /pineapple/payloads -maxdepth 5 -type d -name \"$n\" 2>/dev/null | head -1)\n"
            "[ -n \"$p\" ] && [ -f \"$p/payload.sh\" ] || { echo \"no payload '$n' on device\"; exit 3; }\n"
            f"echo '{shim_b64}' | base64 -d > /tmp/hh_shim.sh 2>/dev/null\n"
            "log=/tmp/hh_run_$n.log; pidf=/tmp/hh_run_$n.pid; : > \"$log\"\n"
            "setsid sh -c '. /tmp/hh_shim.sh; cd \"$0\" 2>/dev/null; . \"$0/payload.sh\"; "
            "echo \"< exited (code $?)\"' \"$p\" >\"$log\" 2>&1 &\n"
            "pid=$!; echo $pid > \"$pidf\"\n"
            "echo \"> launched $p (pid $pid)\"\n"
            "i=0; while [ $i -lt 22 ]; do kill -0 $pid 2>/dev/null || break; sleep 1; i=$((i+1)); done\n"
            "cat \"$log\"\n"
            "if kill -0 $pid 2>/dev/null; then echo \"@@RUNNING pid=$pid@@\"; "
            "else echo \"@@DONE@@\"; rm -f \"$pidf\"; fi\n"
        )
        async for line in self.conn.exec([script], timeout=40):
            yield line

    async def _stop(self, args: list[str]) -> AsyncIterator[str]:
        # Halt a running payload (or all): kill the detached session (negative pid =
        # process group from setsid), then clean up the pid file.
        target = args[0] if args else "*"
        script = (
            f"any=0; for f in /tmp/hh_run_{target}.pid; do [ -f \"$f\" ] || continue; any=1; "
            "pid=$(cat \"$f\"); nm=$(basename \"$f\" .pid | sed 's/^hh_run_//'); "
            "kill -- -\"$pid\" 2>/dev/null || kill \"$pid\" 2>/dev/null; "
            "echo \"stopped $nm (pid $pid)\"; rm -f \"$f\"; done; "
            "[ $any = 0 ] && echo 'no running payloads'; :")
        async for line in self.conn.exec([script]):
            yield line
