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

from .cardex import Card, card_from_entry, load_entries, grade_curve, _registry_path, _seed
from . import chardex

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


def _art_chardex(card: Card) -> bytes:
    """Use a harvested real character portrait matched to this VM (free, offline)."""
    lib = chardex.load_library()
    ch = chardex.match(card, lib)
    if ch is None:
        raise RuntimeError("empty/unmatched character library")
    path = ch.path(chardex.chardex_dir())
    if not path.exists():
        raise RuntimeError(f"portrait missing: {path}")
    return path.read_bytes()


ART_BACKENDS = {
    "sigil": None,  # handled inline (no fetch)
    "chardex": _art_chardex,
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


def _wrap(s: str, n: int, maxlines: int = 3) -> list[str]:
    out, line = [], ""
    for w in s.split():
        if len(line) + len(w) + 1 > n:
            out.append(line)
            line = w
        else:
            line = (line + " " + w).strip()
    if line:
        out.append(line)
    if len(out) > maxlines:
        out = out[:maxlines]
        out[-1] = out[-1][:n - 1] + "…"
    return out


# premium-card visual constants
FOIL_RARITIES = {"Rare", "Epic", "Legendary"}
RARITY_GLINT = {  # lighter metallic highlight per rarity for the beveled border
    "Common": "#cfd4da", "Uncommon": "#c8f4d6", "Rare": "#cfe2ff",
    "Epic": "#ecc9ff", "Legendary": "#fff0b0",
}


def _card_defs(card: Card, border, glow, accent) -> str:
    """All gradients/filters/foil for one premium card (ids are card-local)."""
    tc = TYPE_COLOR.get(card.types[0], "#a0a4ab")
    glint = RARITY_GLINT.get(card.rarity, "#cfd4da")
    d = ['<defs>']
    # backdrop: deep vertical wash, type-tinted toward the foot
    d.append(f'<linearGradient id="bg" x1="0" y1="0" x2="0.2" y2="1">'
             f'<stop offset="0%" stop-color="#0c0e13"/>'
             f'<stop offset="55%" stop-color="#13161d"/>'
             f'<stop offset="100%" stop-color="{glow}"/></linearGradient>')
    # type spotlight behind the art
    d.append(f'<radialGradient id="spot" cx="50%" cy="34%" r="62%">'
             f'<stop offset="0%" stop-color="{tc}" stop-opacity="0.32"/>'
             f'<stop offset="100%" stop-color="{tc}" stop-opacity="0"/>'
             f'</radialGradient>')
    # brushed-metal border: light glint → rarity color → shadow, for a bevel
    d.append(f'<linearGradient id="metal" x1="0" y1="0" x2="1" y2="1">'
             f'<stop offset="0%" stop-color="{glint}"/>'
             f'<stop offset="28%" stop-color="{border}"/>'
             f'<stop offset="60%" stop-color="{border}"/>'
             f'<stop offset="78%" stop-color="#0c0e13"/>'
             f'<stop offset="100%" stop-color="{border}"/></linearGradient>')
    # title-banner sheen
    d.append(f'<linearGradient id="banner" x1="0" y1="0" x2="0" y2="1">'
             f'<stop offset="0%" stop-color="{border}" stop-opacity="0.95"/>'
             f'<stop offset="100%" stop-color="#0c0e13" stop-opacity="0.85"/>'
             f'</linearGradient>')
    # diagonal holographic foil (rainbow shimmer)
    d.append('<linearGradient id="foil" x1="0" y1="0" x2="1" y2="1">'
             '<stop offset="0%" stop-color="#ff5d8f" stop-opacity="0.0"/>'
             '<stop offset="20%" stop-color="#ffd166" stop-opacity="0.16"/>'
             '<stop offset="40%" stop-color="#06d6a0" stop-opacity="0.10"/>'
             '<stop offset="60%" stop-color="#4cc9f0" stop-opacity="0.18"/>'
             '<stop offset="80%" stop-color="#b8a6ff" stop-opacity="0.12"/>'
             '<stop offset="100%" stop-color="#ff5d8f" stop-opacity="0.0"/>'
             '</linearGradient>')
    d.append(f'<radialGradient id="vig" cx="50%" cy="50%" r="75%">'
             f'<stop offset="60%" stop-color="#000000" stop-opacity="0"/>'
             f'<stop offset="100%" stop-color="#000000" stop-opacity="0.45"/>'
             f'</radialGradient>')
    d.append(f'<filter id="glow" x="-30%" y="-30%" width="160%" height="160%">'
             f'<feGaussianBlur stdDeviation="6"/></filter>')
    d.append('</defs>')
    return "".join(d)


def _corner_gem(p: list, cx, cy, accent, glint):
    """A faceted diamond gem ornament at a frame corner."""
    p.append(f'<g transform="translate({cx},{cy}) rotate(45)">'
             f'<rect x="-9" y="-9" width="18" height="18" rx="3" fill="{accent}" '
             f'stroke="{glint}" stroke-width="1.5"/>'
             f'<rect x="-9" y="-9" width="18" height="9" rx="3" fill="{glint}" '
             f'opacity="0.55"/></g>')


def _frame_and_header(p: list, card: Card, border, glow, accent) -> tuple:
    """Shared premium frame + title banner + type gem + header. Returns status pill."""
    tc = TYPE_COLOR.get(card.types[0], "#a0a4ab")
    glint = RARITY_GLINT.get(card.rarity, "#cfd4da")
    p.append(_card_defs(card, border, glow, accent))
    # backdrop + spotlight + vignette
    p.append(f'<rect x="0" y="0" width="{W}" height="{H}" rx="24" fill="url(#bg)"/>')
    p.append(f'<rect x="14" y="80" width="{W-28}" height="330" rx="16" fill="url(#spot)"/>')
    # holo foil sheen for Rare+ / shiny
    if card.shiny or card.rarity in FOIL_RARITIES:
        p.append(f'<rect x="8" y="8" width="{W-16}" height="{H-16}" rx="20" '
                 f'fill="url(#foil)"/>')
        if card.shiny or card.rarity in ("Epic", "Legendary"):
            h = _seed(card.label)
            for k in range(7):
                sx = 30 + (h >> (k * 3)) % (W - 60)
                sy = 90 + (h >> (k * 3 + 1)) % (H - 180)
                r = 1.5 + (h >> (k * 2)) % 3
                p.append(f'<circle cx="{sx}" cy="{sy}" r="{r}" fill="#ffffff" '
                         f'opacity="0.7"/>')
    # multi-layer beveled border
    if card.rarity == "Legendary":      # outer glow ring for legendaries
        p.append(f'<rect x="9" y="9" width="{W-18}" height="{H-18}" rx="20" '
                 f'fill="none" stroke="{border}" stroke-width="10" '
                 f'filter="url(#glow)" opacity="0.7"/>')
    p.append(f'<rect x="7" y="7" width="{W-14}" height="{H-14}" rx="19" '
             f'fill="none" stroke="url(#metal)" stroke-width="8"/>')
    p.append(f'<rect x="13" y="13" width="{W-26}" height="{H-26}" rx="14" '
             f'fill="none" stroke="{accent}" stroke-width="1.5" opacity="0.7"/>')
    p.append(f'<rect x="0" y="0" width="{W}" height="{H}" rx="24" fill="url(#vig)"/>')
    for (gx, gy) in ((22, 22), (W - 22, 22), (22, H - 22), (W - 22, H - 22)):
        _corner_gem(p, gx, gy, accent, glint)
    # title banner + type energy gem + name
    p.append(f'<rect x="{PAD}" y="20" width="{W-2*PAD}" height="50" rx="12" '
             f'fill="url(#banner)" stroke="{accent}" stroke-width="1" opacity="0.96"/>')
    p.append(f'<circle cx="{PAD+26}" cy="45" r="15" fill="{tc}" '
             f'stroke="{glint}" stroke-width="2"/>')
    p.append(f'<circle cx="{PAD+21}" cy="40" r="5" fill="#ffffff" opacity="0.7"/>')
    p.append(f'<text x="{PAD+50}" y="44" font-size="25" font-weight="bold" '
             f'fill="#f7f9fb">{_esc(card.name)}</text>')
    holo = " · ★HOLO" if card.shiny else ""
    p.append(f'<text x="{PAD+50}" y="62" font-size="11" fill="{glint}">'
             f'No.{card.dex:03d} · {_esc(card.label)}{holo}</text>')
    p.append(f'<text x="{W-PAD-8}" y="40" text-anchor="end" font-size="15" '
             f'fill="#ffffff" font-weight="bold">PWR {card.power}</text>')
    bx = W - PAD - 8
    for ty in reversed(card.types):
        tcc = TYPE_COLOR.get(ty, "#a0a4ab")
        w = 16 + len(ty) * 8
        p.append(f'<rect x="{bx-w}" y="50" width="{w}" height="18" rx="9" fill="{tcc}" '
                 f'stroke="{glint}" stroke-width="0.75"/>')
        p.append(f'<text x="{bx-w/2}" y="63" text-anchor="middle" font-size="11" '
                 f'fill="#0c0f14" font-weight="bold">{_esc(ty)}</text>')
        bx -= w + 6
    st_done = card.status in ("done", "complete", "completed")
    return (("#3fae5a", f"✓ {card.status}") if st_done
            else ("#d8a23a", f"● {card.status or 'unknown'}"))


def _art_window(p: list, card: Card, art_data_uri, border, st_col, st_txt):
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
    p.append(f'<rect x="{ART_X}" y="{ART_Y+ART_H-26}" width="{ART_W}" height="26" '
             f'fill="{border}" opacity="0.85"/>')
    p.append(f'<text x="{W/2}" y="{ART_Y+ART_H-8}" text-anchor="middle" font-size="14" '
             f'fill="#0c0f14" font-weight="bold" letter-spacing="2">'
             f'{card.rarity.upper()}</text>')
    sw = 22 + len(st_txt) * 7
    p.append(f'<rect x="{ART_X+8}" y="{ART_Y+8}" width="{sw}" height="20" rx="10" '
             f'fill="{st_col}" opacity="0.92"/>')
    p.append(f'<text x="{ART_X+8+sw/2}" y="{ART_Y+22}" text-anchor="middle" '
             f'font-size="11" fill="#0c0f14" font-weight="bold">{_esc(st_txt)}</text>')


def _stat_bars(p: list, card: Card, y0: int, border, accent):
    s = card.stats
    bw = W - 2 * (PAD + 8)
    for i, (key, lbl) in enumerate(STAT_ORDER):
        y = y0 + i * 30
        val = s[key]
        p.append(f'<text x="{PAD+8}" y="{y+13}" font-size="13" fill="#c8ccd2" '
                 f'font-weight="bold">{lbl}</text>')
        p.append(f'<rect x="{PAD+50}" y="{y}" width="{bw-90}" height="16" rx="8" '
                 f'fill="#2a2e36"/>')
        fillw = max(6, (bw - 90) * val / 255)
        p.append(f'<rect x="{PAD+50}" y="{y}" width="{fillw:.0f}" height="16" rx="8" '
                 f'fill="{border}"/>')
        p.append(f'<text x="{W-PAD-8}" y="{y+13}" text-anchor="end" font-size="13" '
                 f'fill="#f4f6f8">{val}</text>')


def render_card_svg(card: Card, art_data_uri: str | None = None,
                    face: str = "full") -> str:
    """Render a card SVG. face: 'full' (art+stats), 'front' (art+description),
    'back' (stats+subscores) — front/back drive the flippable hub cards."""
    border, glow, accent = RARITY_THEME.get(card.rarity, RARITY_THEME["Common"])
    bst = sum(card.stats.values())
    p = [f'<svg xmlns="http://www.w3.org/2000/svg" '
         f'xmlns:xlink="http://www.w3.org/1999/xlink" width="{W}" height="{H}" '
         f'viewBox="0 0 {W} {H}" font-family="DejaVu Sans, Arial, sans-serif">']
    st_col, st_txt = _frame_and_header(p, card, border, glow, accent)

    if face in ("full", "front"):
        _art_window(p, card, art_data_uri, border, st_col, st_txt)

    if face == "front":
        # DOSSIER panel where stats live on the back — the VM's description.
        y0 = ART_Y + ART_H + 24
        p.append(f'<text x="{PAD+8}" y="{y0}" font-size="13" fill="{accent}" '
                 f'font-weight="bold" letter-spacing="2">VM DOSSIER</text>')
        p.append(f'<line x1="{PAD+8}" y1="{y0+8}" x2="{W-PAD-8}" y2="{y0+8}" '
                 f'stroke="{border}" stroke-width="1.5" opacity="0.6"/>')
        desc = card.purpose or card.flavor or "an unknown sandbox"
        for j, ln in enumerate(_wrap(desc, 46, maxlines=8)):
            p.append(f'<text x="{PAD+8}" y="{y0+30+j*22}" font-size="15" '
                     f'fill="#d6dae0">{_esc(ln)}</text>')
        p.append(f'<text x="{PAD+8}" y="{H-46}" font-size="12" fill="{accent}" '
                 f'font-weight="bold">PWR {card.power} · BST {bst} · '
                 f'{card.rarity}</text>')
        p.append(f'<text x="{PAD+26}" y="{H-30}" font-size="11" fill="#7f868f">'
                 f'tap to flip → stats</text>')
    elif face == "back":
        _stat_bars(p, card, ART_Y + 6, border, accent)
        # subscore breakdown + flavor under the bars
        fy = ART_Y + 6 + 6 * 30 + 14
        p.append(f'<text x="{PAD+8}" y="{fy}" font-size="13" fill="{accent}" '
                 f'font-weight="bold" letter-spacing="2">VALUE BREAKDOWN</text>')
        order = ["completeness", "reusability", "richness", "pedigree", "heft"]
        for k, sk in enumerate(order):
            v = card.subscores.get(sk, 0)
            p.append(f'<text x="{PAD+8}" y="{fy+24+k*20}" font-size="12" '
                     f'fill="#b8bdc4">{sk.capitalize():<14}</text>')
            p.append(f'<text x="{W-PAD-8}" y="{fy+24+k*20}" text-anchor="end" '
                     f'font-size="12" fill="#f4f6f8">{v}</text>')
        gy = fy + 24 + 5 * 20 + 18
        p.append(f'<text x="{PAD+8}" y="{gy}" font-size="13" fill="{accent}" '
                 f'font-weight="bold">BST {bst}</text>')
        for j, ln in enumerate(_wrap(card.flavor, 52, maxlines=3)):
            p.append(f'<text x="{PAD+8}" y="{gy+22+j*16}" font-size="11" '
                     f'fill="#9aa0a8">{_esc(ln)}</text>')
        p.append(f'<text x="{W-PAD-26}" y="{H-30}" text-anchor="end" font-size="11" '
                 f'fill="#7f868f">← tap to flip</text>')
    else:  # full
        _stat_bars(p, card, ART_Y + ART_H + 26, border, accent)
        fy = ART_Y + ART_H + 26 + 6 * 30 + 6
        p.append(f'<text x="{PAD+8}" y="{fy+8}" font-size="12" fill="{accent}" '
                 f'font-weight="bold">BST {bst}</text>')
        for j, ln in enumerate(_wrap(card.flavor, 58)):
            p.append(f'<text x="{PAD+8}" y="{fy+26+j*15}" font-size="11" '
                     f'fill="#9aa0a8">{_esc(ln)}</text>')
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


# ── browser gallery ───────────────────────────────────────────────────────────
RARITY_ORDER = {"Legendary": 0, "Epic": 1, "Rare": 2, "Uncommon": 3, "Common": 4}


def build_gallery(cards: list[Card], out_dir: Path, backend: str,
                  flip: bool = False) -> Path:
    """Render every card as an inline SVG and lay them out in one index.html.

    flip=False → a static grid of 'full' cards (art + stats), good for PNG export.
    flip=True  → an interactive hub: each card shows its VM dossier on the front
                 and flips on hover/click to reveal the stat block on the back."""
    out_dir.mkdir(parents=True, exist_ok=True)
    cards = sorted(cards, key=lambda c: (RARITY_ORDER.get(c.rarity, 9), -c.power))
    tally: dict[str, int] = {}
    cells = []
    for c in cards:
        art_uri = None
        if backend != "sigil":
            try:
                art_uri = fetch_art_data_uri(c, backend)
            except Exception as e:
                print(f"  ! {c.label}: {backend} art failed ({e}); sigil", file=sys.stderr)
        tally[c.rarity] = tally.get(c.rarity, 0) + 1
        if flip:
            front = render_card_svg(c, art_uri, face="front")
            back = render_card_svg(c, art_uri, face="back")
            cells.append(
                f'<div class="flip-card" tabindex="0"><div class="flip-inner">'
                f'<div class="flip-face flip-front">{front}</div>'
                f'<div class="flip-face flip-back">{back}</div>'
                f'</div></div>')
        else:
            cells.append(f'<div class="card">{render_card_svg(c, art_uri)}</div>')
        print(f"  rendered {c.rarity:<10} {c.label}")
    spread = " · ".join(f"{n}× {r}" for r, n in
                        sorted(tally.items(), key=lambda kv: RARITY_ORDER.get(kv[0], 9)))
    flip_css = (
        '.grid{display:grid;grid-template-columns:repeat(4,500px);gap:30px;'
        'padding:24px 36px 60px;justify-content:center}'
        '.flip-card{width:500px;height:700px;perspective:1600px;cursor:pointer;'
        'outline:none}'
        '.flip-inner{position:relative;width:100%;height:100%;'
        'transition:transform .7s cubic-bezier(.2,.7,.2,1);transform-style:preserve-3d}'
        '.flip-card:hover .flip-inner,.flip-card:focus .flip-inner,'
        '.flip-card.flipped .flip-inner{transform:rotateY(180deg)}'
        '.flip-face{position:absolute;inset:0;backface-visibility:hidden;'
        'border-radius:22px;box-shadow:0 10px 36px rgba(0,0,0,.65);overflow:hidden}'
        '.flip-face svg{width:500px;height:700px;display:block}'
        '.flip-back{transform:rotateY(180deg)}'
        '@media(max-width:2100px){.grid{grid-template-columns:repeat(3,500px)}}')
    static_css = (
        '.grid{display:grid;grid-template-columns:repeat(4,500px);gap:26px;'
        'padding:24px 36px 40px;justify-content:center}'
        '.card svg{width:500px;height:700px;display:block;'
        'border-radius:22px;box-shadow:0 8px 30px rgba(0,0,0,.6)}'
        '@media(max-width:2100px){.grid{grid-template-columns:repeat(3,500px)}}')
    sub = ('hover or tap a card to flip → stats' if flip
           else f'{len(cards)} saved VMs · {spread}')
    script = ('<script>document.querySelectorAll(".flip-card").forEach(c=>'
              'c.addEventListener("click",()=>c.classList.toggle("flipped")));'
              '</script>') if flip else ''
    html = (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<style>'
        'body{margin:0;background:radial-gradient(1200px 700px at 50% -10%,'
        '#161b24,#0a0c10 60%);color:#e8eaed;'
        'font-family:DejaVu Sans,Arial,sans-serif}'
        '.hd{padding:28px 36px 8px}'
        '.hd h1{margin:0;font-size:30px;letter-spacing:1px}'
        '.hd p{margin:6px 0 0;color:#9aa0a8;font-size:15px}'
        f'{flip_css if flip else static_css}'
        '</style></head><body>'
        f'<div class="hd"><h1>hack-house VM Cardex</h1>'
        f'<p>{len(cards)} saved VMs · {spread}{"  —  " + sub if flip else ""}</p></div>'
        f'<div class="grid">{"".join(cells)}</div>'
        f'{script}</body></html>')
    idx = out_dir / "index.html"
    idx.write_text(html)
    return idx


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
    p.add_argument("--gallery", action="store_true",
                   help="emit a single browser index.html grid of all cards")
    p.add_argument("--flip", action="store_true",
                   help="with --gallery: interactive flip cards (front=dossier, back=stats)")
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
    # rarity is a population quota — grade the full library on the curve so the
    # gallery matches `hh-cardex`. A lone --label card keeps its absolute rarity.
    if not args.label:
        grade_curve(cards)
    if args.prompt:
        for c in cards:
            print(f"# {c.label} [{c.rarity} {('/'.join(c.types))}]\n{build_prompt(c)}\n")
        return 0

    out = Path(args.out)
    if args.gallery:
        idx = build_gallery(cards, out, args.art, flip=args.flip)
        print(f"gallery → {idx}")
        return 0
    for c in cards:
        path = render_card(c, out, args.art, args.format)
        print(f"{c.rarity:<10} {c.label:<22} → {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
