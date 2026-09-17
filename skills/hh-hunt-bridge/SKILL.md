---
name: hh-hunt-bridge
description: Run bug-bounty/red-team hunting inside a hack-house room instead of a bare local session — operators drive the hunt-*/bb-local-toolkit skills against a target through a shared sandbox, findings are posted to the room as they're confirmed, and validated results feed straight into report-writing. Use when a hunt is asked to happen "in the house", when a room's sandbox should be the operating box for an engagement, or when multiple operators should hunt one target together with a visible trail.
---

# hh-hunt-bridge — operate the bug-bounty/red-team skill library from inside a room

The `hunt-*`/`bb-local-toolkit`/`redteam-mindset` skill library (~40 skills) assumes
a bare local Claude Code session with Bash. `hh-operator` assumes a room + sandbox
with no security-testing doctrine of its own. Neither reaches the other. This skill
is the bridge: it doesn't reimplement any hunting technique — it tells an operator
*how to run an existing hunt skill through a room's sandbox instead of local Bash*,
and how to keep the room a legible record of the engagement rather than a silent
side channel. Built on **`hh-operator`** (read it first) and whichever `hunt-*` /
`bb-local-toolkit` skill fits the target.

```bash
HHREPO="${HH_REPO:-$HOME/coding/hack-house/main}"
HH="$HHREPO/.venv/bin/python -m cmd_chat.operator"     # or: hh-op
```

## 0. Scope discipline (non-negotiable)

Nothing here changes the authorization rules the `hunt-*`/`bb-local-toolkit` skills
already enforce — in-scope targets only, program rules honored, no destructive
actions, no mass-targeting. The room is a *venue*, not a permission slip. If the
brief doesn't already have a scoped, authorized target, get one before joining.

## 1. Solo hunt — one operator, room as an audit trail

The simplest case: you're already the operator, you just want the room (and
anyone watching it) to see the engagement happen rather than running it in a
silent local shell.

```bash
$HH up <host> <port> hunter --password <pw> --no-tls
$HH sbx launch --image kalilinux/kali-rolling --engine podman   # or: use the room's granted sandbox
$HH exec 'nmap -sV -Pn <target>'          # drive the hunt through exec, not local Bash
$HH say "recon: 3 open ports, nginx 1.24 + a leaked /admin path — pivoting to IDOR checks"
```
Then load the fitting skill (`hunt-idor`, `hunt-ssrf`, `bb-local-toolkit`, …) and
follow its doctrine exactly — only the *actuator* changed (`$HH exec`/`write`/`get`
instead of local Bash), not the methodology.

## 2. Multi-operator hunt — same topology pattern as `hh-loop`

For a target big enough to split (recon / hunt-by-vuln-class / validate), reuse
`hh-operator`'s `spawn` the same way `hh-loop` spawns planner/builder/tester — just
swap the brief for a hunting one:

```bash
$HH spawn "Join <host> <port> as recon (--password <pw> --no-tls). Use the \
hh-operator skill for the room, then the bb-local-toolkit skill for the target. \
Scope: <in-scope hosts/program>. Do recon (subdomain enum, fingerprinting, \
HackerOne scope check) and post findings via \$HH say as you confirm them. \
Do NOT test payloads — hand candidates to the hunt operator." \
  --room-host <host> --room-port <port> --room-name recon --go --skip-permissions

$HH spawn "Join <host> <port> as hunter (--password <pw> --no-tls). Use the \
hh-operator skill for the room. Read recon's findings via \$HH read --wait, then \
load hunt-idor / hunt-ssrf / hunt-xss (pick by what recon surfaced) against \
<scope>. Post each candidate finding to the room before validating it, so the \
trail exists even if validation later kills it." \
  --room-host <host> --room-port <port> --room-name hunter --go --skip-permissions
```
Each spawned operator should still load `hh-operator` for the room mechanics, then
the appropriate `hunt-*` skill for the technique — the room membership and the
security methodology are orthogonal, don't collapse them into one prompt.

## 3. Discipline: post before you validate, not after

The room is the audit trail an engagement otherwise lacks. Say a *candidate*
finding as soon as you have one (`$HH say "candidate: IDOR on /api/orders/:id — no
ownership check, testing now"`), then validate per `triage-validation`'s 7-Question
Gate, then post the outcome either way:
```bash
$HH say "CONFIRMED: IDOR on /api/orders/:id — see PoC in /root/poc-idor.md"
# or, just as important:
$HH say "killed: /api/orders/:id IDOR — turned out to be scoped by session, false positive"
```
A room with only confirmed findings looks like every candidate that got tried —
recording kills is what makes the trail trustworthy to whoever reviews it later.

## 4. Hand off to reporting from inside the room

Once `triage-validation` passes a finding, don't leave the house to write it up.
Stamp the finding onto the sandbox's manifest (so it survives a save), then load
`report-writing` (or `bugcrowd-reporting` for Bugcrowd specifically) using the
in-sandbox PoC/evidence already gathered via `$HH exec`/`get`:

```bash
$HH manifest update --root /root --status done \
  --done "confirmed IDOR on /api/orders/:id, PoC + evidence captured" \
  --note "validated via triage-validation 7-question gate"
$HH get /root/poc-idor.md --out ./poc-idor.md   # pull evidence out to write the report
```

## 5. When *not* to bridge

Skip the room entirely for a quick, single-operator local check where there's
nothing to share and no reason for a live trail — `hh-hunt-bridge` earns its keep
when a hunt is collaborative, needs a durable record, or the sandbox itself is the
deliverable (e.g. handing a "confirmed-vulnerable" VM to another operator). A solo
five-minute recon grep doesn't need a room.

## Safety

- In-scope, authorized targets only — the room adds visibility, not permission.
- No destructive actions, no mass-targeting; honor program/engagement rules exactly
  as the underlying `hunt-*`/`bb-local-toolkit` skills already require.
- Blast radius of any exec is the sandbox; never run findings against production
  infrastructure you don't have explicit authorization for, room or no room.
- Post kills as faithfully as confirms — the trail is only useful if it's honest.
