# SPDX-License-Identifier: AGPL-3.0-or-later
"""Render-profile probe and Flamenco worker-tag derivation for this host."""

from __future__ import annotations

import platform
import re
import subprocess
from collections.abc import Callable, Iterable

from spectracut.queue.profile import RenderProfile

CommandRunner = Callable[[list[str]], str | None]
"""Runs a command and returns its stdout, or ``None`` if it is missing or fails."""

DEFAULT_CAPABILITIES: tuple[str, ...] = ("eevee", "cycles", "workbench")

_NVIDIA_SMI = [
    "nvidia-smi",
    "--query-gpu=name,driver_version,memory.total",
    "--format=csv,noheader,nounits",
]
_VENDOR_WORDS = re.compile(r"\b(nvidia|geforce|quadro|amd|radeon|intel|arc|rtx|gtx)\b")


def run_command(cmd: list[str]) -> str | None:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def parse_blender_version(output: str | None) -> str:
    match = re.search(r"Blender\s+(\d+\.\d+(?:\.\d+)?)", output or "")
    return match.group(1) if match else "unknown"


def parse_nvidia_smi(output: str | None) -> tuple[str, str, int]:
    """Return ``(gpu_model, driver, vram_gb)`` for the first GPU listed."""
    lines = [ln for ln in (output or "").splitlines() if ln.strip()]
    if not lines:
        return "unknown", "unknown", 0
    parts = [p.strip() for p in lines[0].split(",")]
    if len(parts) < 3:
        return "unknown", "unknown", 0
    try:
        vram_gb = round(float(parts[2]) / 1024)
    except ValueError:
        vram_gb = 0
    return parts[0] or "unknown", parts[1] or "unknown", max(vram_gb, 0)


def probe_profile(
    blender_bin: str = "blender", runner: CommandRunner = run_command
) -> RenderProfile:
    """Probe Blender and the GPU; unknown parts are reported as ``unknown``."""
    gpu_model, driver, vram_gb = parse_nvidia_smi(runner(_NVIDIA_SMI))
    return RenderProfile(
        blender_version=parse_blender_version(runner([blender_bin, "--version"])),
        gpu_model=gpu_model,
        gpu_driver=driver,
        vram_gb=vram_gb,
        os=platform.system().lower() or "unknown",
    )


def gpu_slug(gpu_model: str) -> str | None:
    """``NVIDIA GeForce RTX 4070 Ti`` -> ``4070ti``; ``None`` when unknown."""
    if gpu_model == "unknown":
        return None
    slug = re.sub(r"[^a-z0-9]", "", _VENDOR_WORDS.sub("", gpu_model.lower()))
    return slug or None


def worker_tags(
    profile: RenderProfile, capabilities: Iterable[str] = DEFAULT_CAPABILITIES
) -> list[str]:
    """Flamenco worker tags for this host: GPU model, VRAM and capabilities."""
    tags: list[str] = []
    slug = gpu_slug(profile.gpu_model)
    if slug:
        tags.append(slug)
    if profile.vram_gb > 0:
        tags.append(f"vram-{profile.vram_gb}gb")
    for cap in capabilities:
        cap = cap.strip().lower()
        if cap and cap not in tags:
            tags.append(cap)
    return tags
