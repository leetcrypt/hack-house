#!/usr/bin/env python3
"""hh-sbx-egress — the CANONICAL sandbox egress gateway (single source of truth).

Both the Python operator (`cmd_chat/operator/egress_gw.py`, which shells out to this)
and the Rust TUI (`hh/src/sbx.rs`, which shells out to this) call this one script, so
the security-critical nft rulesets + tor setup live in exactly one place. Self-contained
(stdlib + subprocess podman/docker), so it runs under the system `python3` with no venv
or package import — mirrors the sidecar-gateway design proven in
research/sandbox-egress-posture-2026-09-07/SPIKE.md.

Commands:
  up <sandbox> --egress {local|scope|tor} [--engine podman]
       create the gateway for <sandbox> and apply its ruleset.
       prints one line: `NETARG=--network=container:hh-egw-<sandbox>` on success
       (the flag the caller adds to its own `podman run`), or `REFUSED: <reason>`.
  postjoin <sandbox> --egress <mode> [--engine]   sandbox-side setup (tor: resolv.conf)
  down <sandbox> [--engine]                        remove the gateway

Only `local`/`scope`/`tor` use a gateway; `guard`/`open`/`none` are handled by the
caller (they need no sidecar). Fail-closed: on any error the gateway is removed and
`REFUSED:` is printed, so the caller never launches an unfiltered sandbox.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

GW_IMAGE = "localhost/hh-egress-gw"
GW_LABEL = "hh.egress-gw"
TOR_NET = "hh-egress-net"
# Hosts always allowed through in local/scope mode (e.g. your tailnet Ollama).
# Set $HH_SBX_ALLOW to a comma-separated list; empty by default so no infra
# address is baked into the source.
DEFAULT_ALLOW = [h.strip() for h in os.environ.get("HH_SBX_ALLOW", "").split(",") if h.strip()]
INTERNAL_CIDRS = ["192.168.0.0/16", "10.0.0.0/8", "172.16.0.0/12",
                  "100.64.0.0/10", "169.254.0.0/16"]
TOR_BOOTSTRAP_TIMEOUT = 75.0

_TORRC = (
    "TransPort 0.0.0.0:9040\n"
    "DNSPort 127.0.0.1:53\n"
    "AutomapHostsOnResolve 1\n"
    "VirtualAddrNetworkIPv4 10.192.0.0/10\n"
    "User tor\n"
    "DataDirectory /var/lib/tor\n"
)
# tor: drop real internal (pivot block; 10.0.0.0/8 split so tor's own 10.192.0.0/10
# virtual range survives), redirect all TCP to the TransPort, drop non-TCP leaks.
_TOR_NFT = (
    "table inet tor {\n"
    "  chain output {\n"
    "    type nat hook output priority -100; policy accept;\n"
    '    meta skuid "tor" return\n'
    '    oifname "lo" return\n'
    "    ip daddr { 192.168.0.0/16, 172.16.0.0/12, 100.64.0.0/10, 169.254.0.0/16,"
    " 10.0.0.0/10, 10.64.0.0/10, 10.128.0.0/10 } drop\n"
    "    tcp dport 1-65535 redirect to :9040\n"
    "  }\n"
    "  chain leakblock {\n"
    "    type filter hook output priority 0; policy accept;\n"
    '    meta skuid "tor" return\n'
    '    oifname "lo" return\n'
    "    meta nfproto ipv6 drop\n"   # tor TransPort is IPv4-only — drop all IPv6 (no leak around tor)
    "    meta l4proto != tcp drop\n"
    "  }\n"
    "}\n"
)


def gw_name(sandbox: str) -> str:
    return f"hh-egw-{sandbox}"


def scope_allow(scope: str | None) -> list[str]:
    extra = [h.strip() for h in (scope or "").split(",") if h.strip()]
    return DEFAULT_ALLOW + extra


def pivot_block_ruleset(allow: list[str]) -> str:
    allows = "\n".join(f"    ip daddr {a} accept" for a in allow)
    drops = ", ".join(INTERNAL_CIDRS)
    # Drop ALL IPv6 egress: the allowlist + pivot drops are IPv4-only, and the default
    # (pasta) network hands the sandbox IPv6 — without this a guest pivots into the
    # tailnet/LAN over IPv6 (red-team finding 2026-09-07). Loopback IPv6 is covered by
    # `oifname lo accept`. IPv6-only external targets are the accepted trade for the block.
    return ("table inet filt {\n  chain output {\n"
            "    type filter hook output priority 0; policy accept;\n"
            '    oifname "lo" accept\n'
            f"{allows}\n"
            f"    ip daddr {{ {drops} }} drop\n"
            "    meta nfproto ipv6 drop\n  }\n}\n")


def _run(argv, stdin=None, timeout=90):
    return subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=timeout)


def _rm(engine, name):
    _run([engine, "rm", "-f", name], timeout=30)


def cmd_up(a) -> int:
    engine, name, mode = a.engine, gw_name(a.sandbox), a.egress
    if mode not in ("local", "scope", "tor"):
        print(f"REFUSED: egress mode '{mode}' needs no gateway")
        return 2
    _rm(engine, name)
    run = [engine, "run", "-d", "--init", "--name", name, "--cap-add=NET_ADMIN",
           "--label", f"{GW_LABEL}=1"]
    if mode == "tor":
        _run([engine, "network", "create", TOR_NET], timeout=30)  # ignore "exists"
        run += ["--network", TOR_NET]
    run += [GW_IMAGE]
    r = _run(run)
    if r.returncode != 0:
        print("REFUSED: gateway run failed: " + (r.stderr or r.stdout).strip()[:160])
        return 1

    def apply(ruleset):
        _run([engine, "exec", "-i", name, "sh", "-c", "cat > /rules.nft"], stdin=ruleset)
        return _run([engine, "exec", name, "nft", "-f", "/rules.nft"])

    if mode in ("local", "scope"):
        r2 = apply(pivot_block_ruleset(scope_allow(a.scope)))
        if r2.returncode != 0:
            _rm(engine, name)
            print("REFUSED: ruleset apply failed: " + (r2.stderr or "").strip()[:160])
            return 1
    else:  # tor
        _run([engine, "exec", name, "sh", "-c",
              "mkdir -p /var/lib/tor && chown tor:tor /var/lib/tor && chmod 700 /var/lib/tor"])
        _run([engine, "exec", "-i", name, "sh", "-c", "cat > /torrc"], stdin=_TORRC)
        _run([engine, "exec", "-d", name, "sh", "-c", "tor -f /torrc > /tor.out 2>&1"])
        deadline = time.monotonic() + TOR_BOOTSTRAP_TIMEOUT
        up = False
        while time.monotonic() < deadline:
            g = _run([engine, "exec", name, "sh", "-c",
                      "grep -q 'Bootstrapped 100' /tor.out && echo OK || true"], timeout=15)
            if "OK" in (g.stdout or ""):
                up = True
                break
            time.sleep(2.0)
        if not up:
            _rm(engine, name)
            print(f"REFUSED: tor did not bootstrap within {int(TOR_BOOTSTRAP_TIMEOUT)}s")
            return 1
        r2 = apply(_TOR_NFT)
        if r2.returncode != 0:
            _rm(engine, name)
            print("REFUSED: tor redirect ruleset failed: " + (r2.stderr or "").strip()[:160])
            return 1
    print(f"NETARG=--network=container:{name}")
    return 0


def cmd_postjoin(a) -> int:
    # Point the sandbox resolver at a reachable nameserver. tor: tor's DNSPort on
    # loopback. local/scope: a PUBLIC IPv4 resolver (1.1.1.1) — the host's own
    # resolvers (169.254.x pasta stub, Proton 10.x) sit in the dropped internal
    # ranges, so without this DNS only resolves over IPv6, which the gateway now
    # drops (red-team finding 2026-09-07).
    if a.egress == "tor":
        ns = "127.0.0.1"
    elif a.egress in ("local", "scope"):
        ns = "1.1.1.1"
    else:
        return 0
    _run([a.engine, "exec", a.sandbox, "sh", "-c",
          f"printf 'nameserver {ns}\\n' > /etc/resolv.conf"])
    return 0


def cmd_down(a) -> int:
    _rm(a.engine, gw_name(a.sandbox))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("up", "postjoin", "down"):
        p = sub.add_parser(c)
        p.add_argument("sandbox")
        p.add_argument("--egress", default="local")
        p.add_argument("--scope", default=None, help="extra allowlist hosts (comma-sep) for scope")
        p.add_argument("--engine", default="podman")
    a = ap.parse_args()
    return {"up": cmd_up, "postjoin": cmd_postjoin, "down": cmd_down}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
