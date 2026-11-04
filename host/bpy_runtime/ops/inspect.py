# SPDX-License-Identifier: AGPL-3.0-or-later
"""Inspection ops: world bounds and framing coverage.

The projection maths is pure stdlib (no ``bpy``) so it is unit-tested with
known numbers. It follows Blender's camera model: the camera looks down its
local -Z with +Y up, and ``sensor_fit`` decides which frame edge the sensor
size spans. Normalised frame coordinates run 0..1 from the bottom-left corner,
like ``bpy_extras.object_utils.world_to_camera_view``.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
from typing import Any

from bpy_runtime.ops import (
    Report,
    ToolError,
    active_camera,
    as_name,
    as_num,
    check_only,
    find_object,
    mesh_objects,
)

Vec3 = tuple[float, float, float]
Matrix = Sequence[Sequence[float]]

NEAR = 1e-3


def euler_matrix(location: Sequence[float], rotation: Sequence[float]) -> list[list[float]]:
    """4x4 world matrix (rows) for an XYZ Euler rotation then a translation."""
    rx, ry, rz = (float(a) for a in rotation)
    cx, sx, cy, sy, cz, sz = (
        math.cos(rx),
        math.sin(rx),
        math.cos(ry),
        math.sin(ry),
        math.cos(rz),
        math.sin(rz),
    )
    # R = Rz @ Ry @ Rx (Blender's XYZ Euler order).
    rot = [
        [cz * cy, cz * sy * sx - sz * cx, cz * sy * cx + sz * sx],
        [sz * cy, sz * sy * sx + cz * cx, sz * sy * cx - cz * sx],
        [-sy, cy * sx, cy * cx],
    ]
    x, y, z = (float(v) for v in location)
    return [rot[0] + [x], rot[1] + [y], rot[2] + [z], [0.0, 0.0, 0.0, 1.0]]


def transform(matrix: Matrix, point: Sequence[float]) -> Vec3:
    x, y, z = (float(v) for v in point)
    m = matrix
    return (
        m[0][0] * x + m[0][1] * y + m[0][2] * z + m[0][3],
        m[1][0] * x + m[1][1] * y + m[1][2] * z + m[1][3],
        m[2][0] * x + m[2][1] * y + m[2][2] * z + m[2][3],
    )


def _to_camera(matrix: Matrix, point: Sequence[float]) -> Vec3:
    """World point to camera space via the inverse of the camera's 3x3 and translation."""
    a = [[float(matrix[r][c]) for c in range(3)] for r in range(3)]
    det = (
        a[0][0] * (a[1][1] * a[2][2] - a[1][2] * a[2][1])
        - a[0][1] * (a[1][0] * a[2][2] - a[1][2] * a[2][0])
        + a[0][2] * (a[1][0] * a[2][1] - a[1][1] * a[2][0])
    )
    if abs(det) < 1e-12:
        raise ToolError("camera matrix is singular")
    inv = [
        [
            (
                a[(c + 1) % 3][(r + 1) % 3] * a[(c + 2) % 3][(r + 2) % 3]
                - a[(c + 1) % 3][(r + 2) % 3] * a[(c + 2) % 3][(r + 1) % 3]
            )
            / det
            for c in range(3)
        ]
        for r in range(3)
    ]
    d = [float(point[i]) - float(matrix[i][3]) for i in range(3)]
    return (
        inv[0][0] * d[0] + inv[0][1] * d[1] + inv[0][2] * d[2],
        inv[1][0] * d[0] + inv[1][1] * d[1] + inv[1][2] * d[2],
        inv[2][0] * d[0] + inv[2][1] * d[1] + inv[2][2] * d[2],
    )


def frame_half_extent(
    lens_mm: float,
    sensor_width: float,
    sensor_height: float,
    sensor_fit: str,
    res_x: int,
    res_y: int,
) -> tuple[float, float]:
    """Half width and half height of the frame on the plane one unit in front of the camera."""
    if lens_mm <= 0 or res_x <= 0 or res_y <= 0:
        raise ToolError("lens and resolution must be positive")
    aspect = res_x / res_y
    fit = sensor_fit
    if fit == "AUTO":
        fit = "HORIZONTAL" if res_x >= res_y else "VERTICAL"
        sensor = sensor_width
    else:
        sensor = sensor_width if fit == "HORIZONTAL" else sensor_height
    if fit == "HORIZONTAL":
        half_w = sensor / (2.0 * lens_mm)
        return half_w, half_w / aspect
    half_h = sensor / (2.0 * lens_mm)
    return half_h * aspect, half_h


def project_point(
    cam_matrix: Matrix,
    lens_mm: float,
    sensor_width: float,
    sensor_height: float,
    sensor_fit: str,
    res_x: int,
    res_y: int,
    point: Sequence[float],
) -> Vec3:
    """Return ``(x, y, depth)``: normalised frame coordinates and distance in front.

    ``depth <= 0`` means the point is behind the camera and ``x``/``y`` are meaningless.
    """
    half_w, half_h = frame_half_extent(
        lens_mm, sensor_width, sensor_height, sensor_fit, res_x, res_y
    )
    cx, cy, cz = _to_camera(cam_matrix, point)
    depth = -cz
    if depth <= 0:
        return (0.0, 0.0, depth)
    return (0.5 + (cx / depth) / (2 * half_w), 0.5 + (cy / depth) / (2 * half_h), depth)


def framing_coverage(
    cam_matrix: Matrix,
    lens_mm: float,
    sensor_width: float,
    sensor_height: float,
    sensor_fit: str,
    res_x: int,
    res_y: int,
    corners: Sequence[Sequence[float]],
) -> dict[str, Any]:
    """Share of the frame covered by the subject's projected bounding box.

    ``corners`` are the subject's 8 world-space bounding-box corners. Segments
    between corners are clipped against the camera's near plane, the projected
    points' 2D bounding box is clipped to the frame, and ``coverage_pct`` is its
    area as a percentage of the frame. ``in_frame_pct`` is how much of the
    unclipped projected box lies inside the frame.
    """
    half_w, half_h = frame_half_extent(
        lens_mm, sensor_width, sensor_height, sensor_fit, res_x, res_y
    )
    cam = [_to_camera(cam_matrix, c) for c in corners]
    # Points in front of the near plane, plus where any segment crosses it. The
    # hull of these is the subject box clipped to the half-space in front.
    front: list[Vec3] = [p for p in cam if -p[2] >= NEAR]
    for p, q in itertools.combinations(cam, 2):
        dp, dq = -p[2] - NEAR, -q[2] - NEAR
        if (dp < 0) != (dq < 0):
            t = dp / (dp - dq)
            front.append(tuple(p[i] + t * (q[i] - p[i]) for i in range(3)))  # type: ignore[arg-type]
    empty = {"coverage_pct": 0.0, "in_frame_pct": 0.0, "bbox_ndc": None}
    if not front:
        return empty
    xs = [0.5 + (p[0] / -p[2]) / (2 * half_w) for p in front]
    ys = [0.5 + (p[1] / -p[2]) / (2 * half_h) for p in front]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    full = (x1 - x0) * (y1 - y0)
    cx0, cx1 = max(x0, 0.0), min(x1, 1.0)
    cy0, cy1 = max(y0, 0.0), min(y1, 1.0)
    if cx1 <= cx0 or cy1 <= cy0:
        return empty
    area = (cx1 - cx0) * (cy1 - cy0)
    return {
        "coverage_pct": area * 100.0,
        "in_frame_pct": (area / full * 100.0) if full > 0 else 0.0,
        "bbox_ndc": [cx0, cy0, cx1, cy1],
    }


def world_corners(obj: Any) -> list[Vec3]:
    matrix = [list(row) for row in obj.matrix_world]
    return [transform(matrix, tuple(c)) for c in obj.bound_box]


def world_bounds(objects: Sequence[Any]) -> tuple[Vec3, Vec3]:
    points = [p for o in objects for p in world_corners(o)]
    lo = (min(p[0] for p in points), min(p[1] for p in points), min(p[2] for p in points))
    hi = (max(p[0] for p in points), max(p[1] for p in points), max(p[2] for p in points))
    return lo, hi


def scene_mesh_bounds(bpy: Any, tool: str) -> tuple[Vec3, Vec3]:
    meshes = mesh_objects(bpy)
    if not meshes:
        raise ToolError(f"{tool}: the scene has no mesh objects", "scene.object_not_found")
    bpy.context.view_layer.update()
    return world_bounds(meshes)


def _bounds_report(lo: Vec3, hi: Vec3) -> dict[str, Any]:
    return {
        "min": list(lo),
        "max": list(hi),
        "center": [(lo[i] + hi[i]) / 2 for i in range(3)],
        "size": [hi[i] - lo[i] for i in range(3)],
    }


def inspect_bounds(bpy: Any, args: dict[str, Any]) -> Report:
    check_only(args, {"objects"}, "inspect.bounds")
    if "objects" not in args:
        return _bounds_report(*scene_mesh_bounds(bpy, "inspect.bounds"))
    names = args["objects"]
    if not isinstance(names, list) or not 0 < len(names) <= 256:
        raise ToolError("objects must be a non-empty list of object names")
    objects = [find_object(bpy, n, "inspect.bounds", f"objects[{i}]") for i, n in enumerate(names)]
    bpy.context.view_layer.update()
    report = _bounds_report(*world_bounds(objects))
    report["objects"] = [as_name(n, "object") for n in names]
    return report


def inspect_framing(bpy: Any, args: dict[str, Any]) -> Report:
    check_only(args, {"object", "min_coverage_pct"}, "inspect.framing")
    camera = active_camera(bpy, "inspect.framing")
    obj = find_object(bpy, args.get("object"), "inspect.framing")
    min_pct = as_num(args.get("min_coverage_pct", 0), "min_coverage_pct", 0, 100)
    bpy.context.view_layer.update()
    render = bpy.context.scene.render
    data = camera.data
    result = framing_coverage(
        [list(row) for row in camera.matrix_world],
        float(data.lens),
        float(data.sensor_width),
        float(data.sensor_height),
        str(data.sensor_fit),
        int(render.resolution_x),
        int(render.resolution_y),
        world_corners(obj),
    )
    if result["coverage_pct"] <= 0:
        raise ToolError(
            f"inspect.framing: {args['object']!r} is outside the camera's frame",
            "framing.subject_out_of_frame",
        )
    return {
        "object": args["object"],
        "camera": str(camera.name),
        **result,
        "meets_min": result["coverage_pct"] >= min_pct,
    }
