# SPIKE — enforced egress filtering for a rootless sandbox (2026-09-07)

**Question:** can we *force + filter* a rootless-pasta sandbox's egress reliably (the
load-bearing uncertainty for both the internal-pivot block and tor egress)?

**Answer: YES — via a sidecar gateway netns.** Proven live on `trillsec`.

## The mechanism

Rootless podman can't easily apply nftables inside a normal sandbox's own netns without
giving the sandbox `NET_ADMIN` (which the agent could then use to remove the rules). The
clean pattern instead:

1. A **gateway container** owns the network + the filtering rules. It runs with
   `--cap-add=NET_ADMIN` and applies an nftables ruleset to its own netns.
2. The **sandbox joins the gateway's netns**: `podman run --network=container:<GW> …`.
   The sandbox has **no** network config of its own and **no** `NET_ADMIN` — all its
   traffic traverses the gateway's stack and rules, with nothing it can bypass or edit.

Two facts this rested on, both confirmed:
- **Rootless nftables works** in a container with `--cap-add=NET_ADMIN`
  (`alpine` + `apk add nftables`, `nft add table` + `nft -f` succeed → `NFT-OK-ROOTLESS`).
- **`--network=container:<GW>` shares the netns** — the sandbox is subject to the
  gateway's rules.

## Proof — pivot-block ruleset

Gateway ruleset (allow Ollama, drop all RFC1918 + tailnet CGNAT + link-local, allow rest):

```nft
table inet filt {
  chain output {
    type filter hook output priority 0; policy accept;
    oifname "lo" accept
    ip daddr 100.110.98.21 accept                     # Ollama allowlist (before the CGNAT drop)
    ip daddr { 192.168.0.0/16, 10.0.0.0/8, 172.16.0.0/12, 100.64.0.0/10, 169.254.0.0/16 } drop
  }
}
```

Probe from inside a sandbox joined to that gateway (`--network=container:GW`):

| Target | Result |
|---|---|
| Host SSH `192.168.1.18:22` | **BLOCKED** |
| LAN routers `192.168.1.1`, `192.168.8.1` | **BLOCKED** |
| Tailnet laptop SSH `100.117.177.50:22` | **BLOCKED** |
| Tailnet Ollama `100.110.98.21:11434` | reachable (allowlisted) |
| Internet `1.1.1.1:443` | reachable |

Exactly the intended posture: local models + internet yes; LAN / host / tailnet pivot no.

## What this unlocks (both modes, same mechanism)

- **`HH_SBX_EGRESS=local` / `scope`** — the gateway runs the pivot-block ruleset (± an
  allowlist from a scope file). Highest-value control for sharing a room with outsiders:
  the guest-driven sandbox physically can't reach the LAN, host, or tailnet.
- **`HH_SBX_EGRESS=tor`** — the same gateway instead runs **tor** (`TransPort`/`DNSPort`)
  and an nft ruleset that REDIRECTs all TCP to the TransPort + forces DNS to the DNSPort +
  drops the rest. Sandbox outbound then exits via a **Tor exit node**, independent of the
  room transport (works whether the room is hosted over the onion **or** the web relay).

## Design decisions for the build

- **Gateway image:** bake a small `localhost/hh-egress-gw` image (alpine + nftables + tor)
  so launch doesn't `apk add` at runtime (offline, fast, deterministic). Runtime `apk`
  was used only to prove the spike.
- **Lifecycle:** one gateway per sandbox (named `hh-egw-<sandbox>`), created before the
  sandbox and torn down with it; ownership-labelled + PID-stamped like the sandbox
  (`sweep_stale`) so a killed daemon doesn't leak gateways.
- **Fail-closed:** if the gateway fails to come up / apply rules, the sandbox launch is
  **refused** (never fall back to an unfiltered network).
- **Compose with `guard`:** `guard` still checks the tunnel is up first; the gateway then
  controls where within/through it the sandbox may go.

## Caveats still to verify in the build
- DNS: `local`/`scope` must also constrain DNS (force the host/gateway resolver) so the
  agent can't resolve+connect out-of-band around the allowlist. For `tor`, force the DNSPort.
- tor `TransPort` transparent-redirect inside the gateway netns (the nft `REDIRECT` to
  tor) is the one piece not yet exercised here — spike it specifically before shipping tor.

## Tor spike outcome (2026-09-07) — the pasta/nat limitation

Building the `tor` gateway surfaced a hard constraint, now precisely characterized:

- **tor itself works in the gateway**: bootstraps to 100% (~30s), `TransPort:9040` (TCP)
  and `DNSPort:5353` (UDP) both functional (`nslookup -port=5353` via tor resolves).
- **Under rootless pasta, the nftables `nat` hook is BYPASSED** for the joined sandbox's
  traffic — an `nat hook output` redirect rule shows `counter packets 0`. This is why the
  transparent tor redirect doesn't catch the sandbox. (The `filter hook output` DROP used by
  `local`/`scope` DOES fire under pasta — hence those modes work.)
- **On a netavark BRIDGE network, `nat` fires** — the same redirect shows
  `udp dport 53 counter packets 2` (DNS redirected to tor). So the tor gateway must run on a
  **bridge network**, not default pasta.
- **Remaining blocker (one more iteration):** on the bridge, DNS packets reach tor's DNSPort
  but the sandbox's resolution still fails ("Temporary failure in name resolution") — the
  DNSPort *response* path / `AutomapHostsOnResolve` handoff to the TransPort needs fixing
  (candidates: bind/redirect target for `:5353`, the sandbox's resolver config, or forcing
  `--dns` on the sandbox to a redirectable address). TCP-through-TransPort was never reached
  because DNS failed first.

**Status:** `HH_SBX_EGRESS=tor` is stubbed **fail-closed** (refuses launch) until the bridge
+ DNS path is finished — so it never silently launches unfiltered. Finishing it = (a) run the
gateway on a dedicated bridge network, (b) resolve the DNSPort response path, (c) verify the
sandbox presents a Tor exit IP via `egress --container`.

## Tor — RESOLVED (2026-09-07)

Finished and validated end-to-end (`HH_SBX_EGRESS=tor` via `launch_container`):
- Gateway on the **bridge network** `hh-egress-net` (so nat fires); tor `TransPort:9040` +
  `DNSPort:127.0.0.1:53`; sandbox resolver pointed at `127.0.0.1` (shared loopback → no
  DNS-redirect fragility); nft nat redirects all TCP to the TransPort.
- **Sandbox egress = a Tor exit IP** (e.g. `23.129.64.150`, `192.42.116.92`), NOT the Proton IP.
- **Internal pivot blocked** — the tor ruleset also drops RFC1918 + tailnet (10.0.0.0/8 split
  so tor's own `10.192.0.0/10` virtual range survives) before the redirect; laptop SSH + LAN
  router BLOCKED.
- **Leak-blocked** — a filter chain drops any non-TCP, non-loopback egress (no QUIC/UDP leak
  around Tor); tor's own traffic (`skuid tor`) and loopback DNS are exempt.
- Cost: ~30s tor bootstrap on launch. Works for ANY room transport (web relay or onion) — egress
  control is orthogonal to how the room is reached.
