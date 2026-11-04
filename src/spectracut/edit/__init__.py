# SPDX-License-Identifier: AGPL-3.0-or-later
"""Edit timeline model, OpenTimelineIO import/export and conform.

The public names are resolved lazily from their defining modules, so each name
has exactly one definition site and importing the package stays cheap.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS: dict[str, str] = {
    "FPS_23_976": "timeline",
    "FPS_24": "timeline",
    "FPS_25": "timeline",
    "FPS_30": "timeline",
    "FPS_60": "timeline",
    "Clip": "timeline",
    "Edit": "timeline",
    "FrameRate": "timeline",
    "Gap": "timeline",
    "Marker": "timeline",
    "SpeedRamp": "timeline",
    "Track": "timeline",
    "Transition": "timeline",
    "find_shot": "conform",
    "replace_shot": "conform",
    "from_otio": "otio_io",
    "read_otio": "otio_io",
    "to_otio": "otio_io",
    "write_otio": "otio_io",
}


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(f"spectracut.edit.{module}"), name)


def __dir__() -> list[str]:
    return sorted(_EXPORTS)
