"""hh-cardex — turn a saved hack-house VM into a collectible "mythic creature" card.

A VM in the host-global registry (`~/.hh/registry.json`) carries enough real
signal — is the work done, is it reusable, how self-describing, how battle-tested,
how much was installed — to *judge its value deterministically*. This module turns
that judgement into a Pokemon-style card: a **PowerScore** (0-1000), a **rarity**
tier, a 6-stat block, an elemental **type**, and a stable procedural **name**.

Everything here is pure + deterministic: the same VM always yields the same card,
seeded only by its label/tags/size — no model, no network, no randomness. The
image pipeline (SVG card frame + creature art) is a separate, later layer that
consumes the card dict this module emits.

Scoring degrades gracefully: registry entries cache `purpose/status/todo/tags`,
but `provenance/objective/usage/blockers` live in the VM's deeper
`.hh-agent/manifest.yaml`. Pass that manifest dict in to score those axes too;
omit it and those sub-scores simply read 0 (the card is still valid).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from dataclasses import dataclass, asdict, field
from pathlib import Path

# ── tuning knobs (all in one place so the rubric is easy to retune) ───────────
BASE_MB = 128.0          # ~size of the empty hh-dev base image; payload = size - BASE
SUBSCORE_MAX = 100       # completeness+reusability+richness+pedigree+heft caps sum
POWER_MAX = 1000         # PowerScore = subscore_sum * 10

# Sub-score caps (sum == SUBSCORE_MAX). completeness/reusability are near-binary
# "is it a real, shipped VM?" gates, so they're kept small; the discriminating
# signal lives in richness (how self-describing + how rare its capabilities),
# heft (installed payload), and pedigree — those carry most of the spread.
COMP_MAX = 20.0
REUSE_MAX = 15.0
RICH_MAX = 30.0
PED_MAX = 10.0
HEFT_MAX = 25.0

# Per-tag capability weight — rare/high-skill security capabilities are worth
# more than generic ones, so a VM's "type rarity" pushes its richness (and thus
# its rarity tier). Unlisted tags default to 1.5.
CAPABILITY_WEIGHT = {
    # marquee / rare offensive + IR capabilities
    "exploit": 3.0, "pwn": 3.0, "c2": 3.0, "payload": 3.0, "malware": 3.0,
    "crack": 3.0, "redteam": 3.0, "dfir": 3.0, "forensics": 3.0, "reverse": 3.0,
    "ransomware": 3.0,
    # solid mid-tier capabilities
    "recon": 2.0, "osint": 2.0, "fuzz": 2.0, "brute": 2.0, "crypto": 2.0,
    "tls": 2.0, "dns": 2.0, "yara": 2.0, "pcap": 2.0, "vuln": 2.0, "cve": 2.0,
    "ids": 2.0, "threat": 2.0, "nmap": 2.0,
    # generic / scaffolding tags
    "dev": 1.0, "build": 1.0, "lib": 1.0, "http": 1.0, "web": 1.0, "log": 1.0,
    "network": 1.0, "audit": 1.0, "blue": 1.0, "training": 1.0, "lab": 1.0,
    "security": 1.0, "smoke": 0.0, "slice": 0.0,
}

# Rarity as a population QUOTA (a curve). PowerScore still ranks the cards, but
# the quota fixes the *shape* of the pyramid so a tight cluster of loop-built
# near-clones can't all land Legendary — scarcity is guaranteed regardless of how
# the raw scores inflate. This is the primary rarity mechanism (see grade_curve).
RARITY_QUOTA = [
    ("Legendary", 0.08),
    ("Epic", 0.15),
    ("Rare", 0.25),
    ("Uncommon", 0.27),
    ("Common", 0.25),
]

# Absolute PowerScore cutoffs — a *fallback* used only when a single card is
# scored in isolation (no population to curve against). Calibrated to the live
# fixed-axis distribution so a lone card still grades sanely.
RARITY_TIERS = [
    ("Legendary", 830),
    ("Epic", 800),
    ("Rare", 720),
    ("Uncommon", 560),
    ("Common", 0),
]

# tag → elemental type. First-matched tags (in entry order) pick up to 2 types.
TYPE_MAP = {
    "recon": "Psychic", "osint": "Psychic", "scanner": "Psychic",
    "exploit": "Dark", "pwn": "Dark", "payload": "Dark", "c2": "Dark",
    "redteam": "Fire", "fuzz": "Fire", "brute": "Fire", "attack": "Fire",
    "http": "Electric", "web": "Electric", "api": "Electric",
    "crypto": "Ghost", "cipher": "Ghost",
    "harden": "Steel", "defense": "Steel", "blue": "Steel", "audit": "Steel",
    "network": "Flying", "dns": "Flying", "proxy": "Flying",
    "kali": "Dragon",
    "dev": "Normal", "build": "Normal", "lib": "Normal", "tooling": "Normal",
}
OFFENSIVE_TAGS = {"exploit", "pwn", "recon", "osint", "fuzz", "kali",
                  "attack", "redteam", "payload", "c2", "scanner", "brute"}
DEFENSIVE_TAGS = {"harden", "defense", "blue", "monitor", "audit",
                  "forensics", "ids", "firewall", "steel"}

# name building blocks, indexed by a hash of the VM label so names are stable.
TYPE_PREFIX = {
    "Psychic": "Psy", "Dark": "Nox", "Fire": "Pyr", "Electric": "Volt",
    "Ghost": "Spec", "Steel": "Fer", "Flying": "Aer", "Dragon": "Dra",
    "Normal": "Neo", "Ground": "Terr", "Water": "Aqua",
}
RARITY_SUFFIX = {
    "Common": ["ling", "et", "ix"],
    "Uncommon": ["id", "ox", "ar"],
    "Rare": ["or", "yn", "ux"],
    "Epic": ["yx", "ron", "thys"],
    "Legendary": ["areth", "thar", "mereon"],
}
# generic words stripped when mining a name root out of the VM label.
_STOP_WORDS = {"vm", "kit", "dev", "test", "lib", "library", "tool", "tools",
               "the", "a", "smoke", "snap", "image", "app"}


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


# ── the five value sub-scores ────────────────────────────────────────────────
def _completeness(status: str, todo: list, blockers: list) -> float:
    """Is it a real, finished VM? Near-binary gate; small cap by design."""
    s = (status or "").strip().lower()
    base = COMP_MAX if s in ("done", "complete", "completed") else (10.0 if s == "in_progress" else 0.0)
    base -= min(len(todo) * 3, 12)
    base -= len(blockers) * 5
    return _clamp(base, 0, COMP_MAX)


def _reusability(shareable: bool, share_path: str, artifact_kind: str) -> float:
    """Can another agent actually pull and reuse it?"""
    v = 10.0 if (shareable and share_path) else 0.0
    v += {"file": 5.0, "image": 3.0, "snapshot": 2.0}.get(artifact_kind, 0.0)
    return _clamp(v, 0, REUSE_MAX)


def _richness(purpose: str, objective: str, tags: list, has_usage: bool) -> float:
    """The discriminating axis: how self-describing + how rare its capabilities.

    Rewards prose depth (purpose/objective length), tag breadth, *and* the
    capability-rarity of those tags (a c2/exploit VM out-scores a dev-lib one)."""
    v = 0.0
    v += min(len(purpose.split()) * 0.8, 8)            # purpose prose depth
    v += 5 if objective.strip() else 0                  # has a stated objective
    v += min(len(tags) * 1.5, 9)                        # tag breadth
    cap = sum(CAPABILITY_WEIGHT.get(t.strip().lower(), 1.5) for t in tags)
    v += min(cap, 8)                                    # capability rarity
    v += 4 if has_usage else 0                          # documents how to run
    return _clamp(v, 0, RICH_MAX)


def _pedigree(provenance: list, created_by: str) -> float:
    """How hand-built its lineage is — registry-derivable from the author, then
    deepened by manifest provenance when a deep manifest is supplied.

    The author tier is the load-bearing signal the CLI actually has: a VM built by
    a named human outranks a loop-mass-produced near-clone, which outranks a
    pulled/smoke throwaway. (Provenance lives in the deep .hh-agent manifest and
    is absent for registry-only scoring, so it can only *add*, never gate.)"""
    cb = (created_by or "").strip().lower()
    if cb in ("", "smoke"):
        base = 0.0
    elif cb == "pulled":
        base = 1.0
    elif cb == "hh-loop":
        base = 4.0            # a real, reproducible automated build
    else:
        base = 8.0            # hand-authored by a named human
    base += min(len(provenance) * 2.0, PED_MAX - base)   # manifest deepens lineage
    return _clamp(base, 0, PED_MAX)


def _heft(size_bytes: int) -> float:
    """Installed payload over the empty base — log-scaled against a 512MB
    reference so heft spreads across the payload range instead of pinning every
    mid-size VM at the cap (the old /6.0 scale saturated by ~350MB)."""
    payload_mb = max(0.0, (size_bytes or 0) / 1e6 - BASE_MB)
    return _clamp(HEFT_MAX * math.log2(1 + payload_mb) / math.log2(1 + 512), 0, HEFT_MAX)


def _rarity_of(power: int) -> str:
    for name, floor in RARITY_TIERS:
        if power >= floor:
            return name
    return "Common"


def _flavor(rarity: str, types: list, purpose: str, bst: int) -> str:
    ty = "/".join(types)
    return (f"A {rarity.lower()} {ty}-type born from {purpose.rstrip('.')}. "
            f"Base stat total {bst}.")


# ── stats: the same sub-scores, re-projected onto the 6 classic stats ─────────
def _stat(frac: float) -> int:
    """Map a 0..1 fraction onto the Pokemon-ish 5..255 stat range."""
    return int(round(5 + _clamp(frac) * 250))


def _types(tags: list) -> list:
    out: list[str] = []
    for t in tags:
        ty = TYPE_MAP.get(t.strip().lower())
        if ty and ty not in out:
            out.append(ty)
        if len(out) == 2:
            break
    return out or ["Normal"]


# ── procedural name (deterministic; seeded by the VM label) ───────────────────
def _seed(label: str) -> int:
    return int(hashlib.sha256(label.encode()).hexdigest(), 16)


def _root(label: str) -> str:
    """Mine a pronounceable name root from the VM label."""
    toks = [t for t in "".join(c if c.isalnum() else " " for c in label).split()
            if t.lower() not in _STOP_WORDS and not t.isdigit()]
    if not toks:
        toks = [label or "vm"]
    # pick a token by hash, take its first 4 letters as the core syllable.
    tok = toks[_seed(label) % len(toks)].lower()
    return (tok[:4] or "mon")


def _name(label: str, primary_type: str, rarity: str) -> str:
    h = _seed(label)
    prefix = TYPE_PREFIX.get(primary_type, "Neo")
    suffixes = RARITY_SUFFIX[rarity]
    suffix = suffixes[(h >> 8) % len(suffixes)]
    core = _root(label)
    # avoid a doubled letter at the prefix/core seam (Volt+tap -> Voltap)
    if prefix and core and prefix[-1].lower() == core[0].lower():
        core = core[1:]
    return (prefix + core + suffix).capitalize()


def _dex(label: str) -> int:
    return _seed(label) % 1000 + 1


@dataclass
class Card:
    label: str
    name: str
    dex: int
    rarity: str
    power: int                      # 0..1000
    types: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)   # HP/Attack/Defense/SpAtk/Speed/SpDef
    subscores: dict = field(default_factory=dict)
    shiny: bool = False             # deterministic "holo pull"
    status: str = ""                # VM build status (done / in_progress / …)
    purpose: str = ""               # the VM's human description (card front text)
    flavor: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def card_from_entry(entry: dict, manifest: dict | None = None) -> Card:
    """Build a Card from a registry entry (+ optional deep .hh-agent manifest)."""
    m = manifest or {}
    state = m.get("state", {}) if isinstance(m.get("state"), dict) else {}
    goals = m.get("goals", {}) if isinstance(m.get("goals"), dict) else {}

    label = entry.get("label", "vm")
    tags = entry.get("tags") or []
    blockers = state.get("blockers") or []
    provenance = m.get("provenance") or []
    objective = goals.get("objective", "")
    has_usage = bool(m.get("usage") or m.get("setup"))

    comp = _completeness(entry.get("status", ""), entry.get("todo") or [], blockers)
    reuse = _reusability(bool(entry.get("shareable")), entry.get("share_path", ""),
                         entry.get("artifact_kind", ""))
    rich = _richness(entry.get("purpose", ""), objective, tags, has_usage)
    ped = _pedigree(provenance, entry.get("created_by", ""))
    heft = _heft(entry.get("size_bytes"))

    subs = {"completeness": round(comp, 1), "reusability": round(reuse, 1),
            "richness": round(rich, 1), "pedigree": round(ped, 1),
            "heft": round(heft, 1)}
    power = int(round(sum(subs.values()) / SUBSCORE_MAX * POWER_MAX))
    power = max(0, min(POWER_MAX, power))
    rarity = _rarity_of(power)
    types = _types(tags)

    # stat block — every stat gets a floor from overall power so a strong VM is
    # well-rounded, plus a specialized component from the axis that "owns" it.
    pf = power / POWER_MAX
    floor = 0.30 * pf
    low = [t.lower() for t in tags]
    off = min(sum(t in OFFENSIVE_TAGS for t in low), 3) / 3
    dfn = min(sum(t in DEFENSIVE_TAGS for t in low), 3) / 3
    payload_mb = max(0.0, (entry.get("size_bytes") or 0) / 1e6 - BASE_MB)
    speed_raw = _clamp(1 - payload_mb / 256)
    stats = {
        "HP":      _stat(floor + 0.70 * (0.6 * heft / HEFT_MAX + 0.4 * comp / COMP_MAX)),
        "Attack":  _stat(floor + 0.70 * off),
        "Defense": _stat(floor + 0.70 * (0.5 * dfn + 0.5 * comp / COMP_MAX)),
        "SpAtk":   _stat(floor + 0.70 * (rich / RICH_MAX)),
        "Speed":   _stat(floor + 0.70 * speed_raw),
        "SpDef":   _stat(floor + 0.70 * (0.6 * reuse / REUSE_MAX + 0.4 * ped / PED_MAX)),
    }

    shiny = (_seed(label) >> 16) % 16 == 0   # ~1/16, the classic shiny odds
    name = _name(label, types[0], rarity)
    purpose = entry.get("purpose") or objective or "an unknown sandbox"
    flavor = _flavor(rarity, types, purpose, sum(stats.values()))

    status = (entry.get("status") or "unknown").strip().lower()
    return Card(label=label, name=name, dex=_dex(label), rarity=rarity, power=power,
                types=types, stats=stats, subscores=subs, shiny=shiny,
                status=status, purpose=purpose, flavor=flavor)


# ── population rarity curve ───────────────────────────────────────────────────
def _apply_rarity(card: Card, rarity: str) -> None:
    """Reassign a card's rarity (e.g. after curve grading) and rebuild the
    rarity-dependent fields — the procedural name suffix and the flavor line."""
    card.rarity = rarity
    card.name = _name(card.label, card.types[0], rarity)
    card.flavor = _flavor(rarity, card.types, card.purpose, sum(card.stats.values()))


def grade_curve(cards: list[Card]) -> list[Card]:
    """Re-grade a whole library's rarity on the population curve (in place).

    PowerScore is the ranking key; RARITY_QUOTA fixes the shape of the pyramid so
    a tight cluster of loop-built near-clones can't all land Legendary. Rank ties
    break deterministically on the VM label, so a fixed library always grades the
    same way — and each card's name/flavor is rebuilt to match its new tier.

    Use this whenever scoring a *library*; a single card scored alone keeps its
    absolute-threshold rarity (there is no population to rank it against)."""
    if not cards:
        return cards
    order = sorted(cards, key=lambda c: (-c.power, _seed(c.label)))
    n = len(order)
    cuts, acc = [], 0.0
    for name, q in RARITY_QUOTA:
        acc += q
        cuts.append((name, acc))
    for i, c in enumerate(order):
        f = (i + 0.5) / n
        rarity = next((name for name, cum in cuts if f <= cum), "Common")
        _apply_rarity(c, rarity)
    return cards


# ── registry I/O + CLI ────────────────────────────────────────────────────────
def _registry_path() -> Path:
    base = os.environ.get("HOME", ".")
    return Path(base) / ".hh" / "registry.json"


def load_entries(path: Path) -> list[dict]:
    data = json.loads(path.read_text())
    return list(data.get("entries", {}).values())


_BAR = "█"


def _render_table(cards: list[Card]) -> str:
    rows = []
    rows.append(f"{'VM':<22} {'NAME':<14} {'#':>4}  {'RARITY':<10} {'PWR':>4}  "
                f"{'TYPE':<16} {'BST':>4}  HP/Atk/Def/SpA/Spe/SpD")
    rows.append("-" * 104)
    for c in sorted(cards, key=lambda x: -x.power):
        s = c.stats
        line = (f"{c.label[:22]:<22} {c.name:<14} {c.dex:>4}  "
                f"{(('*' if c.shiny else '') + c.rarity):<10} {c.power:>4}  "
                f"{'/'.join(c.types):<16} {sum(s.values()):>4}  "
                f"{s['HP']:>3}/{s['Attack']:>3}/{s['Defense']:>3}/"
                f"{s['SpAtk']:>3}/{s['Speed']:>3}/{s['SpDef']:>3}")
        rows.append(line)
    return "\n".join(rows)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="hh-cardex",
                                description="Score saved hack-house VMs as collectible cards.")
    p.add_argument("--registry", default=str(_registry_path()),
                   help="path to registry.json (default: ~/.hh/registry.json)")
    p.add_argument("--label", help="only this VM")
    p.add_argument("--json", action="store_true", help="emit card JSON instead of a table")
    args = p.parse_args(argv)

    entries = load_entries(Path(args.registry))
    single = bool(args.label)
    if single:
        entries = [e for e in entries if e.get("label") == args.label]
        if not entries:
            print(f"no VM labelled '{args.label}' in {args.registry}")
            return 1
    cards = [card_from_entry(e) for e in entries]
    # rarity is a population quota: grade the whole library on the curve. A lone
    # --label card keeps its absolute-threshold rarity (nothing to rank against).
    if not single:
        grade_curve(cards)

    if args.json:
        out = [c.to_dict() for c in cards]
        print(json.dumps(out[0] if args.label else out, indent=2))
    else:
        print(_render_table(cards))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
