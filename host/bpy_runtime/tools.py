# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fixed registry of declarative recipe tools, run inside Blender only.

A recipe step names one of these tools and passes JSON arguments; there is no
way to run arbitrary Python. Every tool validates its arguments before touching
``bpy``. This module is stdlib-only and never imports ``spectracut``.

``TOOL_NAMES`` is a literal so the host guard can read it without importing this
module (the control plane and host agent never import the bpy runtime).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

TOOL_NAMES = frozenset(
    {
        "scene.clear",
        "object.add_primitive",
        "camera.add",
        "camera.set_lens",
        "camera.set_transform",
        "light.add",
        "render.set_engine",
        "render.set_resolution",
    }
)


class ToolError(ValueError):
    """A recipe step names an unknown tool or passes invalid arguments."""


def _only(args: dict[str, Any], allowed: set[str], tool: str) -> None:
    extra = set(args) - allowed
    if extra:
        raise ToolError(f"{tool}: unknown argument(s) {sorted(extra)}")


def _num(value: Any, what: str, lo: float | None = None, hi: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ToolError(f"{what} must be a finite number")
    if (lo is not None and value < lo) or (hi is not None and value > hi):
        raise ToolError(f"{what} must be within [{lo}, {hi}]")
    return float(value)


def _int(value: Any, what: str, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise ToolError(f"{what} must be an integer within [{lo}, {hi}]")
    return value


def _vec3(value: Any, what: str) -> tuple[float, float, float]:
    if not isinstance(value, list) or len(value) != 3:
        raise ToolError(f"{what} must be a list of 3 numbers")
    x, y, z = (_num(v, f"{what}[{i}]") for i, v in enumerate(value))
    return (x, y, z)


def _name(value: Any, what: str) -> str:
    if not isinstance(value, str) or not 0 < len(value) <= 63:
        raise ToolError(f"{what} must be a non-empty string of at most 63 characters")
    return value


def _choice(value: Any, what: str, choices: dict[str, str]) -> str:
    if value not in choices:
        raise ToolError(f"{what} must be one of {sorted(choices)}")
    return choices[value]


def scene_clear(bpy: Any, args: dict[str, Any]) -> None:
    _only(args, set(), "scene.clear")
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)


_PRIMITIVES = {
    "cube": "primitive_cube_add",
    "plane": "primitive_plane_add",
    "uv_sphere": "primitive_uv_sphere_add",
    "cylinder": "primitive_cylinder_add",
}


def object_add_primitive(bpy: Any, args: dict[str, Any]) -> None:
    _only(args, {"kind", "name", "location", "scale"}, "object.add_primitive")
    op = _choice(args.get("kind"), "kind", _PRIMITIVES)
    location = _vec3(args.get("location", [0, 0, 0]), "location")
    getattr(bpy.ops.mesh, op)(location=location)
    obj = bpy.context.active_object
    if "name" in args:
        obj.name = _name(args["name"], "name")
    if "scale" in args:
        obj.scale = _vec3(args["scale"], "scale")


def _link(bpy: Any, obj: Any) -> None:
    bpy.context.scene.collection.objects.link(obj)


def camera_add(bpy: Any, args: dict[str, Any]) -> None:
    _only(args, {"name", "location", "rotation", "lens"}, "camera.add")
    name = _name(args.get("name", "Camera"), "name")
    data = bpy.data.cameras.new(name)
    if "lens" in args:
        data.lens = _num(args["lens"], "lens", 1, 5000)
    obj = bpy.data.objects.new(name, data)
    obj.location = _vec3(args.get("location", [0, 0, 0]), "location")
    obj.rotation_euler = _vec3(args.get("rotation", [0, 0, 0]), "rotation")
    _link(bpy, obj)
    bpy.context.scene.camera = obj


def _camera(bpy: Any, tool: str) -> Any:
    camera = bpy.context.scene.camera
    if camera is None:
        raise ToolError(f"{tool}: the scene has no camera; add one with camera.add")
    return camera


def camera_set_lens(bpy: Any, args: dict[str, Any]) -> None:
    _only(args, {"mm"}, "camera.set_lens")
    _camera(bpy, "camera.set_lens").data.lens = _num(args.get("mm"), "mm", 1, 5000)


def camera_set_transform(bpy: Any, args: dict[str, Any]) -> None:
    _only(args, {"location", "rotation"}, "camera.set_transform")
    camera = _camera(bpy, "camera.set_transform")
    if "location" in args:
        camera.location = _vec3(args["location"], "location")
    if "rotation" in args:
        camera.rotation_euler = _vec3(args["rotation"], "rotation")


_LIGHTS = {"point": "POINT", "sun": "SUN", "area": "AREA", "spot": "SPOT"}


def light_add(bpy: Any, args: dict[str, Any]) -> None:
    _only(args, {"kind", "name", "energy", "location", "rotation"}, "light.add")
    kind = _choice(args.get("kind", "point"), "kind", _LIGHTS)
    name = _name(args.get("name", "Light"), "name")
    data = bpy.data.lights.new(name, type=kind)
    data.energy = _num(args.get("energy", 1000), "energy", 0, 1e7)
    obj = bpy.data.objects.new(name, data)
    obj.location = _vec3(args.get("location", [0, 0, 0]), "location")
    obj.rotation_euler = _vec3(args.get("rotation", [0, 0, 0]), "rotation")
    _link(bpy, obj)


def _eevee_id(bpy: Any) -> str:
    # Blender 4.2-4.x names the new EEVEE "BLENDER_EEVEE_NEXT".
    version = tuple(bpy.app.version)
    return "BLENDER_EEVEE_NEXT" if (4, 2) <= version[:2] < (5, 0) else "BLENDER_EEVEE"


def render_set_engine(bpy: Any, args: dict[str, Any]) -> None:
    _only(args, {"engine"}, "render.set_engine")
    engine = _choice(
        args.get("engine"),
        "engine",
        {"eevee": "EEVEE", "cycles": "CYCLES", "workbench": "BLENDER_WORKBENCH"},
    )
    bpy.context.scene.render.engine = _eevee_id(bpy) if engine == "EEVEE" else engine


def render_set_resolution(bpy: Any, args: dict[str, Any]) -> None:
    _only(args, {"width", "height", "percentage"}, "render.set_resolution")
    render = bpy.context.scene.render
    render.resolution_x = _int(args.get("width"), "width", 16, 16384)
    render.resolution_y = _int(args.get("height"), "height", 16, 16384)
    render.resolution_percentage = _int(args.get("percentage", 100), "percentage", 1, 100)


TOOLS: dict[str, Callable[[Any, dict[str, Any]], None]] = {
    "scene.clear": scene_clear,
    "object.add_primitive": object_add_primitive,
    "camera.add": camera_add,
    "camera.set_lens": camera_set_lens,
    "camera.set_transform": camera_set_transform,
    "light.add": light_add,
    "render.set_engine": render_set_engine,
    "render.set_resolution": render_set_resolution,
}

assert frozenset(TOOLS) == TOOL_NAMES, "TOOLS and TOOL_NAMES must list the same tools"


def run_step(bpy: Any, tool: str, args: dict[str, Any]) -> None:
    """Run one recipe step; unknown tools are refused."""
    fn = TOOLS.get(tool)
    if fn is None:
        raise ToolError(f"unknown tool {tool!r}: the bpy runtime runs only registered tools")
    if not isinstance(args, dict):
        raise ToolError(f"{tool}: args must be an object")
    fn(bpy, args)
