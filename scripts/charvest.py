#!/usr/bin/env python3
"""charvest — harvest + auto-crop iconic character portraits for the hh card art.

Pipeline (all local, free):

    source ──▶ frames ──▶ auto-crop subject ──▶ quality filter ──▶ library
    (URL/video/                (cv2: anime+face        (blur + near-dup
     image/dir)   (ffmpeg       cascades, else          rejection)
                   scene)       edge-energy saliency)

Acquisition reuses the `web-video-grab` skill (yt-dlp first, Playwright screen-
capture fallback) so YouTube / Instagram / X / TikTok all work; a local file or
a directory of images is taken as-is. Each kept portrait is squared, upscaled
and written into the chardex library (`assets/characters/`) with the metadata
you supply, so `cmd_chat/chardex.py` can match VMs to faces.

Run with the anaconda interpreter (has cv2 + numpy):

    python3 scripts/charvest.py URL_OR_PATH --name "Charizard" \
        --element Fire Dragon --tier Legendary \
        --keywords dragon fire wings aggressive flagship --max 3

⚠️  Harvested characters are likely third-party IP — see
    docs/character-art-licensing.md before any public/commercial use.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

import cv2
import numpy as np

REPO = Path(__file__).resolve().parent.parent
LIB = Path(os.environ.get("HH_CHARDEX_DIR", REPO / "assets" / "characters"))
GRAB = Path.home() / ".claude" / "skills" / "web-video-grab" / "grab.sh"
CACHE = Path.home() / ".cache" / "hh-charvest"
ANIMEFACE_URL = ("https://raw.githubusercontent.com/nagadomi/"
                 "lbpcascade_animeface/master/lbpcascade_animeface.xml")
OUT_PX = 640
ACQUIRE_SECONDS = 30
IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
VID_EXT = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".gif"}


# ── acquisition ───────────────────────────────────────────────────────────────
def acquire(src: str, work: Path) -> tuple[str, Path]:
    """Return ('image'|'video'|'dir', path). URLs are fetched to a clean mp4."""
    p = Path(src)
    if p.exists():
        if p.is_dir():
            return "dir", p
        ext = p.suffix.lower()
        if ext in IMG_EXT:
            return "image", p
        if ext in VID_EXT:
            return "video", p
        raise SystemExit(f"unsupported local file type: {ext}")
    # remote URL → grab just a short, low-res section fast via yt-dlp (no full
    # download, no Playwright). Falls back to the web-video-grab skill if needed.
    out = work / "grab.mp4"
    print(f"[acquire] yt-dlp (≤{ACQUIRE_SECONDS}s clip) ← {src}")
    r = subprocess.run(
        ["yt-dlp", "--no-playlist", "--no-warnings",
         "-f", "bv*[height<=720][ext=mp4]/bv*[height<=720]/b[height<=720]/best",
         "--download-sections", f"*0:00-0:{ACQUIRE_SECONDS:02d}",
         "-o", str(out), src],
        timeout=150)
    if r.returncode == 0 and out.exists():
        return "video", out
    if GRAB.exists():
        print("[acquire] yt-dlp failed; web-video-grab fallback", file=sys.stderr)
        r = subprocess.run(["bash", str(GRAB), "--out", str(out), src], timeout=180)
        if r.returncode == 0 and out.exists():
            return "video", out
    raise SystemExit(f"could not acquire media from {src}")


def extract_frames(video: Path, outdir: Path, scene: float, fps_cap: float,
                   max_frames: int) -> list[Path]:
    """ffmpeg: prefer scene-change frames; fall back to a steady fps sample."""
    outdir.mkdir(parents=True, exist_ok=True)
    vf = f"select='gt(scene,{scene})',scale=720:-2"
    subprocess.run(["ffmpeg", "-y", "-i", str(video), "-vf", vf, "-vsync", "vfr",
                    "-frames:v", str(max_frames * 3), str(outdir / "s%04d.png")],
                   stderr=subprocess.DEVNULL)
    frames = sorted(outdir.glob("s*.png"))
    if len(frames) < 3:                      # sparse scene cuts → steady sample
        subprocess.run(["ffmpeg", "-y", "-i", str(video), "-vf",
                        f"fps={fps_cap},scale=720:-2", "-frames:v",
                        str(max_frames * 3), str(outdir / "f%04d.png")],
                       stderr=subprocess.DEVNULL)
        frames = sorted(outdir.glob("[sf]*.png"))
    return frames


# ── subject detection + cropping ──────────────────────────────────────────────
def _animeface_cascade():
    CACHE.mkdir(parents=True, exist_ok=True)
    dst = CACHE / "lbpcascade_animeface.xml"
    if not dst.exists():
        try:
            urllib.request.urlretrieve(ANIMEFACE_URL, dst)
        except Exception as e:
            print(f"[crop] anime cascade unavailable ({e}); faces only", file=sys.stderr)
            return None
    c = cv2.CascadeClassifier(str(dst))
    return None if c.empty() else c


_FRONTAL = cv2.CascadeClassifier(
    cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
_ANIME = _animeface_cascade()


def _face_bbox(gray: np.ndarray):
    """Largest face (anime cascade first, then frontal); None if no face."""
    best = None
    for cas, mn in ((_ANIME, 24), (_FRONTAL, 30)):
        if cas is None:
            continue
        for (x, y, w, h) in cas.detectMultiScale(gray, 1.1, 4, minSize=(mn, mn)):
            if best is None or w * h > best[2] * best[3]:
                best = (x, y, w, h)
        if best is not None:
            break
    return best


def _saliency_bbox(gray: np.ndarray):
    """Edge-energy fallback for creatures/objects with no detectable face.

    Sobel magnitude → blur to an energy map → threshold at mean+std → bounding
    box of the largest salient blob."""
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    mag = cv2.magnitude(gx, gy)
    mag = cv2.GaussianBlur(mag, (0, 0), sigmaX=gray.shape[1] / 40.0)
    thr = mag.mean() + mag.std()
    mask = (mag > thr).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE,
                            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15)))
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return None
    return cv2.boundingRect(max(cnts, key=cv2.contourArea))


def _square_from_face(bbox, W, H):
    x, y, w, h = bbox
    cx = x + w / 2
    side = min(max(w * 2.4, h * 2.4), min(W, H))   # head+shoulders portrait
    top = y - h * 0.9                               # leave headroom above face
    x0 = int(np.clip(cx - side / 2, 0, W - side))
    y0 = int(np.clip(top, 0, H - side))
    return x0, y0, int(side)


def _square_from_bbox(bbox, W, H, pad=0.18):
    x, y, w, h = bbox
    cx, cy = x + w / 2, y + h / 2
    if h > w * 1.25:
        # tall standing figure (full-body art): frame head + upper body from the
        # top instead of centering on the waist, so the face is always in shot.
        side = float(np.clip(max(w * 1.5, h * 0.55), 0.42 * min(W, H), min(W, H)))
        y0 = int(np.clip(y - side * 0.06, 0, H - side))
    else:
        side = min(max(w, h) * (1 + pad), min(W, H))
        y0 = int(np.clip(cy - side / 2, 0, H - side))
    x0 = int(np.clip(cx - side / 2, 0, W - side))
    return x0, y0, int(side)


def autocrop(img: np.ndarray):
    """Return a squared OUT_PX×OUT_PX portrait centered on the subject, or None."""
    H, W = img.shape[:2]
    if min(H, W) < 120:
        return None
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    face = _face_bbox(gray)
    if face is not None:
        x0, y0, side = _square_from_face(face, W, H)
    else:
        bb = _saliency_bbox(gray)
        if bb is None:
            return None
        x0, y0, side = _square_from_bbox(bb, W, H)
    if side < 100:
        return None
    crop = img[y0:y0 + side, x0:x0 + side]
    crop = cv2.resize(crop, (OUT_PX, OUT_PX), interpolation=cv2.INTER_LANCZOS4)
    # gentle unsharp for upscaled frames
    blur = cv2.GaussianBlur(crop, (0, 0), 1.2)
    return cv2.addWeighted(crop, 1.4, blur, -0.4, 0)


# ── quality filters ───────────────────────────────────────────────────────────
def blur_score(img: np.ndarray) -> float:
    return cv2.Laplacian(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var()


def ahash(img: np.ndarray) -> int:
    g = cv2.resize(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), (8, 8))
    bits = (g > g.mean()).flatten()
    h = 0
    for b in bits:
        h = (h << 1) | int(b)
    return h


def _hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


# ── library write ─────────────────────────────────────────────────────────────
def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "char"


def append_library(name, element, tier, keywords, source, credit,
                   portraits: list[np.ndarray]) -> list[str]:
    LIB.mkdir(parents=True, exist_ok=True)
    idx_path = LIB / "index.json"
    index = json.loads(idx_path.read_text()) if idx_path.exists() else []
    base = _slug(name)
    written = []
    for i, port in enumerate(portraits):
        slug = base if len(portraits) == 1 else f"{base}-{i+1}"
        index = [e for e in index if e["slug"] != slug]    # replace same slug
        fn = f"{slug}.png"
        cv2.imwrite(str(LIB / fn), port, [cv2.IMWRITE_PNG_COMPRESSION, 6])
        index.append({"slug": slug, "name": name, "file": fn,
                      "element": element, "tier": tier or "",
                      "keywords": keywords, "source": source, "credit": credit})
        written.append(slug)
    idx_path.write_text(json.dumps(index, indent=2))
    return written


def harvest(args) -> int:
    with tempfile.TemporaryDirectory(prefix="charvest-") as td:
        work = Path(td)
        kind, path = acquire(args.url, work)
        if kind == "image":
            frames = [path]
        elif kind == "dir":
            frames = sorted(p for p in path.iterdir() if p.suffix.lower() in IMG_EXT)
        else:
            frames = extract_frames(path, work / "frames", args.scene,
                                    args.fps, args.max)
        print(f"[crop] {len(frames)} candidate frame(s)")

        scored: list[tuple[float, int, np.ndarray]] = []
        seen: list[int] = []
        for fp in frames:
            img = cv2.imread(str(fp))
            if img is None:
                continue
            port = autocrop(img)
            if port is None:
                continue
            bs = blur_score(port)
            if bs < args.min_sharpness:
                continue
            hh = ahash(port)
            if any(_hamming(hh, s) <= 6 for s in seen):      # near-duplicate
                continue
            seen.append(hh)
            scored.append((bs, len(scored), port))

        if not scored:
            print("[crop] no usable portraits (no subject detected / too blurry)",
                  file=sys.stderr)
            return 1
        scored.sort(key=lambda t: -t[0])                     # sharpest first
        picks = [p for _, _, p in scored[:args.max]]
        slugs = append_library(args.name, args.element, args.tier,
                               [k.lower() for k in args.keywords],
                               args.url, args.credit, picks)
        print(f"[done] {len(slugs)} portrait(s) → {LIB}: {', '.join(slugs)}")
        return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="charvest", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("url", help="YouTube/Instagram/web URL, or a local video/image/dir")
    p.add_argument("--name", required=True, help="character name (e.g. 'Charizard')")
    p.add_argument("--element", nargs="*", default=[],
                   help="card type(s) it fits: Fire Dragon Psychic Steel …")
    p.add_argument("--tier", default="", help="rarity affinity (Common..Legendary)")
    p.add_argument("--keywords", nargs="*", default=[],
                   help="descriptive words used to match VM purpose")
    p.add_argument("--credit", default="", help="franchise / attribution")
    p.add_argument("--max", type=int, default=3, help="max portraits to keep")
    p.add_argument("--scene", type=float, default=0.30, help="ffmpeg scene-cut threshold")
    p.add_argument("--fps", type=float, default=0.5, help="fallback sample fps")
    p.add_argument("--min-sharpness", type=float, default=40.0,
                   help="reject portraits below this Laplacian variance")
    return harvest(p.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
