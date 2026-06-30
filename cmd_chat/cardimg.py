"""hh-cardimg — render a cardex Card as a collectible trading-card image.

Free + local-first by design, with paid art backends wired in as opt-in options:

  art backend   cost      needs            what it draws in the art window
  ───────────   ──────    ───────────────  ───────────────────────────────
  sigil         free      nothing          a deterministic procedural creature
                (offline)                  glyph rendered in pure SVG (default)
  pollinations  free      network          Pollinations flux text-to-image (no key)
  stability     paid      STABILITY_API_KEY  Stability "stable-image core"
  openai        paid      OPENAI_API_KEY     OpenAI gpt-image-1
  runway        paid      RUNWAYML_API_SECRET  RunwayML gen4_image (async task)

The card *frame* (border, name, type badges, 6 stat bars, dex#, rarity holo,
flavor) is always hand-built SVG — no backend touches it, so a card always
renders fully offline; the chosen backend only fills the portrait window. The
SVG is self-contained (any fetched art is inlined as a base64 data URI) and is
rasterized to PNG locally via `cairosvg` or ImageMagick `convert` when present.

Everything is deterministic and seeded by the VM label: the same VM mints the
same card art (same Pollinations/paid seed, same sigil geometry).

  python -m cmd_chat.cardimg --label net-mapper-kit --art sigil --format png
  python -m cmd_chat.cardimg --all --art pollinations --out cards/
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from .cardex import Card, card_from_entry, load_entries, _registry_path, _seed

# ── card geometry ─────────────────────────────────────────────────────────────
W, H = 500, 700
PAD = 18
ART_X, ART_Y, ART_W, ART_H = PAD + 8, 96, W - 2 * (PAD + 8), 300

# rarity → frame palette (border, inner glow, accent text)
RARITY_THEME = {
    "Common":    ("#8a8f98", "#3a3f47", "#c8ccd2"),
    "Uncommon":  ("#3fae5a", "#13351f", "#9be8ae"),
    "Rare":      ("#3f7fd6", "#102742", "#9cc3f4"),
    "Epic":      ("#9a4fd0", "#2a1140", "#d6a8f4"),
    "Legendary": ("#e0a020", "#3a2705", "#ffd86b"),
}
# elemental type → badge color
TYPE_COLOR = {
    "Psychic": "#d44d8a", "Dark": "#5a5366", "Fire": "#e0622a", "Electric": "#e0b62a",
    "Ghost": "#7766b8", "Steel": "#8a96a6", "Flying": "#6fa8dc", "Dragon": "#5a4fd0",
    "Normal": "#a0a4ab", "Ground": "#c8a05a", "Water": "#3f8fd6",
}
# type → a couple of visual descriptors for the AI-art prompt
TYPE_VISUAL = {
    "Psychic": "luminous psionic aura, third eye, violet energy",
    "Dark": "shadowy, obsidian carapace, smoke wreathed",
    "Fire": "molten cracks, ember plumes, scorched scales",
    "Electric": "crackling arcs, neon circuitry, charged spines",
    "Ghost": "spectral, translucent, drifting wisps",
    "Steel": "armored plating, riveted chrome, bladed edges",
    "Flying": "feathered wings, wind currents, aerodynamic",
    "Dragon": "ancient draconic, horned, scaled wings",
    "Normal": "earthen, sturdy, understated",
    "Ground": "rocky hide, sediment, tectonic",
    "Water": "fluid, iridescent fins, deep currents",
}
STAT_ORDER = [("HP", "HP"), ("Attack", "ATK"), ("Defense", "DEF"),
              ("SpAtk", "SpA"), ("Speed", "SPE"), ("SpDef", "SpD")]


# ── procedural creature sigil (free, offline, deterministic) ──────────────────
def _sigil_svg(card: Card) -> str:
    """A deterministic radial creature glyph drawn from the label seed.

    No model, no network: a symmetric burst of limbs + an eye, colored by type.
    Distinct per VM (geometry, limb count, eye) yet stable across runs."""
    h = _seed(card.label)
    cx, cy = ART_X + ART_W / 2, ART_Y + ART_H / 2
    R = min(ART_W, ART_H) * 0.34
    col = TYPE_COLOR.get(card.types[0], "#a0a4ab")
    col2 = TYPE_COLOR.get(card.types[-1], col)
    limbs = 5 + (h % 6)                      # 5..10 radial limbs
    twist = (h >> 4) % 360
    parts = [f'<defs><radialGradient id="sg" cx="50%" cy="42%" r="62%">'
             f'<stop offset="0%" stop-color="{col2}" stop-opacity="0.95"/>'
             f'<stop offset="100%" stop-color="{col}" stop-opacity="0.15"/>'
             f'</radialGradient></defs>']
    # radial limbs
    for i in range(limbs):
        ang = math.radians(twist + i * 360.0 / limbs)
        seg = 3 + ((h >> (i + 2)) % 3)
        pts = []
        for s in range(seg + 1):
            rr = R * (0.35 + 0.75 * s / seg)
            wob = math.radians(((h >> (i + s)) % 40) - 20)
            x = cx + rr * math.cos(ang + wob)
            y = cy + rr * math.sin(ang + wob)
            pts.append(f"{x:.1f},{y:.1f}")
        parts.append(f'<polyline points="{" ".join(pts)}" fill="none" '
                     f'stroke="{col}" stroke-width="{6 - seg*0.6:.1f}" '
                     f'stroke-linecap="round" opacity="0.85"/>')
    # body
    parts.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{R*0.62:.1f}" fill="url(#sg)" '
                 f'stroke="{col2}" stroke-width="2"/>')
    # eye(s)
    eyes = 1 + (h % 3)
    for e in range(eyes):
        ex = cx + (e - (eyes - 1) / 2) * R * 0.36
        ey = cy - R * 0.06
        parts.append(f'<circle cx="{ex:.1f}" cy="{ey:.1f}" r="{R*0.16:.1f}" '
                     f'fill="#0c0f14"/>')
        parts.append(f'<circle cx="{ex:.1f}" cy="{ey-R*0.05:.1f}" r="{R*0.06:.1f}" '
                     f'fill="#ffffff" opacity="0.9"/>')
    return "".join(parts)


# ── AI-art prompt (deterministic text from the card) ──────────────────────────
def build_prompt(card: Card) -> str:
    ty = card.types[0]
    vis = ", ".join(TYPE_VISUAL.get(t, "") for t in card.types if TYPE_VISUAL.get(t))
    grandeur = {
        "Common": "humble small creature",
        "Uncommon": "spirited creature",
        "Rare": "powerful beast",
        "Epic": "fearsome legendary-class monster",
        "Legendary": "god-tier mythical titan, epic scale, awe-inspiring",
    }[card.rarity]
    return (f"a {grandeur} named {card.name}, {ty}-type mythical creature, {vis}, "
            f"centered character portrait, fantasy trading-card creature art, "
            f"dramatic rim lighting, clean dark background, highly detailed, "
            f"digital painting, no text, no border")


# ── art backends: return raw image bytes (PNG/JPEG) or raise ──────────────────
def _http(req: urllib.request.Request, timeout=120) -> bytes:
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _art_pollinations(card: Card) -> bytes:
    """Free hosted flux text-to-image — no API key."""
    prompt = urllib.parse.quote(build_prompt(card))
    seed = _seed(card.label) % 1_000_000
    url = (f"https://image.pollinations.ai/prompt/{prompt}"
           f"?width={ART_W*2}&height={ART_H*2}&seed={seed}&nologo=true&model=flux")
    return _http(urllib.request.Request(url, headers={"User-Agent": "hh-cardimg"}))


def _art_stability(card: Card) -> bytes:
    key = os.environ.get("STABILITY_API_KEY")
    if not key:
        raise RuntimeError("STABILITY_API_KEY not set")
    boundary = "----hhcardimg"
    parts, body = [], b""
    fields = {"prompt": build_prompt(card), "output_format": "png",
              "aspect_ratio": "1:1", "seed": str(_seed(card.label) % 4_000_000_000)}
    for k, v in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n')
    body = ("".join(parts) + f"--{boundary}--\r\n").encode()
    req = urllib.request.Request(
        "https://api.stability.ai/v2beta/stable-image/generate/core", data=body,
        headers={"Authorization": f"Bearer {key}", "Accept": "image/*",
                 "Content-Type": f"multipart/form-data; boundary={boundary}"})
    return _http(req)


def _art_openai(card: Card) -> bytes:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY not set")
    payload = json.dumps({"model": "gpt-image-1", "prompt": build_prompt(card),
                          "size": "1024x1024", "n": 1}).encode()
    req = urllib.request.Request(
        "https://api.openai.com/v1/images/generations", data=payload,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    out = json.loads(_http(req))
    return base64.b64decode(out["data"][0]["b64_json"])


def _art_runway(card: Card) -> bytes:
    """RunwayML gen4_image — async task: create, poll, download."""
    key = os.environ.get("RUNWAYML_API_SECRET")
    if not key:
        raise RuntimeError("RUNWAYML_API_SECRET not set")
    hdr = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
           "X-Runway-Version": "2024-11-06"}
    payload = json.dumps({"model": "gen4_image", "ratio": "1024:1024",
                          "promptText": build_prompt(card)}).encode()
    task = json.loads(_http(urllib.request.Request(
        "https://api.dev.runwayml.com/v1/text_to_image", data=payload, headers=hdr)))
    tid = task["id"]
    import time
    for _ in range(60):
        time.sleep(3)
        st = json.loads(_http(urllib.request.Request(
            f"https://api.dev.runwayml.com/v1/tasks/{tid}", headers=hdr)))
        if st.get("status") == "SUCCEEDED":
            return _http(urllib.request.Request(st["output"][0]))
        if st.get("status") in ("FAILED", "CANCELLED"):
            raise RuntimeError(f"runway task {st.get('status')}: {st.get('failure','')}")
    raise RuntimeError("runway task timed out")


ART_BACKENDS = {
    "sigil": None,  # handled inline (no fetch)
    "pollinations": _art_pollinations,
    "stability": _art_stability,
    "openai": _art_openai,
    "runway": _art_runway,
}


def fetch_art_data_uri(card: Card, backend: str) -> str | None:
    """Return a base64 data-URI for the portrait, or None to use the sigil."""
    fn = ART_BACKENDS.get(backend)
    if fn is None:
        return None
    raw = fn(card)
    mime = "image/png"
    if raw[:3] == b"\xff\xd8\xff":
        mime = "image/jpeg"
    return f"data:{mime};base64," + base64.b64encode(raw).decode()


# ── SVG card frame ────────────────────────────────────────────────────────────
def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _wrap(s: str, n: int) -> list[str]:
    out, line = [], ""
    for w in s.split():
        if len(line) + len(w) + 1 > n:
            out.append(line)
            line = w
        else:
            line = (line + " " + w).strip()
    if line:
        out.append(line)
    return out[:3]


def render_card_svg(card: Card, art_data_uri: str | None = None) -> str:
    border, glow, accent = RARITY_THEME.get(card.rarity, RARITY_THEME["Common"])
    s = card.stats
    bst = sum(s.values())
    p = [f'<svg xmlns="http://www.w3.org/2000/svg" '
         f'xmlns:xlink="http://www.w3.org/1999/xlink" width="{W}" height="{H}" '
         f'viewBox="0 0 {W} {H}" font-family="DejaVu Sans, Arial, sans-serif">']
    # defs: backdrop + holo
    p.append(f'<defs>'
             f'<linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">'
             f'<stop offset="0%" stop-color="#1a1d24"/>'
             f'<stop offset="100%" stop-color="{glow}"/></linearGradient>'
             f'<linearGradient id="holo" x1="0" y1="0" x2="1" y2="1">'
             f'<stop offset="0%" stop-color="{accent}" stop-opacity="0.0"/>'
             f'<stop offset="50%" stop-color="{accent}" stop-opacity="0.18"/>'
             f'<stop offset="100%" stop-color="{accent}" stop-opacity="0.0"/>'
             f'</linearGradient></defs>')
    # frame
    p.append(f'<rect x="0" y="0" width="{W}" height="{H}" rx="22" fill="url(#bg)"/>')
    p.append(f'<rect x="6" y="6" width="{W-12}" height="{H-12}" rx="18" '
             f'fill="none" stroke="{border}" stroke-width="5"/>')
    if card.shiny:
        p.append(f'<rect x="6" y="6" width="{W-12}" height="{H-12}" rx="18" '
                 f'fill="url(#holo)"/>')
    # header: name + dex (holo marker folded into the dex line to avoid PWR collision)
    p.append(f'<text x="{PAD+6}" y="40" font-size="26" font-weight="bold" '
             f'fill="#f4f6f8">{_esc(card.name)}</text>')
    holo = " · ★HOLO" if card.shiny else ""
    p.append(f'<text x="{PAD+6}" y="62" font-size="12" fill="{accent}">'
             f'No.{card.dex:03d} · {_esc(card.label)}{holo}</text>')
    p.append(f'<text x="{W-PAD-6}" y="40" text-anchor="end" font-size="15" '
             f'fill="#f4f6f8" font-weight="bold">PWR {card.power}</text>')
    # type badges (top-right under PWR)
    bx = W - PAD - 6
    for ty in reversed(card.types):
        tc = TYPE_COLOR.get(ty, "#a0a4ab")
        w = 16 + len(ty) * 8
        p.append(f'<rect x="{bx-w}" y="50" width="{w}" height="20" rx="10" fill="{tc}"/>')
        p.append(f'<text x="{bx-w/2}" y="64" text-anchor="middle" font-size="12" '
                 f'fill="#0c0f14" font-weight="bold">{_esc(ty)}</text>')
        bx -= w + 6
    # art window
    p.append(f'<rect x="{ART_X}" y="{ART_Y}" width="{ART_W}" height="{ART_H}" rx="12" '
             f'fill="#0c0f14" stroke="{border}" stroke-width="3"/>')
    if art_data_uri:
        p.append(f'<clipPath id="aw"><rect x="{ART_X}" y="{ART_Y}" width="{ART_W}" '
                 f'height="{ART_H}" rx="12"/></clipPath>')
        p.append(f'<image x="{ART_X}" y="{ART_Y}" width="{ART_W}" height="{ART_H}" '
                 f'preserveAspectRatio="xMidYMid slice" clip-path="url(#aw)" '
                 f'xlink:href="{art_data_uri}"/>')
    else:
        p.append(_sigil_svg(card))
    # rarity ribbon
    p.append(f'<rect x="{ART_X}" y="{ART_Y+ART_H-26}" width="{ART_W}" height="26" '
             f'fill="{border}" opacity="0.85"/>')
    p.append(f'<text x="{W/2}" y="{ART_Y+ART_H-8}" text-anchor="middle" font-size="14" '
             f'fill="#0c0f14" font-weight="bold" letter-spacing="2">'
             f'{card.rarity.upper()}</text>')
    # stat bars
    y0 = ART_Y + ART_H + 26
    bw = W - 2 * (PAD + 8)
    maxstat = 255
    for i, (key, lbl) in enumerate(STAT_ORDER):
        y = y0 + i * 30
        val = s[key]
        p.append(f'<text x="{PAD+8}" y="{y+13}" font-size="13" fill="#c8ccd2" '
                 f'font-weight="bold">{lbl}</text>')
        p.append(f'<rect x="{PAD+50}" y="{y}" width="{bw-90}" height="16" rx="8" '
                 f'fill="#2a2e36"/>')
        fillw = max(6, (bw - 90) * val / maxstat)
        p.append(f'<rect x="{PAD+50}" y="{y}" width="{fillw:.0f}" height="16" rx="8" '
                 f'fill="{border}"/>')
        p.append(f'<text x="{W-PAD-8}" y="{y+13}" text-anchor="end" font-size="13" '
                 f'fill="#f4f6f8">{val}</text>')
    # footer: BST + flavor
    fy = y0 + 6 * 30 + 6
    p.append(f'<text x="{PAD+8}" y="{fy+8}" font-size="12" fill="{accent}" '
             f'font-weight="bold">BST {bst}</text>')
    for j, ln in enumerate(_wrap(card.flavor, 58)):
        p.append(f'<text x="{PAD+8}" y="{fy+26+j*15}" font-size="11" '
                 f'fill="#9aa0a8"><tspan>{_esc(ln)}</tspan></text>')
    p.append("</svg>")
    return "".join(p)


# ── rasterize SVG → PNG locally ───────────────────────────────────────────────
def svg_to_png(svg_path: Path, png_path: Path) -> bool:
    if shutil.which("cairosvg"):
        subprocess.run(["cairosvg", str(svg_path), "-o", str(png_path)], check=True)
        return True
    conv = shutil.which("convert") or shutil.which("magick")
    if conv:
        subprocess.run([conv, "-density", "192", "-background", "none",
                        str(svg_path), str(png_path)], check=True)
        return True
    return False


def render_card(card: Card, out_dir: Path, backend: str, fmt: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    art_uri = None
    if backend != "sigil":
        try:
            art_uri = fetch_art_data_uri(card, backend)
        except Exception as e:                       # graceful: fall back to sigil
            print(f"  ! {card.label}: {backend} art failed ({e}); using sigil",
                  file=sys.stderr)
    svg = render_card_svg(card, art_uri)
    svg_path = out_dir / f"{card.label}.svg"
    svg_path.write_text(svg)
    if fmt == "svg":
        return svg_path
    png_path = out_dir / f"{card.label}.png"
    if svg_to_png(svg_path, png_path):
        return png_path
    print("  ! no local SVG rasterizer (cairosvg/convert); kept SVG", file=sys.stderr)
    return svg_path


# ── CLI ───────────────────────────────────────────────────────────────────────
def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="hh-cardimg",
                                description="Render cardex VM cards as images.")
    p.add_argument("--registry", default=str(_registry_path()))
    p.add_argument("--label", help="one VM (default: all)")
    p.add_argument("--all", action="store_true", help="render every VM in the registry")
    p.add_argument("--art", default="sigil", choices=list(ART_BACKENDS),
                   help="portrait backend (default: sigil = free + offline)")
    p.add_argument("--format", default="png", choices=["png", "svg"])
    p.add_argument("--out", default="cards", help="output directory")
    p.add_argument("--prompt", action="store_true",
                   help="just print the AI-art prompt(s) and exit")
    args = p.parse_args(argv)

    entries = load_entries(Path(args.registry))
    if args.label:
        entries = [e for e in entries if e.get("label") == args.label]
        if not entries:
            print(f"no VM labelled '{args.label}'")
            return 1
    elif not args.all:
        print("specify --label <vm> or --all")
        return 2

    cards = [card_from_entry(e) for e in entries]
    if args.prompt:
        for c in cards:
            print(f"# {c.label} [{c.rarity} {('/'.join(c.types))}]\n{build_prompt(c)}\n")
        return 0

    out = Path(args.out)
    for c in cards:
        path = render_card(c, out, args.art, args.format)
        print(f"{c.rarity:<10} {c.label:<22} → {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
