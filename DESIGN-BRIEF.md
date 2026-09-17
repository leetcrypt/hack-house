# DESIGN-BRIEF.md — `format-forge` (data-format converter bench)

Written by `vm-designer` (fleet `hackhouse-round2-tmp`) for `vm-builder2` to build from.
Design only — not built. See `./FLEET-vm-designer.md` for the mission this satisfies.

## Why this concept

Checked the live registry (`~/.hh/registry.json`, 81 entries) before picking a concept.
The catalog is overwhelmingly `ml-*-detector` / security-scanner clones (~50 entries), plus
a handful of CTF/crypto labs and one just-added game bench (`chess-engine-lab`, puzzle/game
category — already covers that lane). **Nothing touches structured-data tooling.** A
data-format converter bench is a clean, unclaimed third lane distinct from both ML-detection
and puzzle/game engines, genuinely useful as a dev tool, and small enough to build and prove
in one sitting.

## Purpose

`format-forge` — a one-command bench for converting and validating structured data across
JSON, YAML, TOML, CSV, and MessagePack, with JSON Schema validation and round-trip integrity
checks. Not a toy: real libraries, real edge cases (nested structures, numeric precision,
binary vs. text encoding), each conversion path independently verifiable.

## Base image

`docker.io/library/python:3.11-slim` — matches the box's other Python-only benches
(crypto-attack-lab, jwt-attack-lab), small, no compiled-runtime bloat needed.

## Packages

Pip (all pure-Python or well-maintained wheels, no exotic native deps):
- `PyYAML` — YAML read/write
- `toml` — TOML read/write
- `msgpack` — MessagePack binary encode/decode
- `jsonschema` — JSON Schema validation
- stdlib only for JSON and CSV (`json`, `csv`) — no extra package needed

## Layout (suggested)

```
/root/format-forge/
  convert.py          # CLI: convert.py <in.ext> <out.ext>, format inferred from extension
  schemas/
    config.schema.json
  samples/
    config.json        # nested config: strings, ints, floats, nested objects, lists
    dataset.csv         # tabular sample, ~20 rows, mixed types incl. a quoted-comma field
  roundtrip_check.py    # asserts semantic equality after A->B->A conversion
```

## Usage examples (must all pass — this is the proof it works)

1. **Round-trip JSON → YAML → TOML → JSON.** Take `samples/config.json` (nested dict,
   include at least one float like `3.14` and one list-of-dicts to stress ordering/typing),
   convert JSON→YAML→TOML→JSON via `convert.py`, and assert the final JSON is
   deep-equal to the original via `roundtrip_check.py`. Proves the converters aren't lossy
   on nesting or numeric types — the actual hard part of this domain.

2. **CSV ↔ JSON records, row-count + type check.** Convert `samples/dataset.csv`
   (with a quoted field containing an embedded comma, to catch a naive `split(",")`
   implementation) to JSON array-of-records and back to CSV. Assert row count is preserved
   and the quoted-comma field survives intact both directions.

3. **JSON Schema validation catches a real violation.** Validate `samples/config.json`
   against `schemas/config.schema.json` — expect pass. Then mutate one required field to the
   wrong type (e.g. a string where the schema requires an integer) and re-validate — expect
   `jsonschema.exceptions.ValidationError`, with the specific offending field named in the
   error message. Proves the schema check actually discriminates, not just "runs without
   crashing."

(Optional 4th if time allows: MessagePack round-trip of the same config, with a size
comparison printed vs. the JSON encoding — MessagePack is normally smaller, a nice concrete
"why would I use this" data point, but not required for Done.)

## Done bar for vm-builder2

All three usage examples above run for real inside the built sandbox and produce a
PASS/FAIL line each (not just "ran without exception" — actual assertions). Save + publish
per the existing `hh-operator` doctrine (own room, own sandbox, `manifest push`,
`sbx save` + `publish`), tagged something like `["data", "converters", "yaml", "json",
"schema", "dev-tool"]` so `hh-catalog-audit` can find it. Do not build this in the vm-designer
pane — this file is the handoff.
