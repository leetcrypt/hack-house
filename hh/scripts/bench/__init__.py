"""hh model-benchmark toolkit.

A small, extensible harness for answering one question: *which open-source model
works best for my workflow?* It has two axes, kept deliberately separate:

  • capability-per-language — can the model write correct Go/Rust/Python/Bash/JS?
    (driven by MultiPL-E + the original HumanEval, executed in a sandbox)
  • tool-path fitness — does the model behave on hack-house's own /ai chat and
    !task sandbox paths? (the existing bench-ai.py / bench-sandbox.py harnesses)

Both feed a common scorecard (score.py), which a workflow profile then weights
into a single ranked recommendation. Everything is dependency-light: model
completions go straight to Ollama's HTTP API, datasets come from the Hugging
Face datasets-server REST endpoint (no `datasets`/`pyarrow` install), and code
runs in rootless podman (with a host-toolchain fallback).
"""

__version__ = "0.1.0"
