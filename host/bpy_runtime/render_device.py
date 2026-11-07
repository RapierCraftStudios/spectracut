# SPDX-License-Identifier: AGPL-3.0-or-later
"""Render device selection inside Blender.

Jobs are built from recipes with ``--factory-startup`` and no ``.blend`` load, so
neither saved preferences nor Flamenco's ``load_post`` hook pick a GPU: Cycles
would render on its default CPU device. :func:`select_render_device` enables the
best GPU compute backend for Cycles (OptiX, else CUDA, HIP, Metal, oneAPI), and
falls back to the CPU only when no GPU device exists, with a warning.

EEVEE and Workbench rasterise on the GPU and have no device setting, so they are
reported as GPU without touching the Cycles preferences.
"""

from __future__ import annotations

from typing import Any

#: Cycles compute backends, most preferred first.
GPU_BACKENDS = ("OPTIX", "CUDA", "HIP", "METAL", "ONEAPI")
CPU_WARNING = "no GPU compute device found; rendering on the CPU"


def _devices(prefs: Any) -> list[Any]:
    for refresh in ("refresh_devices", "get_devices"):
        fn = getattr(prefs, refresh, None)
        if callable(fn):
            try:
                fn()
            except (TypeError, AttributeError, RuntimeError):
                continue
            break
    try:
        return list(prefs.devices)
    except TypeError:
        return []


def _try_backend(prefs: Any, kind: str) -> list[Any]:
    """Switch ``prefs`` to ``kind``; return its devices of that type, else ``[]``."""
    try:
        prefs.compute_device_type = kind
    except (TypeError, AttributeError, RuntimeError, ValueError):
        return []
    return [d for d in _devices(prefs) if getattr(d, "type", None) == kind]


def select_render_device(bpy: Any) -> dict[str, Any]:
    """Pick the render device for the current scene and say what was picked."""
    scene = bpy.context.scene
    engine = str(scene.render.engine)
    if engine != "CYCLES":
        return {"engine": engine, "device": "GPU", "backend": engine}
    prefs = bpy.context.preferences.addons["cycles"].preferences
    for kind in GPU_BACKENDS:
        found = _try_backend(prefs, kind)
        if not found:
            continue
        for device in _devices(prefs):
            device.use = getattr(device, "type", None) == kind
        scene.cycles.device = "GPU"
        if hasattr(scene.cycles, "denoising_use_gpu"):
            scene.cycles.denoising_use_gpu = True
        names = sorted(str(getattr(d, "name", "")) for d in found)
        return {"engine": engine, "device": "GPU", "backend": kind, "devices": names}
    try:
        prefs.compute_device_type = "NONE"
    except (TypeError, AttributeError, RuntimeError, ValueError):
        pass
    scene.cycles.device = "CPU"
    return {
        "engine": engine,
        "device": "CPU",
        "backend": "CPU",
        "devices": [],
        "warning": CPU_WARNING,
    }
