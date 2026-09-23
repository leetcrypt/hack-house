# Session-music licensing & provenance

_Generated 2026-07-07 for the bundled albums under `hh/music/`._

This is the provenance + attribution register for the background-music albums that
the Rust client plays during a session (`/music`, see `hh/src/music.rs`). Each album
is a `hh/music/<name>.json` manifest pointing at the `.mp3` files shipped alongside it.

## Verdict: ALL bundled tracks CLEARED — CC BY 4.0, attribution required

- **2 albums / 11 tracks**, every one composed by **Kevin MacLeod** and published on
  **incompetech.com** under **Creative Commons Attribution 4.0 International (CC BY 4.0)**.
- CC BY 4.0 **permits** redistribution, bundling, and commercial use **provided the
  author is credited**. That makes these safe to commit to the repo, push to any
  remote, demo publicly, and ship — unlike the quarantined character art
  (`character-art-licensing.md`).
- Attribution is carried in two machine-readable places already: the `license` field
  of each `hh/music/*.json` manifest, and this register. Keep both intact.

### Required attribution (CC BY 4.0)

> Music by **Kevin MacLeod** (incompetech.com) — licensed under
> **Creative Commons: By Attribution 4.0** — https://creativecommons.org/licenses/by/4.0/

Surface this credit wherever the music is used publicly (demo video description,
release notes, an About/Credits screen). Do not remove the `license` fields from the
manifests, and do not relabel these tracks as original or royalty-free-without-credit.

## Per-track register

| Album | Track | Composer | Source | Licence | Status |
|-------|-------|----------|--------|---------|--------|
| crypt | Ossuary 5 - Rest | Kevin MacLeod | incompetech.com | CC BY 4.0 | CLEARED |
| crypt | Ghostpocalypse - 6 Crossing the Threshold | Kevin MacLeod | incompetech.com | CC BY 4.0 | CLEARED |
| crypt | Crypto | Kevin MacLeod | incompetech.com | CC BY 4.0 | CLEARED |
| crypt | Killers | Kevin MacLeod | incompetech.com | CC BY 4.0 | CLEARED |
| crypt | Hitman | Kevin MacLeod | incompetech.com | CC BY 4.0 | CLEARED |
| terminal | Neon Laser Horizon | Kevin MacLeod | incompetech.com | CC BY 4.0 | CLEARED |
| terminal | Cyborg Ninja | Kevin MacLeod | incompetech.com | CC BY 4.0 | CLEARED |
| terminal | Space Fighter Loop | Kevin MacLeod | incompetech.com | CC BY 4.0 | CLEARED |
| terminal | Voltaic | Kevin MacLeod | incompetech.com | CC BY 4.0 | CLEARED |
| terminal | Blip Stream | Kevin MacLeod | incompetech.com | CC BY 4.0 | CLEARED |
| terminal | Pixelland | Kevin MacLeod | incompetech.com | CC BY 4.0 | CLEARED |

> `crypt` is the dark-ambient album (matches the default "crypt" vestment); `terminal`
> is the hacker synth/chiptune album. Files live under `hh/music/<album>/`.

## Imported albums (`/music import`)

`/music import <path>` writes user albums to `~/.hh/music/*.json`, referencing the
operator's own files **in place** (absolute paths — nothing is copied into the repo).
Those files are the operator's responsibility and are **out of scope** for this
register; only the bundled `hh/music/` albums above are shipped and covered here.
