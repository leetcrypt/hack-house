"""hh-chardex — a library of harvested character portraits + a VM→character matcher.

The cardex (`cardex.py`) judges a VM's value and assigns it an elemental *type*
and a *purpose*. This module gives each VM a **face**: it indexes a library of
real, auto-cropped character portraits (harvested by `scripts/charvest.py` from
YouTube / Instagram / image sources) and picks the one whose element + keywords
best fit the VM — deterministically, so a VM always wears the same character.

Pure stdlib so `cardimg` can import it offline. The library lives under
`assets/characters/` (override with `$HH_CHARDEX_DIR`):

    assets/characters/
      index.json            # [{slug,name,element,tier,keywords,source,credit,file}]
      <slug>.png            # the cropped square portrait

Matching is a transparent additive score (type overlap dominates, then keyword
overlap with the VM's tags + purpose, then a rarity/tier affinity), with a
hash-of-label tie-break so picks are stable and well-distributed. No model,
no network.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path


def chardex_dir() -> Path:
    env = os.environ.get("HH_CHARDEX_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "assets" / "characters"


@dataclass
class Character:
    slug: str
    name: str
    file: str                       # portrait filename within the library dir
    element: list = field(default_factory=list)   # card types this fits, e.g. ["Fire","Dragon"]
    tier: str = ""                  # optional rarity affinity (Common..Legendary)
    keywords: list = field(default_factory=list)  # descriptive words for purpose-match
    source: str = ""                # acquisition URL
    credit: str = ""                # attribution / franchise

    def path(self, base: Path) -> Path:
        return base / self.file


def load_library(base: Path | None = None) -> list[Character]:
    base = base or chardex_dir()
    idx = base / "index.json"
    if not idx.exists():
        return []
    raw = json.loads(idx.read_text())
    out = []
    for e in raw:
        out.append(Character(
            slug=e["slug"], name=e.get("name", e["slug"]),
            file=e.get("file", e["slug"] + ".png"),
            element=e.get("element") or [], tier=e.get("tier", ""),
            keywords=[k.lower() for k in (e.get("keywords") or [])],
            source=e.get("source", ""), credit=e.get("credit", "")))
    return out


def save_library(chars: list[Character], base: Path | None = None) -> Path:
    base = base or chardex_dir()
    base.mkdir(parents=True, exist_ok=True)
    idx = base / "index.json"
    idx.write_text(json.dumps([{
        "slug": c.slug, "name": c.name, "file": c.file, "element": c.element,
        "tier": c.tier, "keywords": c.keywords, "source": c.source,
        "credit": c.credit} for c in chars], indent=2))
    return idx


_TIER_RANK = {"Common": 0, "Uncommon": 1, "Rare": 2, "Epic": 3, "Legendary": 4}
_STOP = {"a", "an", "the", "for", "and", "of", "to", "vm", "with", "via", "on",
         "shared", "dev", "sandbox", "unknown", "from", "into", "kit", "lab",
         # rarity / flavor-boilerplate words — rarity is handled by tier affinity,
         # not keyword overlap, so these must not drive the character match.
         "legendary", "epic", "rare", "uncommon", "common", "mythical",
         "born", "base", "stat", "total", "type"}


def _purpose_words(purpose: str) -> set[str]:
    toks = "".join(c if c.isalnum() else " " for c in (purpose or "").lower()).split()
    return {t for t in toks if len(t) > 2 and t not in _STOP}


def _seed(s: str) -> int:
    return int(hashlib.sha256(s.encode()).hexdigest(), 16)


def score(card, ch: Character) -> float:
    """Additive, transparent fit score between a cardex Card and a Character."""
    s = 0.0
    types = set(getattr(card, "types", []) or [])
    elem = set(ch.element)
    matched = len(types & elem)
    # elemental fit dominates, but with diminishing returns on extra matches so a
    # strong single-type face can still compete (via jitter) with a dual-type one
    # — otherwise every same-typed VM collapses onto the one dual-type character.
    if matched:
        s += 6.0 + 3.0 * (matched - 1)                 # 1→6.0, 2→9.0, 3→12.0
    # keyword overlap with the VM's tags + purpose prose (capped so purpose fit
    # nudges but never overrides elemental fit)
    vocab = {t.lower() for t in (getattr(card, "label", "").replace("-", " ").split())}
    vocab |= _purpose_words(getattr(card, "flavor", ""))
    # cardex Card has no raw tags, but flavor embeds purpose; also use name root
    kw = set(ch.keywords)
    s += min(1.5 * len(kw & vocab), 3.0)
    # rarity / tier affinity — a gentle nudge so legendary characters lean toward
    # legendary VMs, kept small enough that the per-VM jitter still spreads picks
    # across same-type faces (see match()) instead of collapsing onto one.
    if ch.tier and getattr(card, "rarity", ""):
        dist = abs(_TIER_RANK.get(ch.tier, 2) - _TIER_RANK.get(card.rarity, 2))
        s += max(0.0, 1.5 - 0.75 * dist)
    return s


# Jitter folded INTO the score so that among characters of the *same* element
# (equal 6.0-per-type base) the winner varies per VM instead of every same-type
# VM collapsing onto the single highest-tier character. Kept below JITTER_MAX <
# one type-match unit (6.0) so a better *elemental* fit always still wins.
JITTER_MAX = 4.0


def match(card, library: list[Character] | None = None) -> Character | None:
    """Pick the best-fitting character for a VM card; stable per VM label.

    Elemental fit dominates; within an element, a per-(VM,character) hash jitter
    spreads picks across all equally-fitting faces so a common VM type doesn't
    reuse one portrait. Deterministic — a given VM always wears the same face."""
    lib = library if library is not None else load_library()
    if not lib:
        return None
    label = getattr(card, "label", "vm")
    best, best_key = None, None
    for ch in lib:
        sc = score(card, ch)
        jitter = JITTER_MAX * ((_seed(label + "\x00" + ch.slug) % 10000) / 10000.0)
        key = sc + jitter
        if best_key is None or key > best_key:
            best, best_key = ch, key
    return best


# ── CLI: inspect the library / preview matches ────────────────────────────────
def main(argv=None) -> int:
    import argparse
    from .cardex import card_from_entry, load_entries, _registry_path

    p = argparse.ArgumentParser(prog="hh-chardex",
                                description="Inspect the character library and preview VM→character matches.")
    p.add_argument("--dir", default=str(chardex_dir()), help="character library dir")
    p.add_argument("--registry", default=str(_registry_path()))
    p.add_argument("--matches", action="store_true",
                   help="show the matched character for every VM in the registry")
    args = p.parse_args(argv)

    base = Path(args.dir)
    lib = load_library(base)
    if not lib:
        print(f"empty character library at {base} — run scripts/charvest.py first")
        return 1
    if not args.matches:
        print(f"{len(lib)} characters in {base}:")
        for c in lib:
            print(f"  {c.slug:<22} {c.name:<20} {'/'.join(c.element):<16} "
                  f"{c.tier:<10} {' '.join(c.keywords[:6])}")
        return 0

    entries = load_entries(Path(args.registry))
    for e in entries:
        card = card_from_entry(e)
        ch = match(card, lib)
        print(f"{card.label:<22} {card.rarity:<10} {'/'.join(card.types):<16} "
              f"→ {ch.name if ch else '(none)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
