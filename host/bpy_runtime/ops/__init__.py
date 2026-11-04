# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared pieces of the declarative bpy ops: errors, argument checks, lookups.

Every op validates its JSON arguments with these helpers before touching
``bpy``. Errors carry a stable dotted ``code`` so callers (the harness MCP
server, the runner's refusal line) can react without parsing messages. This
package is stdlib-only and never imports ``spectracut``.
"""

from __future__ import annotations

import math
from typing import Any

ERROR_CODES = (
    "args.invalid",
    "tool.unknown",
    "scene.no_camera",
    "scene.object_not_found",
    "asset.path_refused",
    "framing.subject_out_of_frame",
)

Report = dict[str, Any] | None


class ToolError(ValueError):
    """A recipe step names an unknown tool, passes invalid arguments or can't run.

    ``code`` is one of :data:`ERROR_CODES`.
    """

    def __init__(self, message: str, code: str = "args.invalid") -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"unknown ToolError code {code!r}")
        super().__init__(message)
        self.code = code


def check_only(args: dict[str, Any], allowed: set[str], tool: str) -> None:
    extra = set(args) - allowed
    if extra:
        raise ToolError(f"{tool}: unknown argument(s) {sorted(extra)}")


def as_num(value: Any, what: str, lo: float | None = None, hi: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ToolError(f"{what} must be a finite number")
    if (lo is not None and value < lo) or (hi is not None and value > hi):
        raise ToolError(f"{what} must be within [{lo}, {hi}]")
    return float(value)


def as_int(value: Any, what: str, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise ToolError(f"{what} must be an integer within [{lo}, {hi}]")
    return value


def as_bool(value: Any, what: str) -> bool:
    if not isinstance(value, bool):
        raise ToolError(f"{what} must be true or false")
    return value


def as_vec3(value: Any, what: str) -> tuple[float, float, float]:
    if not isinstance(value, list) or len(value) != 3:
        raise ToolError(f"{what} must be a list of 3 numbers")
    x, y, z = (as_num(v, f"{what}[{i}]") for i, v in enumerate(value))
    return (x, y, z)


def as_name(value: Any, what: str) -> str:
    if not isinstance(value, str) or not 0 < len(value) <= 63:
        raise ToolError(f"{what} must be a non-empty string of at most 63 characters")
    return value


def as_choice(value: Any, what: str, choices: dict[str, str]) -> str:
    if not isinstance(value, str) or value not in choices:
        raise ToolError(f"{what} must be one of {sorted(choices)}")
    return choices[value]


def active_camera(bpy: Any, tool: str) -> Any:
    camera = bpy.context.scene.camera
    if camera is None:
        raise ToolError(
            f"{tool}: the scene has no camera; add one with camera.add", "scene.no_camera"
        )
    return camera


def find_object(bpy: Any, name: Any, tool: str, what: str = "object") -> Any:
    obj = bpy.data.objects.get(as_name(name, what))
    if obj is None:
        raise ToolError(f"{tool}: no object named {name!r}", "scene.object_not_found")
    return obj


def eevee_id(bpy: Any) -> str:
    # Blender 4.2-4.x names the new EEVEE "BLENDER_EEVEE_NEXT".
    version = tuple(bpy.app.version)
    return "BLENDER_EEVEE_NEXT" if (4, 2) <= version[:2] < (5, 0) else "BLENDER_EEVEE"


ENGINES = {"eevee": "EEVEE", "cycles": "CYCLES", "workbench": "BLENDER_WORKBENCH"}


def engine_id(bpy: Any, engine: str) -> str:
    """Map a validated :data:`ENGINES` value to this Blender's engine identifier."""
    return eevee_id(bpy) if engine == "EEVEE" else engine


def mesh_objects(bpy: Any) -> list[Any]:
    return [o for o in bpy.data.objects if o.type == "MESH"]
