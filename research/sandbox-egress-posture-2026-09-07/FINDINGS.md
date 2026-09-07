# Sandbox egress & host-exposure posture — measured findings (2026-09-07)

**Scope:** what a hack-house shared sandbox can reach on the network, what outbound
IP it presents, and what a room guest with sandbox drive is therefore able to see /
do to the host and its networks. Measured live on `trillsec` (rootless podman + pasta,
host behind ProtonVPN + Tailscale, two home LANs). Motivated by: *when you host a room
and hand `/grant` drive to other people, the sandbox is a shell inside your perimeter
wearing your egress identity.*

## Method

- Launched throwaway `python:3.11-slim` containers with the **default** sandbox network
  (what `sbx launch` / `/sbx podman` produce) and probed reachability + egress from
  **inside** the container with stdlib `socket`/`urllib` (no assumptions — direct TCP
  connects and a public-IP echo).
- Host context from `ss -tlnH`, `ip addr/route`, `tailscale status`.

## Measurements

### Egress IP (what an external target logs as the source)
- **Host egress:** `154.47.16.82` — a **ProtonVPN** exit (`proton0` is the default route,
  not the ISP link).
- **Sandbox egress:** `154.47.16.82` — **identical to the host**. Rootless pasta routes
  container outbound through the host default route → Proton. Confirmed by a real
  container probe (`container_public_ip == host_public_ip`).
- **The room relay (web / Tor onion) is ORTHOGONAL to this** — verified in code: no
  `serve`/`web`/`tor` path sets container networking; only the sbx launcher does
  (`--network=none` under hardening). The onion anonymizes the **room's inbound address**,
  never the sandbox's **outbound** ops.

### Reachability from inside a default sandbox (the pivot surface)
| Target | Reachable | Note |
|---|---|---|
| Host SSH (`192.168.1.18:22`, 0.0.0.0-bound) | **YES** | guest reaches the host's sshd |
| Home LAN routers (`192.168.1.1:80`, `192.168.8.1:80`) | **YES** | both subnets' admin |
| Tailnet Ollama (`100.110.98.21:11434`) | YES | intended |
| **Tailnet laptop SSH (`100.117.177.50:22`)** | **YES** | the canonical dev machine (`~/coding`) |
| External internet (`1.1.1.1:443`) | YES | via Proton exit |
| Host `127.0.0.1` services (i2p `7657`, CUPS `631`, operator daemons `20xxx`, `:4444`) | **NO** | pasta contains host loopback — good |

Host services bound to `0.0.0.0` (SSH `:22`, `:8787`, `:24656`, `:2222`, `:3030`) **are**
reachable from the sandbox via the host's non-loopback IPs; `127.0.0.1`-bound services are not.

## Threat model — hosting a room and granting drive to others

1. **Open egress / exit.** Everything a guest does outbound is attributed to your Proton
   IP (or your **real ISP IP if Proton drops**). Third-party attacks / illegal acts trace
   to you. *(Mitigated at launch by the egress **guard** — see Controls.)*
2. **Internal pivot (highest risk).** A guest can scan/attack the **home LAN (both subnets +
   routers), the host (SSH), and the entire tailnet** — including the laptop holding the
   canonical workspace. The sandbox is a beachhead behind the firewall. This is raw internal
   reachability, **not** an egress-IP problem — the egress guard/tor does nothing for it.
3. **Host recon.** The sandbox shares the **host kernel** (`uname` = host kernel) → CVE
   targeting; guests also learn the egress IP and network topology (subnets, tailnet peers).
4. **Container escape → host.** Default (non-`HARDEN`) containers keep default caps; rootless
   reduces but doesn't eliminate the escape surface.
5. **Broker exposure.** `hh-host` binds `0.0.0.0:4173` → the room broker is on the LAN by
   default; public relay/onion adds surface (+ the known relay-PIN-lockout bypass).

**Already holding:** rootless podman, host loopback contained, Proton masks external egress,
`--network=none`/`HARDEN=strict` exist.

## Controls — status & priority

| Control | Addresses | Status |
|---|---|---|
| **Egress guard** (`HH_SBX_EGRESS=guard`, now default) — refuse networked launch unless the route is a tunnel | #1 silent real-IP leak | **SHIPPED** (`egress.py`) |
| **Egress probe** (`operator egress [--container]`) — measure presented IP + reachability | visibility | **SHIPPED** |
| `--network=none` (`HH_SBX_EGRESS=none` / `HARDEN=strict`) | all egress + pivot | exists |
| **Internal-pivot block** — drop RFC1918 (`192.168/16`,`10/8`) + tailnet (`100.64/10`) except an Ollama allowlist | **#2 pivot (highest)** | **next — needs the enforcement spike** |
| **Tor egress** (`HH_SBX_EGRESS=tor`) — force sandbox outbound through Tor (a Tor exit, not Proton); available for ANY room transport (onion *or* web relay) | #1 anonymity, and #2 as a side-effect | after the spike |
| Bind broker to tailnet/loopback (not `0.0.0.0`); default `HARDEN` for shared rooms | #4/#5 | follow-up |

**The load-bearing uncertainty** for both the pivot-block and tor is the same: *can we
force + filter a rootless-pasta sandbox's egress (nftables + DNS control) reliably?* → a
spike proves it once and both controls fall out of it.

## Next
1. **Spike** enforced egress filtering for a rootless sandbox netns (this dir → `SPIKE.md`).
2. **Internal-pivot block** mode.
3. **Tor egress** mode (option-for-any-transport), verified with `egress --container`.

## Red-team finding + fix — IPv6 pivot bypass (2026-09-07, post-ship)

Adversarial test from inside a `local`-mode sandbox (no NET_ADMIN, like a room guest):
- **IPv4 pivot correctly blocked**, but **IPv6 pivot BYPASSED** — the sandbox reached the
  tailnet host over IPv6 (`fd7a:115c:a1e0::e537:6216`) and external IPv6. Root cause: the nft
  rulesets used `ip daddr` (IPv4 only); the default (pasta) network hands the sandbox IPv6, so
  a guest pivots into the tailnet/LAN over v6 — defeating the whole `local` host-safety control.
- **Latent bug exposed:** `local`/`scope` DNS was resolving ONLY over the IPv6 resolver, because
  the host's IPv4 resolvers (`169.254.x` pasta stub, Proton `10.x`) sit in the dropped internal
  ranges. Blocking IPv6 broke DNS — proving DNS had been silently depending on the leak.
- **Fix:** drop all IPv6 egress in the gateway (`meta nfproto ipv6 drop`, both local/scope and
  tor rulesets), and point the local/scope sandbox resolver at a public IPv4 nameserver
  (`1.1.1.1`) in postjoin. Re-verified: IPv4 internet + DNS + Ollama work; IPv4 **and** IPv6
  pivot blocked; egress still Proton-masked. tor mode was already safe (bridge net has no IPv6).
- **Config-tamper attempts denied** (no `ip`/`nft`, no NET_ADMIN) — the guest cannot alter rules.
