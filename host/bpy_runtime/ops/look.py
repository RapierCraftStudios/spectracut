# SPDX-License-Identifier: AGPL-3.0-or-later
"""``look.set_view``: switch the shading mode and auto-frame the camera on the scene.

The camera placement is pure stdlib (:func:`auto_frame`) and unit-tested: the
camera sits on the chosen angle's direction from the scene's bounds centre, far
enough that the bounding sphere fits the narrower field of view.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

from bpy_runtime.ops import (
    ENGINES,
    Report,
    ToolError,
    as_choice,
    as_num,
    check_only,
    engine_id,
)
from bpy_runtime.ops.inspect import Vec3, scene_mesh_bounds

# Direction from the subject towards the camera, per angle.
ANGLES: dict[str, Vec3] = {
    "front": (0.0, -1.0, 0.0),
    "three_quarter": (1.0, -1.0, 0.8),
    "top": (0.0, 0.0, 1.0),
}
SHADINGS = {"solid": "solid", "material": "material", "rendered": "rendered"}


def look_rotation(forward: Sequence[float]) -> Vec3:
    """XYZ Euler rotation that points a Blender camera (-Z forward, +Y up) along ``forward``."""
    fx, fy, fz = (float(v) for v in forward)
    n = math.sqrt(fx * fx + fy * fy + fz * fz)
    fx, fy, fz = fx / n, fy / n, fz / n
    pitch = math.acos(max(-1.0, min(1.0, -fz)))
    yaw = math.atan2(-fx, fy) if abs(fx) + abs(fy) > 1e-12 else 0.0
    return (pitch, 0.0, yaw)


def auto_frame(
    bounds_min: Sequence[float],
    bounds_max: Sequence[float],
    angle: str,
    lens_mm: float,
    sensor_width: float,
    aspect: float,
    margin: float = 1.1,
) -> tuple[Vec3, Vec3]:
    """Camera ``(location, rotation_euler)`` framing the bounds from ``angle``.

    ``sensor_width`` spans the longer frame edge (Blender's ``AUTO`` sensor fit).
    """
    center = [(float(bounds_min[i]) + float(bounds_max[i])) / 2 for i in range(3)]
    radius = max(
        1e-3,
        math.sqrt(sum((float(bounds_max[i]) - float(bounds_min[i])) ** 2 for i in range(3))) / 2,
    )
    half_long = math.atan(sensor_width / (2 * lens_mm))
    half_short = math.atan(math.tan(half_long) / max(aspect, 1 / aspect))
    distance = radius * margin / math.sin(min(half_long, half_short))
    dx, dy, dz = ANGLES[angle]
    n = math.sqrt(dx * dx + dy * dy + dz * dz)
    direction = (dx / n, dy / n, dz / n)
    location = (
        center[0] + direction[0] * distance,
        center[1] + direction[1] * distance,
        center[2] + direction[2] * distance,
    )
    return location, look_rotation((-direction[0], -direction[1], -direction[2]))


def _ensure_camera(bpy: Any) -> Any:
    camera = bpy.context.scene.camera
    if camera is None:
        data = bpy.data.cameras.new("LookCam")
        camera = bpy.data.objects.new("LookCam", data)
        bpy.context.scene.collection.objects.link(camera)
        bpy.context.scene.camera = camera
    return camera


def look_set_view(bpy: Any, args: dict[str, Any]) -> Report:
    check_only(args, {"shading", "angle", "engine", "margin"}, "look.set_view")
    shading = as_choice(args.get("shading"), "shading", SHADINGS)
    angle = None
    if "angle" in args:
        angle = as_choice(args["angle"], "angle", {k: k for k in ANGLES})
    margin = as_num(args.get("margin", 1.1), "margin", 1.0, 10.0)
    if "engine" in args and shading != "rendered":
        raise ToolError("look.set_view: engine applies only to rendered shading")
    engine = as_choice(args.get("engine", "eevee"), "engine", ENGINES)
    if angle is not None:
        lo, hi = scene_mesh_bounds(bpy, "look.set_view")
    scene = bpy.context.scene
    if shading == "rendered":
        scene.render.engine = engine_id(bpy, engine)
    else:
        scene.render.engine = "BLENDER_WORKBENCH"
        scene.display.shading.light = "STUDIO"
        scene.display.shading.color_type = "MATERIAL" if shading == "material" else "SINGLE"
    report: dict[str, Any] = {"shading": shading, "engine": str(scene.render.engine)}
    if angle is not None:
        camera = _ensure_camera(bpy)
        res_x, res_y = int(scene.render.resolution_x), int(scene.render.resolution_y)
        location, rotation = auto_frame(
            lo,
            hi,
            angle,
            float(camera.data.lens),
            float(camera.data.sensor_width),
            res_x / res_y if res_y > 0 else 1.0,
            margin,
        )
        camera.location = location
        camera.rotation_euler = rotation
        report.update(
            angle=angle,
            camera={
                "name": str(camera.name),
                "location": list(location),
                "rotation": list(rotation),
            },
        )
    return report
