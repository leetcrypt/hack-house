#!/usr/bin/env python3
"""imgharvest — build the hh card-art library from web *image* search.

Complements `charvest.py` (which pulls frames from a video URL). Instead of one
subject per video, this queries a keyless image engine (DuckDuckGo) for a
character, downloads many candidate stills, and runs each through the *same*
auto-crop + quality pipeline as charvest — then keeps the sharpest, most
distinct portraits. Static official art tends to sit on clean backgrounds, so
the saliency crop lands far better than on motion-blurred video frames, and one
query yields several usable, non-duplicate faces at once.

Mix genres freely — Pokémon, Digimon, Yu-Gi-Oh — tagging each with the card
`--element`(s) so `cmd_chat/chardex.py` can match a VM to a fitting face.

Run with the anaconda interpreter (needs cv2 + numpy, like charvest):

    python3 scripts/imgharvest.py "Gardevoir official art" \
        --name Gardevoir --element Psychic Fairy --tier Epic \
        --keywords psychic elegant guardian embrace --max 3 --credit Pokemon

⚠️  Harvested characters are likely third-party IP — see
    docs/character-art-licensing.md before any public/commercial use.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

import cv2

# reuse the proven crop + quality + library-write pipeline
sys.path.insert(0, str(Path(__file__).resolve().parent))
import charvest as cv  # noqa: E402

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")
IMG_EXT = cv.IMG_EXT


# ── keyless DuckDuckGo image search ───────────────────────────────────────────
def _vqd(query: str) -> str | None:
    req = urllib.request.Request(
        "https://duckduckgo.com/?q=" + urllib.parse.quote(query) + "&iax=images&ia=images",
        headers={"User-Agent": UA})
    html = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore")
    for pat in (r'vqd=([\d-]+)\&', r'vqd="([\d-]+)"', r"vqd='([\d-]+)'",
                r'vqd=([\d-]+)'):
        m = re.search(pat, html)
        if m:
            return m.group(1)
    return None


def ddg_image_urls(query: str, limit: int) -> list[str]:
    """Return direct image URLs for a query via DuckDuckGo's i.js endpoint."""
    vqd = _vqd(query)
    if not vqd:
        print(f"[search] no vqd token for {query!r}", file=sys.stderr)
        return []
    api = ("https://duckduckgo.com/i.js?l=us-en&o=json&q="
           + urllib.parse.quote(query) + f"&vqd={vqd}&f=,,,,,&p=1")
    req = urllib.request.Request(api, headers={
        "User-Agent": UA, "Referer": "https://duckduckgo.com/",
        "Accept": "application/json, text/javascript, */*; q=0.01"})
    data = json.loads(urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore"))
    urls = [r["image"] for r in data.get("results", []) if r.get("image")]
    return urls[:limit]


def download(url: str, dst: Path) -> Path | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        raw = urllib.request.urlopen(req, timeout=20).read()
    except Exception:
        return None
    if len(raw) < 4096:                                  # too small to be useful
        return None
    ext = Path(urllib.parse.urlparse(url).path).suffix.lower()
    dst = dst.with_suffix(ext if ext in IMG_EXT else ".jpg")
    dst.write_bytes(raw)
    return dst


# ── harvest ───────────────────────────────────────────────────────────────────
def harvest(args) -> int:
    queries = args.query if isinstance(args.query, list) else [args.query]
    with tempfile.TemporaryDirectory(prefix="imgharvest-") as td:
        work = Path(td)
        candidates: list[Path] = []
        for q in queries:
            urls = ddg_image_urls(q, args.candidates)
            print(f"[search] {len(urls)} image url(s) for {q!r}")
            for i, u in enumerate(urls):
                fp = download(u, work / f"{cv._slug(q)}-{i:02d}")
                if fp:
                    candidates.append(fp)
        print(f"[crop] {len(candidates)} downloaded candidate(s)")

        scored: list[tuple[float, cv.np.ndarray]] = []
        seen: list[int] = []
        for fp in candidates:
            img = cv2.imread(str(fp))
            if img is None:
                continue
            port = cv.autocrop(img)
            if port is None:
                continue
            bs = cv.blur_score(port)
            if bs < args.min_sharpness:
                continue
            hh = cv.ahash(port)
            if any(cv._hamming(hh, s) <= 6 for s in seen):    # near-duplicate
                continue
            seen.append(hh)
            scored.append((bs, port))

        if not scored:
            print("[crop] no usable portraits (no subject detected / too blurry)",
                  file=sys.stderr)
            return 1
        scored.sort(key=lambda t: -t[0])                     # sharpest first
        picks = [p for _, p in scored[:args.max]]
        slugs = cv.append_library(args.name, args.element, args.tier,
                                  [k.lower() for k in args.keywords],
                                  queries[0], args.credit, picks)
        print(f"[done] {len(slugs)} portrait(s) → {cv.LIB}: {', '.join(slugs)}")
        return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="imgharvest", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("query", nargs="+", help="image-search query/queries")
    p.add_argument("--name", required=True, help="character name (e.g. 'Gardevoir')")
    p.add_argument("--element", nargs="*", default=[],
                   help="card type(s) it fits: Normal Psychic Flying Steel …")
    p.add_argument("--tier", default="", help="rarity affinity (Common..Legendary)")
    p.add_argument("--keywords", nargs="*", default=[],
                   help="descriptive words used to match VM purpose")
    p.add_argument("--credit", default="", help="franchise / attribution")
    p.add_argument("--max", type=int, default=3, help="max portraits to keep")
    p.add_argument("--candidates", type=int, default=20,
                   help="image results to download per query before filtering")
    p.add_argument("--min-sharpness", type=float, default=45.0,
                   help="reject portraits below this Laplacian variance")
    return harvest(p.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
