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

from bpy_runtime.ops import ENGINES, Report, ToolError, as_bool, engine_id
from bpy_runtime.ops import active_camera as _active_camera
from bpy_runtime.ops import as_choice as _choice
from bpy_runtime.ops import as_int as _int
from bpy_runtime.ops import as_name as _name
from bpy_runtime.ops import as_num as _num
from bpy_runtime.ops import as_vec3 as _vec3
from bpy_runtime.ops import check_only as _only
from bpy_runtime.ops.animate import object_animate
from bpy_runtime.ops.camera import (
    camera_add_aspect,
    camera_move,
    camera_set_dof,
    camera_use,
)
from bpy_runtime.ops.compositor import comp_apply_graph
from bpy_runtime.ops.device import device_add
from bpy_runtime.ops.finish import finish_apply_look
from bpy_runtime.ops.geometry import (
    curve_add,
    object_add_slab,
    object_array,
    object_boolean,
    object_parent,
)
from bpy_runtime.ops.inspect import inspect_bounds, inspect_framing
from bpy_runtime.ops.keyframe import object_keyframe, render_set_frame_range
from bpy_runtime.ops.look import look_set_view
from bpy_runtime.ops.material import material_apply_preset, material_apply_screen
from bpy_runtime.ops.particles import particles_add
from bpy_runtime.ops.props import render_set_output, render_set_passes, render_set_property
from bpy_runtime.ops.scene import object_import_gltf
from bpy_runtime.ops.text import text_add
from bpy_runtime.ops.world import world_set_gradient, world_set_haze, world_set_hdri
from bpy_runtime.type3d import type_add_2d, type_add_3d

__all__ = ["TOOL_NAMES", "TOOLS", "run_step"]

TOOL_NAMES = frozenset(
    {
        "scene.clear",
        "object.add_primitive",
        "object.import_gltf",
        "material.apply_preset",
        "material.apply_screen",
        "camera.add",
        "camera.set_lens",
        "camera.set_transform",
        "camera.set_dof",
        "camera.move",
        "camera.add_aspect",
        "camera.use",
        "light.add",
        "render.set_engine",
        "render.set_resolution",
        "render.set_property",
        "inspect.bounds",
        "inspect.framing",
        "look.set_view",
        "render.set_output",
        "finish.apply_look",
        "render.set_passes",
        "comp.apply_graph",
        "type.add_2d",
        "type.add_3d",
        "device.add",
        "object.animate",
        "text.add",
        "world.set_hdri",
        "world.set_gradient",
        "world.set_haze",
        "curve.add",
        "object.add_slab",
        "object.array",
        "object.boolean",
        "object.parent",
        "object.keyframe",
        "render.set_frame_range",
        "particles.add",
    }
)


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
    return _active_camera(bpy, tool)


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
    allowed = {"kind", "name", "energy", "location", "rotation", "size", "color", "spot_angle"}
    allowed |= {"size_y", "camera_visible"}
    _only(args, allowed, "light.add")
    kind = _choice(args.get("kind", "point"), "kind", _LIGHTS)
    name = _name(args.get("name", "Light"), "name")
    energy = _num(args.get("energy", 1000), "energy", 0, 1e7)
    location = _vec3(args.get("location", [0, 0, 0]), "location")
    rotation = _vec3(args.get("rotation", [0, 0, 0]), "rotation")
    size = None
    if "size" in args:
        if kind != "AREA":
            raise ToolError("light.add: size applies only to area lights")
        size = _num(args["size"], "size", 0, 100)
        if size <= 0:
            raise ToolError("size must be within (0, 100]")
    size_y = None
    if "size_y" in args:
        # A rectangular area light: size (x) by size_y.
        if kind != "AREA":
            raise ToolError("light.add: size_y applies only to area lights")
        size_y = _num(args["size_y"], "size_y", 0, 100)
        if size_y <= 0:
            raise ToolError("size_y must be within (0, 100]")
    camera_visible = None
    if "camera_visible" in args:
        camera_visible = as_bool(args["camera_visible"], "camera_visible")
    color = None
    if "color" in args:
        color = _vec3(args["color"], "color")
        for i, c in enumerate(color):
            _num(c, f"color[{i}]", 0, 1)
    spot = None
    if "spot_angle" in args:
        if kind != "SPOT":
            raise ToolError("light.add: spot_angle applies only to spot lights")
        spot = math.radians(_num(args["spot_angle"], "spot_angle", 1, 180))
    data = bpy.data.lights.new(name, type=kind)
    data.energy = energy
    if size is not None:
        data.size = size
    if size_y is not None:
        data.shape = "RECTANGLE"
        data.size_y = size_y
    if color is not None:
        data.color = color
    if spot is not None:
        data.spot_size = spot
    obj = bpy.data.objects.new(name, data)
    obj.location = location
    obj.rotation_euler = rotation
    if camera_visible is not None:
        obj.visible_camera = camera_visible
    _link(bpy, obj)


def render_set_engine(bpy: Any, args: dict[str, Any]) -> None:
    _only(args, {"engine"}, "render.set_engine")
    engine = _choice(args.get("engine"), "engine", ENGINES)
    bpy.context.scene.render.engine = engine_id(bpy, engine)


def render_set_resolution(bpy: Any, args: dict[str, Any]) -> None:
    _only(args, {"width", "height", "percentage"}, "render.set_resolution")
    render = bpy.context.scene.render
    render.resolution_x = _int(args.get("width"), "width", 16, 16384)
    render.resolution_y = _int(args.get("height"), "height", 16, 16384)
    render.resolution_percentage = _int(args.get("percentage", 100), "percentage", 1, 100)


TOOLS: dict[str, Callable[[Any, dict[str, Any]], Report]] = {
    "scene.clear": scene_clear,
    "object.add_primitive": object_add_primitive,
    "camera.add": camera_add,
    "camera.set_lens": camera_set_lens,
    "camera.set_transform": camera_set_transform,
    "light.add": light_add,
    "render.set_engine": render_set_engine,
    "render.set_resolution": render_set_resolution,
    "object.import_gltf": object_import_gltf,
    "material.apply_preset": material_apply_preset,
    "material.apply_screen": material_apply_screen,
    "camera.set_dof": camera_set_dof,
    "camera.move": camera_move,
    "camera.add_aspect": camera_add_aspect,
    "camera.use": camera_use,
    "render.set_property": render_set_property,
    "inspect.bounds": inspect_bounds,
    "inspect.framing": inspect_framing,
    "look.set_view": look_set_view,
    "render.set_output": render_set_output,
    "finish.apply_look": finish_apply_look,
    "render.set_passes": render_set_passes,
    "comp.apply_graph": comp_apply_graph,
    "type.add_2d": type_add_2d,
    "type.add_3d": type_add_3d,
    "device.add": device_add,
    "object.animate": object_animate,
    "text.add": text_add,
    "world.set_hdri": world_set_hdri,
    "world.set_gradient": world_set_gradient,
    "world.set_haze": world_set_haze,
    "curve.add": curve_add,
    "object.add_slab": object_add_slab,
    "object.array": object_array,
    "object.boolean": object_boolean,
    "object.parent": object_parent,
    "object.keyframe": object_keyframe,
    "render.set_frame_range": render_set_frame_range,
    "particles.add": particles_add,
}

assert frozenset(TOOLS) == TOOL_NAMES, "TOOLS and TOOL_NAMES must list the same tools"


def run_step(bpy: Any, tool: str, args: dict[str, Any]) -> Report:
    """Run one recipe step and return its JSON report (or ``None``); unknown tools are refused."""
    fn = TOOLS.get(tool)
    if fn is None:
        raise ToolError(
            f"unknown tool {tool!r}: the bpy runtime runs only registered tools", "tool.unknown"
        )
    if not isinstance(args, dict):
        raise ToolError(f"{tool}: args must be an object")
    return fn(bpy, args)
