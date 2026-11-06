# SPDX-License-Identifier: AGPL-3.0-or-later
"""Geometry ops: bevelled curves, bevelled slabs, arrays, booleans and parenting.

- ``curve.add`` adds a bezier or poly curve through control points. A
  ``bevel_depth`` above 0 turns it into a round tube (beams, cables).
- ``object.add_slab`` adds a box mesh built at its true dimensions (scale 1), so
  the Bevel modifier's width is in metres and even on every edge.
- ``object.array`` repeats an object ``count`` times by a constant ``offset``,
  either as an Array modifier or as linked duplicates sharing the mesh data.
- ``object.boolean`` adds a Boolean modifier against a named cutter mesh and
  hides the cutter from renders (it stays in the scene so the modifier evaluates).
- ``object.parent`` parents one object to another and keeps its world transform.

None of these ops is random, so none takes a seed. Every argument is checked
before ``bpy`` is touched. This module is stdlib-only and never imports
``spectracut``.
"""

from __future__ import annotations

from typing import Any

from bpy_runtime.ops import (
    Report,
    ToolError,
    as_bool,
    as_choice,
    as_int,
    as_name,
    as_num,
    as_vec3,
    check_only,
    find_object,
)

MAX_POINTS = 256
MAX_ARRAY = 1000
MAX_DIMENSION = 1000.0
CURVE_KINDS = {"bezier": "BEZIER", "poly": "POLY"}
ARRAY_MODES = {"modifier": "modifier", "linked": "linked"}
BOOLEAN_OPS = {"difference": "DIFFERENCE", "union": "UNION", "intersect": "INTERSECT"}

# A unit box: 8 corners, 6 quads with outward normals; scaled by the slab's half extents.
_BOX_CORNERS = [
    (-1, -1, -1),
    (1, -1, -1),
    (1, 1, -1),
    (-1, 1, -1),
    (-1, -1, 1),
    (1, -1, 1),
    (1, 1, 1),
    (-1, 1, 1),
]
_BOX_FACES = [
    (0, 3, 2, 1),
    (4, 5, 6, 7),
    (0, 1, 5, 4),
    (1, 2, 6, 5),
    (2, 3, 7, 6),
    (3, 0, 4, 7),
]


def _link(bpy: Any, obj: Any) -> None:
    bpy.context.scene.collection.objects.link(obj)


def _new_name(bpy: Any, value: Any, default: str, tool: str) -> str:
    name = as_name(value if value is not None else default, "name")
    if bpy.data.objects.get(name) is not None:
        raise ToolError(f"{tool}: an object named {name!r} already exists")
    return name


def _mesh(obj: Any, what: str, tool: str) -> Any:
    if obj.type != "MESH":
        raise ToolError(f"{tool}: {what} {obj.name!r} is not a mesh object")
    return obj


# --- curve.add ------------------------------------------------------------------------------


def _points(value: Any, tool: str) -> list[tuple[float, float, float]]:
    if not isinstance(value, list) or not 2 <= len(value) <= MAX_POINTS:
        raise ToolError(f"{tool}: points must be a list of 2 to {MAX_POINTS} [x, y, z] points")
    return [as_vec3(p, f"points[{i}]") for i, p in enumerate(value)]


def curve_add(bpy: Any, args: dict[str, Any]) -> Report:
    tool = "curve.add"
    check_only(
        args,
        {
            "name",
            "points",
            "kind",
            "bevel_depth",
            "bevel_resolution",
            "resolution",
            "cyclic",
            "location",
        },
        tool,
    )
    points = _points(args.get("points"), tool)
    kind = as_choice(args.get("kind", "bezier"), "kind", CURVE_KINDS)
    bevel_depth = as_num(args.get("bevel_depth", 0.0), "bevel_depth", 0, 10)
    bevel_resolution = as_int(args.get("bevel_resolution", 4), "bevel_resolution", 0, 32)
    resolution = as_int(args.get("resolution", 12), "resolution", 1, 64)
    cyclic = as_bool(args.get("cyclic", False), "cyclic")
    location = as_vec3(args.get("location", [0, 0, 0]), "location")
    name = _new_name(bpy, args.get("name"), "Curve", tool)

    curve = bpy.data.curves.new(name, type="CURVE")
    curve.dimensions = "3D"
    curve.resolution_u = resolution
    curve.bevel_depth = bevel_depth
    curve.bevel_resolution = bevel_resolution
    curve.use_fill_caps = True
    spline = curve.splines.new(kind)
    spline.use_cyclic_u = cyclic
    if kind == "BEZIER":
        spline.bezier_points.add(len(points) - 1)
        for bp, co in zip(spline.bezier_points, points, strict=True):
            bp.co = co
            bp.handle_left_type = "AUTO"
            bp.handle_right_type = "AUTO"
    else:
        spline.points.add(len(points) - 1)
        for pt, co in zip(spline.points, points, strict=True):
            pt.co = (co[0], co[1], co[2], 1.0)
    obj = bpy.data.objects.new(name, curve)
    obj.location = location
    _link(bpy, obj)
    return {"object": str(obj.name), "kind": kind.lower(), "points": len(points)}


# --- object.add_slab ------------------------------------------------------------------------


def object_add_slab(bpy: Any, args: dict[str, Any]) -> Report:
    tool = "object.add_slab"
    check_only(
        args, {"name", "dimensions", "bevel_width", "segments", "location", "rotation"}, tool
    )
    dims = as_vec3(args.get("dimensions"), "dimensions")
    for i, d in enumerate(dims):
        if not 0 < d <= MAX_DIMENSION:
            raise ToolError(f"dimensions[{i}] must be within (0, {MAX_DIMENSION}]")
    bevel_width = as_num(args.get("bevel_width", 0.0), "bevel_width", 0, min(dims) / 2)
    segments = as_int(args.get("segments", 3), "segments", 1, 16)
    location = as_vec3(args.get("location", [0, 0, 0]), "location")
    rotation = as_vec3(args.get("rotation", [0, 0, 0]), "rotation")
    name = _new_name(bpy, args.get("name"), "Slab", tool)

    half = [d / 2 for d in dims]
    verts = [(x * half[0], y * half[1], z * half[2]) for x, y, z in _BOX_CORNERS]
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts, [], _BOX_FACES)
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    obj.location = location
    obj.rotation_euler = rotation
    _link(bpy, obj)
    report: dict[str, Any] = {"object": str(obj.name), "dimensions": list(dims)}
    if bevel_width > 0:
        mod = obj.modifiers.new(name="bevel", type="BEVEL")
        mod.width = bevel_width
        mod.segments = segments
        mod.limit_method = "ANGLE"
        report["modifier"] = "bevel"
    return report


# --- object.array ---------------------------------------------------------------------------


def object_array(bpy: Any, args: dict[str, Any]) -> Report:
    tool = "object.array"
    check_only(args, {"object", "count", "offset", "mode"}, tool)
    as_name(args.get("object"), "object")
    count = as_int(args.get("count"), "count", 2, MAX_ARRAY)
    offset = as_vec3(args.get("offset"), "offset")
    mode = as_choice(args.get("mode", "modifier"), "mode", ARRAY_MODES)
    obj = find_object(bpy, args["object"], tool)

    if mode == "modifier":
        mod_name = f"array.{obj.name}"[:63]
        mod = obj.modifiers.new(name=mod_name, type="ARRAY")
        mod.count = count
        mod.use_relative_offset = False
        mod.use_constant_offset = True
        mod.constant_offset_displace = offset
        return {"object": str(obj.name), "mode": mode, "count": count, "modifier": mod_name}

    base = str(obj.name)[:59]
    names = [f"{base}.{i:03d}" for i in range(1, count)]
    taken = [n for n in names if bpy.data.objects.get(n) is not None]
    if taken:
        raise ToolError(f"{tool}: object(s) {taken[:3]} already exist")
    start = tuple(float(v) for v in obj.location)
    made = []
    for i, dup_name in enumerate(names, start=1):
        dup = obj.copy()  # Object.copy() shares the mesh data: a linked duplicate.
        dup.name = dup_name
        dup.location = tuple(start[k] + i * offset[k] for k in range(3))
        _link(bpy, dup)
        made.append(str(dup.name))
    return {"object": str(obj.name), "mode": mode, "count": count, "duplicates": made}


# --- object.boolean -------------------------------------------------------------------------


def object_boolean(bpy: Any, args: dict[str, Any]) -> Report:
    tool = "object.boolean"
    check_only(args, {"object", "cutter", "operation"}, tool)
    target_name = as_name(args.get("object"), "object")
    cutter_name = as_name(args.get("cutter"), "cutter")
    operation = as_choice(args.get("operation", "difference"), "operation", BOOLEAN_OPS)
    if target_name == cutter_name:
        raise ToolError(f"{tool}: object and cutter must be different objects")
    target = _mesh(find_object(bpy, target_name, tool), "object", tool)
    cutter = _mesh(find_object(bpy, cutter_name, tool, "cutter"), "cutter", tool)

    mod_name = f"boolean.{cutter_name}"[:63]
    mod = target.modifiers.new(name=mod_name, type="BOOLEAN")
    mod.operation = operation
    mod.object = cutter
    cutter.hide_render = True
    cutter.display_type = "WIRE"
    return {
        "object": target_name,
        "cutter": cutter_name,
        "operation": operation.lower(),
        "modifier": mod_name,
    }


# --- object.parent --------------------------------------------------------------------------


def object_parent(bpy: Any, args: dict[str, Any]) -> Report:
    tool = "object.parent"
    check_only(args, {"child", "parent"}, tool)
    child_name = as_name(args.get("child"), "child")
    parent_name = as_name(args.get("parent"), "parent")
    if child_name == parent_name:
        raise ToolError(f"{tool}: an object cannot be its own parent")
    child = find_object(bpy, child_name, tool, "child")
    parent = find_object(bpy, parent_name, tool, "parent")
    node = parent
    while node is not None:
        if node is child:
            raise ToolError(f"{tool}: {parent_name!r} is a descendant of {child_name!r}")
        node = node.parent

    # Earlier steps set locations without a depsgraph update: refresh matrix_world first.
    bpy.context.view_layer.update()
    world = child.matrix_world.copy()
    child.parent = parent
    child.matrix_parent_inverse = parent.matrix_world.inverted()
    # world = parent.matrix_world @ parent_inverse @ basis, so basis = world keeps it.
    child.matrix_basis = world
    return {"child": child_name, "parent": parent_name}
