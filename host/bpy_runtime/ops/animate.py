# SPDX-License-Identifier: AGPL-3.0-or-later
"""Object animation op: ``object.animate`` keys a preset move on a frame range.

Presets:

- ``turntable``: rotate ``degrees`` about ``axis`` between the two frames (linear);
- ``reveal``: grow from (near) zero scale, or slide in from ``offset``, to rest;
- ``shatter``: split the mesh into ``pieces`` and fly them apart;
- ``assemble``: ``shatter`` reversed (pieces fly in and meet).

The split needs no cell-fracture add-on: :func:`shatter_plan` draws seed points
with ``random.Random(seed)``, every face joins its nearest seed, and each piece
gets a fixed outward offset and spin, so a recipe always yields the same pieces.
"""

from __future__ import annotations

import math
import random
from typing import Any

from bpy_runtime.ops import (
    Report,
    ToolError,
    as_choice,
    as_int,
    as_num,
    as_vec3,
    check_only,
    find_object,
)

TOOL = "object.animate"
PRESETS = ("turntable", "reveal", "shatter", "assemble")
_AXES = {"x": "0", "y": "1", "z": "2"}
_EXTRA = {
    "turntable": {"degrees", "axis"},
    "reveal": {"from", "offset"},
    "shatter": {"pieces", "seed", "distance"},
    "assemble": {"pieces", "seed", "distance"},
}
HIDDEN_SCALE = 1e-4
Vec = tuple[float, float, float]


def _dist2(a: Vec, b: Vec) -> float:
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2


def nearest(point: Vec, seeds: list[Vec]) -> int:
    """Index of the seed nearest ``point`` (ties go to the lower index)."""
    return min(range(len(seeds)), key=lambda i: (_dist2(point, seeds[i]), i))


def shatter_plan(centers: list[Vec], pieces: int, seed: int, distance: float) -> dict[str, Any]:
    """Deterministic split: seed points, then an outward offset and spin per piece."""
    if len(centers) < 2:
        raise ToolError(f"{TOOL}: shatter needs a mesh with at least 2 faces")
    rng = random.Random(seed)
    n = min(pieces, len(centers))
    seeds = [centers[i] for i in sorted(rng.sample(range(len(centers)), n))]
    c = tuple(sum(p[k] for p in centers) / len(centers) for k in range(3))
    offsets, spins = [], []
    for s in seeds:
        d = [s[k] - c[k] for k in range(3)]
        length = math.sqrt(sum(v * v for v in d))
        if length < 1e-9:
            d = [rng.uniform(-1, 1) for _ in range(3)]
            length = math.sqrt(sum(v * v for v in d)) or 1.0
        reach = distance * (0.6 + 0.4 * rng.random())
        offsets.append([round(v / length * reach, 6) for v in d])
        spins.append([round(rng.uniform(-math.pi, math.pi), 6) for _ in range(3)])
    return {"seeds": [list(s) for s in seeds], "offsets": offsets, "spins": spins}


def _parse(args: dict[str, Any]) -> dict[str, Any]:
    preset = args.get("preset")
    if not isinstance(preset, str) or preset not in _EXTRA:
        raise ToolError(f"{TOOL}: preset must be one of {list(PRESETS)}")
    check_only(args, {"object", "preset", "frame_start", "frame_end"} | _EXTRA[preset], TOOL)
    p: dict[str, Any] = {
        "preset": preset,
        "frame_start": as_int(args.get("frame_start"), "frame_start", 0, 1_000_000),
        "frame_end": as_int(args.get("frame_end"), "frame_end", 0, 1_000_000),
    }
    if p["frame_end"] <= p["frame_start"]:
        raise ToolError(f"{TOOL}: frame_end must be after frame_start")
    if preset == "turntable":
        p["degrees"] = as_num(args.get("degrees", 360), "degrees", -3600, 3600)
        p["axis"] = int(as_choice(args.get("axis", "z"), "axis", _AXES))
    elif preset == "reveal":
        mode = args.get("from", "scale")
        if mode not in ("scale", "position"):
            raise ToolError(f"{TOOL}: from must be 'scale' or 'position'")
        if mode == "scale" and "offset" in args:
            raise ToolError(f"{TOOL}: offset applies to a position reveal only")
        p["from"] = mode
        if mode == "position":
            p["offset"] = as_vec3(args.get("offset"), "offset")
    else:
        p["pieces"] = as_int(args.get("pieces", 12), "pieces", 2, 256)
        p["seed"] = as_int(args.get("seed", 0), "seed", 0, 2**31 - 1)
        p["distance"] = as_num(args.get("distance", 1.0), "distance", 0, 100)
    return p


def _key(obj: Any, path: str, frame: int) -> None:
    obj.keyframe_insert(data_path=path, frame=frame)


def _linear(obj: Any, path: str) -> None:
    action = getattr(obj.animation_data, "action", None)
    for curve in getattr(action, "fcurves", None) or []:
        if curve.data_path == path:
            for point in curve.keyframe_points:
                point.interpolation = "LINEAR"


def _turntable(obj: Any, p: dict[str, Any]) -> None:
    start = [float(v) for v in obj.rotation_euler]
    obj.rotation_euler = tuple(start)
    _key(obj, "rotation_euler", p["frame_start"])
    end = list(start)
    end[p["axis"]] += math.radians(p["degrees"])
    obj.rotation_euler = tuple(end)
    _key(obj, "rotation_euler", p["frame_end"])
    _linear(obj, "rotation_euler")


def _reveal(obj: Any, p: dict[str, Any]) -> None:
    if p["from"] == "scale":
        rest = tuple(float(v) for v in obj.scale)
        obj.scale = (HIDDEN_SCALE,) * 3
        _key(obj, "scale", p["frame_start"])
        obj.scale = rest
        _key(obj, "scale", p["frame_end"])
        return
    rest = tuple(float(v) for v in obj.location)
    obj.location = tuple(r + o for r, o in zip(rest, p["offset"], strict=True))
    _key(obj, "location", p["frame_start"])
    obj.location = rest
    _key(obj, "location", p["frame_end"])


def _centers(obj: Any) -> list[Vec]:
    out: list[Vec] = []
    for poly in obj.data.polygons:
        c = poly.center
        out.append((float(c[0]), float(c[1]), float(c[2])))
    return out


def _split(bpy: Any, obj: Any, seeds: list[Vec]) -> list[tuple[int, Any]]:
    """Separate every seed's faces (but the first's) into its own object.

    Returns ``(seed index, piece)`` pairs; the original object keeps seed 0's faces.
    """
    pieces: list[tuple[int, Any]] = [(0, obj)]
    for k in range(1, len(seeds)):
        bpy.ops.object.mode_set(mode="OBJECT")
        bpy.ops.object.select_all(action="DESELECT")
        bpy.context.view_layer.objects.active = obj
        obj.select_set(True)
        hits = 0
        for poly in obj.data.polygons:
            c = poly.center
            poly.select = nearest((float(c[0]), float(c[1]), float(c[2])), seeds) == k
            hits += bool(poly.select)
        if not hits:
            continue
        before = {o.name for o in bpy.data.objects}
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.separate(type="SELECTED")
        bpy.ops.object.mode_set(mode="OBJECT")
        for new in [o for o in bpy.data.objects if o.name not in before]:
            new.name = f"{obj.name}.piece.{k:03d}"[:63]
            pieces.append((k, new))
    bpy.ops.object.select_all(action="DESELECT")
    for _, piece in pieces:
        piece.select_set(True)
    bpy.ops.object.origin_set(type="ORIGIN_GEOMETRY", center="BOUNDS")
    return pieces


def _scatter(pieces: list[tuple[int, Any]], plan: dict[str, Any], p: dict[str, Any]) -> None:
    whole, apart = p["frame_start"], p["frame_end"]
    if p["preset"] == "assemble":
        whole, apart = apart, whole
    for k, piece in pieces:
        offset, spin = plan["offsets"][k], plan["spins"][k]
        loc = tuple(float(v) for v in piece.location)
        rot = tuple(float(v) for v in piece.rotation_euler)
        for frame, t in ((whole, 0.0), (apart, 1.0)):
            piece.location = tuple(a + t * b for a, b in zip(loc, offset, strict=True))
            piece.rotation_euler = tuple(a + t * b for a, b in zip(rot, spin, strict=True))
            _key(piece, "location", frame)
            _key(piece, "rotation_euler", frame)
        piece.location, piece.rotation_euler = loc, rot


def object_animate(bpy: Any, args: dict[str, Any]) -> Report:
    p = _parse(args)
    obj = find_object(bpy, args.get("object"), TOOL)
    report: dict[str, Any] = {
        "object": str(obj.name),
        "preset": p["preset"],
        "frame_start": p["frame_start"],
        "frame_end": p["frame_end"],
    }
    if p["preset"] == "turntable":
        _turntable(obj, p)
    elif p["preset"] == "reveal":
        _reveal(obj, p)
    else:
        if obj.type != "MESH":
            raise ToolError(f"{TOOL}: {args['object']!r} is not a mesh object")
        plan = shatter_plan(_centers(obj), p["pieces"], p["seed"], p["distance"])
        seeds = [(s[0], s[1], s[2]) for s in plan["seeds"]]
        pieces = _split(bpy, obj, seeds)
        _scatter(pieces, plan, p)
        report["pieces"] = [str(o.name) for _, o in pieces]
        report["seed"] = p["seed"]
    return report
