"""Execution backends for model-generated code.

Two interchangeable runtimes implement ``run(lang, source, timeout) -> Exec``:

  • PodmanRuntime  — rootless, network-disabled, per-language image. The safe
    default: a 1.5B model's Rust is run in a throwaway container, not on the host.
  • LocalRuntime   — a throwaway temp dir using the host toolchain. Zero setup,
    no isolation; the fallback when podman is unavailable.

The harness only ever sees the Exec result, so swapping runtimes never touches
grading logic.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .langs import Lang


@dataclass
class Exec:
    ok: bool                 # exit 0 and not skipped/timed-out == tests passed
    rc: int | None
    out: str
    note: str = ""           # "timeout" | "image-missing" | error tail


class LocalRuntime:
    """Run in a temp dir with the host toolchain. No isolation — fallback only."""

    name = "local"

    def available(self, lang: Lang) -> bool:
        tool = lang.run.split()[0]
        return shutil.which(tool) is not None

    def run(self, lang: Lang, source: str, timeout: float) -> Exec:
        work = Path(tempfile.mkdtemp(prefix="hh-bench-"))
        try:
            (work / lang.filename).write_text(source)
            try:
                p = subprocess.run(["bash", "-c", lang.run], cwd=work,
                                   capture_output=True, text=True, timeout=timeout)
            except subprocess.TimeoutExpired:
                return Exec(False, None, "", "timeout")
            out = (p.stdout + p.stderr)
            return Exec(p.returncode == 0, p.returncode, out,
                        "" if p.returncode == 0 else out.strip()[-160:])
        finally:
            shutil.rmtree(work, ignore_errors=True)


class PodmanRuntime:
    """Run inside a rootless, network-less podman container per language."""

    name = "podman"

    def __init__(self, podman: str = "podman"):
        self.podman = podman

    def available(self, lang: Lang) -> bool:
        return shutil.which(self.podman) is not None

    def ensure_image(self, lang: Lang) -> bool:
        """Pull the language image if absent. Returns False if it can't be had."""
        have = subprocess.run([self.podman, "image", "exists", lang.image])
        if have.returncode == 0:
            return True
        pull = subprocess.run([self.podman, "pull", lang.image],
                              capture_output=True, text=True)
        return pull.returncode == 0

    def run(self, lang: Lang, source: str, timeout: float) -> Exec:
        if not self.ensure_image(lang):
            return Exec(False, None, "", f"image-missing: {lang.image}")
        work = Path(tempfile.mkdtemp(prefix="hh-bench-"))
        try:
            (work / lang.filename).write_text(source)
            cmd = [
                self.podman, "run", "--rm",
                "--network=none",          # model code never touches the network
                "--memory=512m", "--pids-limit=128",
                "-v", f"{work}:/w:Z", "-w", "/w",
                lang.image, "sh", "-c", lang.run,
            ]
            try:
                p = subprocess.run(cmd, capture_output=True, text=True,
                                   timeout=timeout)
            except subprocess.TimeoutExpired:
                return Exec(False, None, "", "timeout")
            out = (p.stdout + p.stderr)
            return Exec(p.returncode == 0, p.returncode, out,
                        "" if p.returncode == 0 else out.strip()[-160:])
        finally:
            shutil.rmtree(work, ignore_errors=True)


def get_runtime(kind: str = "auto", lang: Lang | None = None):
    """Pick a runtime. 'auto' prefers podman, falls back to local."""
    if kind == "podman":
        return PodmanRuntime()
    if kind == "local":
        return LocalRuntime()
    pod = PodmanRuntime()
    if pod.available(lang) if lang else shutil.which("podman"):
        return pod
    return LocalRuntime()
