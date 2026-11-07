# SPDX-License-Identifier: AGPL-3.0-or-later
"""Keyframe ops: ``object.keyframe`` and ``render.set_frame_range``.

``object.keyframe`` keys an object's ``location``, ``rotation`` and ``scale``, a
camera's ``lens`` and an allowlist of Principled BSDF inputs (:data:`INPUTS`) on
the object's material, then eases every key with the ``camera.move`` vocabulary
(:data:`bpy_runtime.ops.camera.EASINGS`). Every argument is checked before the
scene is changed. Nothing here is random, so no op takes a seed.

``render.set_frame_range`` sets the scene's frame range. A render job's frames
still win at render time: the runner applies the job manifest's range after
the recipe steps.
"""

from __future__ import annotations

from typing import Any

from bpy_runtime.ops import (
    Report,
    ToolError,
    as_choice,
    as_int,
    as_name,
    as_num,
    as_vec3,
    check_only,
    find_object,
)
from bpy_runtime.ops.camera import EASINGS, MAX_FRAME, _fcurves

TOOL = "object.keyframe"
RANGE_TOOL = "render.set_frame_range"
BSDF = "Principled BSDF"
# Keyable material input -> (Principled BSDF socket, low, high).
INPUTS: dict[str, tuple[str, float, float]] = {
    "emission_strength": ("Emission Strength", 0.0, 1000.0),
    "alpha": ("Alpha", 0.0, 1.0),
    "roughness": ("Roughness", 0.0, 1.0),
    "transmission": ("Transmission Weight", 0.0, 1.0),
}
_TRANSFORMS = {"location": "location", "rotation": "rotation_euler", "scale": "scale"}
_KEY_FIELDS = {"frame", "location", "rotation", "scale", "lens", "inputs"}


def _inputs(value: Any, what: str) -> dict[str, float]:
    if not isinstance(value, dict) or not value:
        raise ToolError(f"{what} must be a non-empty object")
    check_only(value, set(INPUTS), what)
    out: dict[str, float] = {}
    for name, raw in value.items():
        _, lo, hi = INPUTS[name]
        out[name] = as_num(raw, f"{what}.{name}", lo, hi)
    return out


def parse_keys(value: Any) -> list[dict[str, Any]]:
    """Check ``keys``: 2 to 1000 keys, strictly increasing frames, one channel at least."""
    if not isinstance(value, list) or not 2 <= len(value) <= 1000:
        raise ToolError(f"{TOOL}: keys must be a list of 2 to 1000 keyframes")
    keys: list[dict[str, Any]] = []
    last = None
    for i, key in enumerate(value):
        if not isinstance(key, dict):
            raise ToolError(f"{TOOL}: keys[{i}] must be an object")
        check_only(key, _KEY_FIELDS, f"{TOOL} keys[{i}]")
        frame = as_int(key.get("frame"), f"keys[{i}].frame", 0, MAX_FRAME)
        if last is not None and frame <= last:
            raise ToolError(f"{TOOL}: keys must have strictly increasing frames")
        last = frame
        parsed: dict[str, Any] = {"frame": frame}
        for field in _TRANSFORMS:
            if field in key:
                parsed[field] = as_vec3(key[field], f"keys[{i}].{field}")
        if "lens" in key:
            parsed["lens"] = as_num(key["lens"], f"keys[{i}].lens", 1, 5000)
        if "inputs" in key:
            parsed["inputs"] = _inputs(key["inputs"], f"keys[{i}].inputs")
        if len(parsed) == 1:
            raise ToolError(f"{TOOL}: keys[{i}] needs location, rotation, scale, lens or inputs")
        keys.append(parsed)
    return keys


def _material(obj: Any, name: str | None) -> Any:
    if name is None:
        mat = getattr(obj, "active_material", None)
        if mat is None:
            raise ToolError(f"{TOOL}: {obj.name!r} has no material to key inputs on")
        return mat
    for slot in getattr(obj, "material_slots", None) or []:
        mat = slot.material
        if mat is not None and mat.name == name:
            return mat
    raise ToolError(f"{TOOL}: {obj.name!r} has no material named {name!r}")


def object_keyframe(bpy: Any, args: dict[str, Any]) -> Report:
    check_only(args, {"object", "keys", "easing", "material"}, TOOL)
    keys = parse_keys(args.get("keys"))
    easing = as_choice(args.get("easing", "ease_in_out"), "easing", {k: k for k in EASINGS})
    wanted = as_name(args["material"], "material") if "material" in args else None
    obj = find_object(bpy, args.get("object"), TOOL)
    if any("lens" in k for k in keys) and obj.type != "CAMERA":
        raise ToolError(f"{TOOL}: lens applies only to camera objects")
    keyed_inputs = sorted({n for k in keys for n in k.get("inputs", {})})
    if wanted is not None and not keyed_inputs:
        raise ToolError(f"{TOOL}: material applies only to keys with inputs")
    sockets: dict[str, Any] = {}
    tree = None
    if keyed_inputs:
        mat = _material(obj, wanted)
        tree = getattr(mat, "node_tree", None)
        bsdf = tree.nodes.get(BSDF) if tree is not None else None
        if bsdf is None:
            raise ToolError(f"{TOOL}: material {mat.name!r} has no {BSDF} node")
        for name in keyed_inputs:
            socket = bsdf.inputs.get(INPUTS[name][0])
            if socket is None:
                raise ToolError(f"{TOOL}: {BSDF} has no {INPUTS[name][0]!r} input")
            sockets[name] = socket
    for key in keys:
        frame = key["frame"]
        for field, path in _TRANSFORMS.items():
            if field in key:
                setattr(obj, path, key[field])
                obj.keyframe_insert(data_path=path, frame=frame)
        if "lens" in key:
            obj.data.lens = key["lens"]
            obj.data.keyframe_insert(data_path="lens", frame=frame)
        for name, value in key.get("inputs", {}).items():
            sockets[name].default_value = value
            sockets[name].keyframe_insert(data_path="default_value", frame=frame)
    interpolation, ease = EASINGS[easing]
    blocks = [obj] + ([obj.data] if obj.data is not None else []) + ([tree] if tree else [])
    for block in blocks:
        for fc in _fcurves(block):
            for point in fc.keyframe_points:
                point.interpolation = interpolation
                if ease is not None:
                    point.easing = ease
    channels = [f for f in (*_TRANSFORMS, "lens") if any(f in k for k in keys)]
    return {
        "object": str(obj.name),
        "easing": easing,
        "frames": [k["frame"] for k in keys],
        "channels": channels + [f"inputs.{n}" for n in keyed_inputs],
    }


def render_set_frame_range(bpy: Any, args: dict[str, Any]) -> Report:
    check_only(args, {"frame_start", "frame_end"}, RANGE_TOOL)
    start = as_int(args.get("frame_start"), "frame_start", 0, MAX_FRAME)
    end = as_int(args.get("frame_end"), "frame_end", 0, MAX_FRAME)
    if end <= start:
        raise ToolError(f"{RANGE_TOOL}: frame_end must be after frame_start")
    scene = bpy.context.scene
    scene.frame_start = start
    scene.frame_end = end
    return {"frame_start": start, "frame_end": end}
