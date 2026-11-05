# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tutorial, explainer, ad and trailer assemblers plus the edit-rule linter.

The public names are resolved lazily from their defining modules, so each name
has exactly one definition site and importing the package stays cheap.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "AspectRender": "cutdowns",
    "CutDownLength": "cutdowns",
    "CutDownSpec": "cutdowns",
    "MissingRender": "cutdowns",
    "RenderLibrary": "cutdowns",
    "Variant": "cutdowns",
    "cut_down": "cutdowns",
    "required_renders": "cutdowns",
    "variant_matrix": "cutdowns",
    "EditRuleError": "linter",
    "LintIssue": "linter",
    "ShotLook": "linter",
    "ShotScale": "linter",
    "ensure_clean": "linter",
    "lint": "linter",
    "PLATFORM_SAFE_AREAS": "safe_areas",
    "ExportPreset": "safe_areas",
    "Overlay": "safe_areas",
    "SafeArea": "safe_areas",
    "SafeAreaError": "safe_areas",
    "export_preset": "safe_areas",
    "inside_safe_area": "safe_areas",
    "safe_area": "safe_areas",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(f"spectracut.formats.{module}"), name)


def __dir__() -> list[str]:
    return sorted(_EXPORTS)
