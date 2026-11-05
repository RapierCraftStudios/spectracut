# SPDX-License-Identifier: AGPL-3.0-or-later
"""Procedural sound design: a pure-Python reference renderer and SuperCollider NRT.

Names are resolved lazily from their defining modules.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS: dict[str, str] = {
    "PROCEDURAL_KINDS": "synth",
    "ProceduralGenerator": "synth",
    "active_span_seconds": "synth",
    "peak_time_seconds": "synth",
    "spectral_centroid": "synth",
    "SuperColliderGenerator": "supercollider",
    "build_score": "supercollider",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(f"spectracut.audio.procedural.{module}"), name)


def __dir__() -> list[str]:
    return sorted(_EXPORTS)
