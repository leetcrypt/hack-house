---
name: hh-catalog-audit
description: Quality-audit the existing hack-house VM registry — batch-score every saved entry with cardex, flag anything below Rare, dangling todos, duplicate capability across tags, and shareable entries missing their published .tar. Use before queuing new hh-loop briefs (avoid duplicating an existing VM's capability), periodically to keep the library trustworthy, or when asked to audit/clean up the saved-VM catalog.
---

# hh-catalog-audit — sweep the saved-VM registry for quality and duplication

`hh-loop` scores and gates **new** VMs against the PowerScore rubric before it
saves one. Nothing sweeps the ~dozens of VMs that are already in
`~/.hh/registry.json` for the same things: a stale manifest, a dangling `todo`
that never got closed, two VMs quietly covering the same capability, or a
`shareable:true` entry whose `.tar` went missing. This skill is that sweep — read
**`hh-loop`** first for what "high value" means; this just applies the same rubric
backwards, in batches, over the whole library. Same pattern as the `vault-audit`
skill (batch-score, verdict, report), applied to VMs instead of notes.

```bash
HHREPO="${HH_REPO:-$HOME/coding/hack-house/main}"
CARDEX="$HHREPO/.venv/bin/python -m cmd_chat.cardex"
HH="$HHREPO/.venv/bin/python -m cmd_chat.operator"     # or: hh-op
```

## 1. Pull the whole library, scored

```bash
$CARDEX --json > /tmp/hh-catalog-scored.json      # every entry, PowerScore + rarity + stats
python3 -c '
import json
cards = json.load(open("/tmp/hh-catalog-scored.json"))
print(f"{len(cards)} VMs in the registry")
for c in sorted(cards, key=lambda c: c["power"]):
    if c["rarity"] in ("Common", "Rare"):
        print(f"  below-Epic: {c[\"label\"]:<28} {c[\"rarity\"]:<8} power={c[\"power\"]}")'
```
A `Common`/`Rare` verdict on a *finished* VM (not one still mid-brief) is the
signal — per `hh-loop`'s rubric that almost always means a thin payload (heft) or
a half-filled manifest (richness/pedigree), not that the tool itself is bad.

## 2. Dangling-`todo` sweep (completeness axis)

A VM whose manifest still carries an open `todo` is either genuinely unfinished
or was saved prematurely — either way it shouldn't be presented as a ready-to-pull
capability:

```bash
$HH registry list --json | python3 -c '
import json, sys
for e in json.load(sys.stdin):
    label = e.get("label", "?")
    # registry show scrapes purpose/status/todo from the manifest into the entry
    print(label)' | while read -r LABEL; do
  TODO="$($HH registry show --label "$LABEL" --json 2>/dev/null | python3 -c \
    'import json,sys; d=json.load(sys.stdin); print(d.get("todo",""))' 2>/dev/null)"
  [ -n "$TODO" ] && [ "$TODO" != "None" ] && echo "DANGLING TODO: $LABEL -> $TODO"
done
```

## 3. Duplicate-capability check (before queuing a new `hh-loop` brief, always)

`hh-loop` already says "always `registry list` first" — this makes that a real
check instead of eyeballing a list. Compare the new brief's `tags` against every
existing entry's tags; anything with ≥2 tag overlap plus a similar `purpose`
string is a candidate duplicate, not a new contribution:

```bash
$HH registry list --json | python3 -c '
import json, sys
from collections import defaultdict
entries = json.load(sys.stdin)
by_tag = defaultdict(list)
for e in entries:
    for t in e.get("tags", []):
        by_tag[t].append(e["label"])
for tag, labels in sorted(by_tag.items()):
    if len(labels) > 1:
        print(f"{tag}: {labels}")'
```
Read every cluster with >1 label before a wave queues a brief sharing that tag —
a fifth "recon, kali" VM is not a fifth contribution unless it genuinely does
something the other four don't.

## 4. Broken-publish check (reusability axis)

`shareable:true` is supposed to mean a real portable `.tar` exists. Confirm it
actually does — a registry entry can drift from disk (a manually deleted
snapshot, an interrupted publish):

```bash
$HH registry list --json | python3 -c '
import json, sys, os
for e in json.load(sys.stdin):
    if e.get("shareable") and e.get("share_path"):
        p = os.path.expanduser(e["share_path"])
        if not os.path.exists(p):
            print(f"BROKEN PUBLISH: {e[\"label\"]} -> missing {p}")'
```

## 5. Report

Roll §1–4 into one pass/flag list — this is a report-and-flag skill, not an
auto-fix one (the same posture as `memory-dream`'s destructive-proposal staging):
list what's below-Epic, what has a dangling todo, what tag clusters look
duplicated, and what's broken-published. A human (or the next `hh-loop` run)
decides whether to re-open, re-tag, re-publish, or retire each flagged entry —
this skill never deletes a registry entry or a snapshot itself.

## Safety

- Read-only against the registry and the filesystem — no mutation, no `sbx
  save`/`publish`, no deletion of a snapshot or registry entry.
- Flag, don't judge from vibes: every finding above traces to a concrete field
  (`power`, `todo`, `tags`, `share_path`) so the report is reproducible.
- Run this **before** queuing new `hh-loop` briefs in a wave, not just
  periodically — the duplicate-capability check is cheapest before the fact.
