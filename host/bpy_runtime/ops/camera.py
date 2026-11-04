# SPDX-License-Identifier: AGPL-3.0-or-later
"""Camera ops: depth of field, keyframed moves with easing, per-aspect cameras."""

from __future__ import annotations

from typing import Any

from bpy_runtime.ops import (
    Report,
    ToolError,
    active_camera,
    as_bool,
    as_choice,
    as_int,
    as_name,
    as_num,
    as_vec3,
    check_only,
    find_object,
)

ASPECTS: dict[str, tuple[int, int]] = {
    "16:9": (16, 9),
    "9:16": (9, 16),
    "1:1": (1, 1),
    "4:5": (4, 5),
}
ASPECT_PROP = "spectracut_aspect"
# Easing name -> (keyframe interpolation, Bezier easing or None).
EASINGS: dict[str, tuple[str, str | None]] = {
    "linear": ("LINEAR", None),
    "ease_in": ("BEZIER", "EASE_IN"),
    "ease_out": ("BEZIER", "EASE_OUT"),
    "ease_in_out": ("BEZIER", "EASE_IN_OUT"),
}
MAX_FRAME = 1_048_574


def camera_set_dof(bpy: Any, args: dict[str, Any]) -> Report:
    check_only(args, {"enabled", "focus_object", "focus_distance", "fstop"}, "camera.set_dof")
    if "focus_object" in args and "focus_distance" in args:
        raise ToolError("camera.set_dof: pass focus_object or focus_distance, not both")
    camera = active_camera(bpy, "camera.set_dof")
    enabled = as_bool(args.get("enabled", True), "enabled")
    focus = (
        find_object(bpy, args["focus_object"], "camera.set_dof", "focus_object")
        if "focus_object" in args
        else None
    )
    distance = (
        as_num(args["focus_distance"], "focus_distance", 0, 1e6)
        if "focus_distance" in args
        else None
    )
    fstop = as_num(args["fstop"], "fstop", 0.1, 128) if "fstop" in args else None
    dof = camera.data.dof
    dof.use_dof = enabled
    report: dict[str, Any] = {"camera": str(camera.name), "enabled": enabled}
    if focus is not None:
        dof.focus_object = focus
        report["focus_object"] = args["focus_object"]
    if distance is not None:
        dof.focus_object = None
        dof.focus_distance = distance
        report["focus_distance"] = distance
    if fstop is not None:
        dof.aperture_fstop = fstop
        report["fstop"] = fstop
    return report


def _keys(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not 2 <= len(value) <= 1000:
        raise ToolError("keys must be a list of 2 to 1000 keyframes")
    keys: list[dict[str, Any]] = []
    last = None
    for i, key in enumerate(value):
        if not isinstance(key, dict):
            raise ToolError(f"keys[{i}] must be an object")
        check_only(key, {"frame", "location", "rotation", "lens"}, f"camera.move keys[{i}]")
        frame = as_int(key.get("frame"), f"keys[{i}].frame", 0, MAX_FRAME)
        if last is not None and frame <= last:
            raise ToolError("keys must have strictly increasing frames")
        last = frame
        parsed: dict[str, Any] = {"frame": frame}
        if "location" in key:
            parsed["location"] = as_vec3(key["location"], f"keys[{i}].location")
        if "rotation" in key:
            parsed["rotation"] = as_vec3(key["rotation"], f"keys[{i}].rotation")
        if "lens" in key:
            parsed["lens"] = as_num(key["lens"], f"keys[{i}].lens", 1, 5000)
        if len(parsed) == 1:
            raise ToolError(f"keys[{i}] needs location, rotation or lens")
        keys.append(parsed)
    return keys


def _fcurves(id_block: Any) -> list[Any]:
    """F-curves of an ID's action: legacy ``action.fcurves`` or layered channelbags (5.0+)."""
    anim = id_block.animation_data
    action = anim.action if anim is not None else None
    if action is None:
        return []
    if hasattr(action, "fcurves"):
        return list(action.fcurves)
    return [
        fc
        for layer in action.layers
        for strip in layer.strips
        for bag in strip.channelbags
        for fc in bag.fcurves
    ]


def camera_move(bpy: Any, args: dict[str, Any]) -> Report:
    check_only(args, {"keys", "easing"}, "camera.move")
    camera = active_camera(bpy, "camera.move")
    keys = _keys(args.get("keys"))
    easing = as_choice(args.get("easing", "ease_in_out"), "easing", {k: k for k in EASINGS})
    for key in keys:
        frame = key["frame"]
        if "location" in key:
            camera.location = key["location"]
            camera.keyframe_insert(data_path="location", frame=frame)
        if "rotation" in key:
            camera.rotation_euler = key["rotation"]
            camera.keyframe_insert(data_path="rotation_euler", frame=frame)
        if "lens" in key:
            camera.data.lens = key["lens"]
            camera.data.keyframe_insert(data_path="lens", frame=frame)
    interpolation, ease = EASINGS[easing]
    for fc in _fcurves(camera) + _fcurves(camera.data):
        for point in fc.keyframe_points:
            point.interpolation = interpolation
            if ease is not None:
                point.easing = ease
    return {
        "camera": str(camera.name),
        "easing": easing,
        "frames": [k["frame"] for k in keys],
    }


def aspect_resolution(aspect: str, long_edge: int) -> tuple[int, int]:
    """Even-sized resolution for ``aspect`` with ``long_edge`` pixels on the longer side."""
    w, h = ASPECTS[aspect]
    if w >= h:
        return long_edge, 2 * round(long_edge * h / w / 2)
    return 2 * round(long_edge * w / h / 2), long_edge


def camera_add_aspect(bpy: Any, args: dict[str, Any]) -> Report:
    check_only(args, {"aspect", "name"}, "camera.add_aspect")
    source = active_camera(bpy, "camera.add_aspect")
    aspect = as_choice(args.get("aspect"), "aspect", {k: k for k in ASPECTS})
    w, h = ASPECTS[aspect]
    name = as_name(args.get("name", f"{source.name}.{w}x{h}"), "name")
    obj = bpy.data.objects.new(name, source.data.copy())
    obj.location = tuple(source.location)
    obj.rotation_euler = tuple(source.rotation_euler)
    obj[ASPECT_PROP] = aspect
    bpy.context.scene.collection.objects.link(obj)
    return {"camera": str(obj.name), "aspect": aspect, "source": str(source.name)}


def camera_use(bpy: Any, args: dict[str, Any]) -> Report:
    check_only(args, {"name", "long_edge"}, "camera.use")
    obj = find_object(bpy, args.get("name"), "camera.use", "name")
    if obj.type != "CAMERA":
        raise ToolError(f"camera.use: {args['name']!r} is not a camera", "scene.object_not_found")
    long_edge = as_int(args.get("long_edge", 1920), "long_edge", 16, 16384)
    scene = bpy.context.scene
    scene.camera = obj
    aspect = obj.get(ASPECT_PROP)
    report: dict[str, Any] = {"camera": str(obj.name)}
    if isinstance(aspect, str) and aspect in ASPECTS:
        width, height = aspect_resolution(aspect, long_edge)
        scene.render.resolution_x = width
        scene.render.resolution_y = height
        report["aspect"] = aspect
    report["resolution"] = [int(scene.render.resolution_x), int(scene.render.resolution_y)]
    return report
