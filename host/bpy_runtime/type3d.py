# SPDX-License-Identifier: AGPL-3.0-or-later
"""``type.add_2d`` and ``type.add_3d``: motion typography in the bpy runtime.

The control plane (``spectracut.finishing.typography``) has already copy-gated
the text, resolved the brand font and laid the card out in whole pixels of the
frame; these ops only build it. Type is parented to the active camera at a
fixed distance, so one pixel of the layout is exactly one pixel of the render
(:func:`pixel_scale`): 2D type sits flat on the pixel grid, 3D type is extruded
and can take the camera's depth-of-field focus.

Animations are keyframes with Blender's own interpolation and easing:

- ``tracking_reveal``: letter spacing closes in while the type fades up;
- ``mask_wipe``: a left-to-right wipe threshold on the type's shader;
- ``glyph_stagger``: the wipe threshold steps glyph by glyph, ``stagger_frames`` apart;
- ``match_cut``: the type moves and scales from the UI text's box to its own.

Pure maths (:func:`pixel_scale`, :func:`pixel_to_camera`, :func:`reveal_keys`) is
stdlib-only and unit-tested without Blender.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
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
    check_only,
)
from bpy_runtime.ops.scene import assets_root, resolve_asset

ANIMATIONS = {
    "tracking_reveal": "tracking_reveal",
    "mask_wipe": "mask_wipe",
    "glyph_stagger": "glyph_stagger",
    "match_cut": "match_cut",
}
# easing name -> (Blender keyframe interpolation, easing)
EASINGS: dict[str, tuple[str, str]] = {
    "linear": ("LINEAR", "AUTO"),
    "ease_in": ("CUBIC", "EASE_IN"),
    "ease_out": ("CUBIC", "EASE_OUT"),
    "ease_in_out": ("CUBIC", "EASE_IN_OUT"),
    "expo_out": ("EXPO", "EASE_OUT"),
    "back_out": ("BACK", "EASE_OUT"),
}
FONT_SUFFIXES = (".ttf", ".otf")
MAX_TEXT = 500
TRACKING_FROM = 1.6
"""Letter spacing at the start of a tracking reveal (1.0 is the font's own)."""

COMMON_ARGS = {
    "name",
    "text",
    "font",
    "size_px",
    "box",
    "resolution",
    "color",
    "animation",
    "easing",
    "start",
    "reveal_frames",
    "stagger_frames",
    "match_from",
}
ARGS_2D = COMMON_ARGS
ARGS_3D = COMMON_ARGS | {"distance", "extrude", "bevel", "dof"}


# ---- pure maths ------------------------------------------------------------------------


def pixel_scale(
    lens_mm: float,
    sensor_width: float,
    sensor_height: float,
    fit: str,
    width: int,
    height: int,
    distance: float,
) -> float:
    """Scene units per render pixel on a plane ``distance`` in front of the camera."""
    if fit == "HORIZONTAL":
        return sensor_width / lens_mm * distance / width
    if fit == "VERTICAL":
        return sensor_height / lens_mm * distance / height
    return sensor_width / lens_mm * distance / max(width, height)


def pixel_to_camera(
    px: float, py: float, width: int, height: int, scale: float, distance: float
) -> tuple[float, float, float]:
    """Camera-local position of render pixel ``(px, py)`` (origin top-left)."""
    return ((px - width / 2) * scale, (height / 2 - py) * scale, -distance)


def reveal_keys(
    animation: str, start: int, reveal: int, stagger: int, glyphs: int
) -> list[tuple[int, float]]:
    """``(frame, progress)`` keys of the reveal, progress 0 (hidden) to 1 (shown).

    For ``glyph_stagger`` progress is the fraction of glyphs shown; each glyph
    takes ``reveal`` frames and starts ``stagger`` frames after the previous one.
    """
    if animation != "glyph_stagger" or glyphs <= 1:
        return [(start, 0.0), (start + reveal, 1.0)]
    keys = [(start, 0.0)]
    for i in range(glyphs):
        keys.append((start + i * stagger + reveal, (i + 1) / glyphs))
    return keys


# ---- argument checks -------------------------------------------------------------------


def _ints(value: Any, what: str, n: int, lo: int, hi: int) -> list[int]:
    if not isinstance(value, list) or len(value) != n:
        raise ToolError(f"{what} must be a list of {n} integers")
    return [as_int(v, f"{what}[{i}]", lo, hi) for i, v in enumerate(value)]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _font(value: Any, tool: str) -> tuple[dict[str, Any], Any]:
    if not isinstance(value, dict):
        raise ToolError(f"{tool}: font must be {{family, weight[, path, sha256]}}")
    check_only(value, {"family", "weight", "path", "sha256"}, f"{tool} font")
    family = value.get("family")
    if not isinstance(family, str) or not family:
        raise ToolError(f"{tool}: font.family must be a non-empty string")
    weight = as_int(value.get("weight", 400), "font.weight", 100, 900)
    path = None
    if "path" in value or "sha256" in value:
        path = resolve_asset(value.get("path"), assets_root(), FONT_SUFFIXES)
        want = value.get("sha256")
        if not isinstance(want, str) or _sha256(path) != want:
            raise ToolError(
                f"font {value.get('path')!r} does not match its pinned sha256",
                "asset.hash_mismatch",
            )
    return {"family": family, "weight": weight}, path


def _parse(args: dict[str, Any], tool: str, allowed: set[str]) -> dict[str, Any]:
    check_only(args, allowed, tool)
    text = args.get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
        raise ToolError(f"{tool}: text must be a non-empty string of at most {MAX_TEXT} chars")
    width, height = _ints(args.get("resolution"), "resolution", 2, 16, 16384)
    box = _ints(args.get("box"), "box", 4, 0, 16384)
    if box[2] < 1 or box[3] < 1 or box[0] + box[2] > width or box[1] + box[3] > height:
        raise ToolError(f"{tool}: box must lie inside the {width}x{height} frame")
    match_from = None
    if args.get("match_from") is not None:
        match_from = _ints(args.get("match_from"), "match_from", 4, 0, 16384)
    animation = as_choice(args.get("animation"), "animation", ANIMATIONS)
    if animation == "match_cut" and match_from is None:
        raise ToolError(f"{tool}: match_cut needs match_from")
    color = args.get("color", [1.0, 1.0, 1.0])
    if not isinstance(color, list) or len(color) != 3:
        raise ToolError(f"{tool}: color must be a list of 3 numbers")
    font, font_path = _font(args.get("font"), tool)
    easing = args.get("easing", "ease_out")
    if not isinstance(easing, str) or easing not in EASINGS:
        raise ToolError(f"easing must be one of {sorted(EASINGS)}")
    return {
        "name": as_name(args.get("name"), "name"),
        "text": text,
        "font": font,
        "font_path": font_path,
        "size_px": as_int(args.get("size_px"), "size_px", 1, 4096),
        "box": box,
        "resolution": (width, height),
        "color": [as_num(c, f"color[{i}]", 0.0, 1.0) for i, c in enumerate(color)],
        "animation": animation,
        "easing": easing,
        "start": as_int(args.get("start", 0), "start", 0, 1_000_000),
        "reveal": as_int(args.get("reveal_frames"), "reveal_frames", 1, 600),
        "stagger": as_int(args.get("stagger_frames", 0), "stagger_frames", 0, 60),
        "match_from": match_from,
    }


# ---- building --------------------------------------------------------------------------


def _ease(owner: Any, interpolation: str, easing: str) -> None:
    """Set interpolation and easing on every keyframe of ``owner``'s action, when present."""
    anim = getattr(owner, "animation_data", None)
    action = getattr(anim, "action", None)
    curves = getattr(action, "fcurves", None)
    if curves is None:
        return
    try:
        items = list(curves)
    except TypeError:
        return
    for curve in items:
        for point in list(getattr(curve, "keyframe_points", [])):
            point.interpolation = interpolation
            point.easing = easing


def _material(bpy: Any, name: str, color: list[float]) -> tuple[Any, Any]:
    """Emission shader with alpha = (generated X <= threshold) * fade; returns (mat, value)."""
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    if hasattr(mat, "surface_render_method"):
        mat.surface_render_method = "BLENDED"
    nodes, links = mat.node_tree.nodes, mat.node_tree.links
    nodes.clear()
    out = nodes.new("ShaderNodeOutputMaterial")
    emit = nodes.new("ShaderNodeEmission")
    emit.inputs["Color"].default_value = (*color, 1.0)
    emit.inputs["Strength"].default_value = 1.0
    clear = nodes.new("ShaderNodeBsdfTransparent")
    coords = nodes.new("ShaderNodeTexCoord")
    split = nodes.new("ShaderNodeSeparateXYZ")
    reveal = nodes.new("ShaderNodeValue")
    reveal.name = "reveal"
    reveal.outputs[0].default_value = 1.0
    shown = nodes.new("ShaderNodeMath")
    shown.operation = "LESS_THAN"
    mix = nodes.new("ShaderNodeMixShader")
    links.new(coords.outputs["Generated"], split.inputs[0])
    links.new(split.outputs["X"], shown.inputs[0])
    links.new(reveal.outputs[0], shown.inputs[1])
    links.new(shown.outputs[0], mix.inputs["Fac"])
    links.new(clear.outputs[0], mix.inputs[1])
    links.new(emit.outputs[0], mix.inputs[2])
    links.new(mix.outputs[0], out.inputs["Surface"])
    return mat, reveal


def _key_value(socket: Any, keys: list[tuple[int, float]]) -> int:
    for frame, value in keys:
        socket.default_value = value
        socket.keyframe_insert("default_value", frame=frame)
    return len(keys)


def _build(bpy: Any, p: dict[str, Any], tool: str, dimension: str, extra: dict[str, Any]) -> Report:
    camera = active_camera(bpy, tool)
    scene = bpy.context.scene
    width, height = p["resolution"]
    if (int(scene.render.resolution_x), int(scene.render.resolution_y)) != (width, height):
        raise ToolError(
            f"{tool}: laid out for {width}x{height} but the scene renders "
            f"{scene.render.resolution_x}x{scene.render.resolution_y}; set render.set_resolution"
        )
    cam = camera.data
    distance = extra.get("distance", 1.0)
    scale = pixel_scale(
        float(cam.lens),
        float(cam.sensor_width),
        float(cam.sensor_height),
        str(cam.sensor_fit),
        width,
        height,
        distance,
    )
    curve = bpy.data.curves.new(p["name"], type="FONT")
    curve.body = p["text"]
    curve.align_x = "LEFT"
    curve.align_y = "TOP"
    curve.size = p["size_px"] * scale
    if p["font_path"] is not None:
        curve.font = bpy.data.fonts.load(str(p["font_path"]), check_existing=True)
    if dimension == "3d":
        # extrude and bevel are fractions of the em size, so they scale with the type
        curve.extrude = extra["extrude"] * curve.size
        curve.bevel_depth = extra["bevel"] * curve.size
    obj = bpy.data.objects.new(p["name"], curve)
    bpy.context.scene.collection.objects.link(obj)
    obj.parent = camera
    x, y, _w, _h = p["box"]
    home = pixel_to_camera(x, y, width, height, scale, distance)
    obj.location = home
    mat, reveal = _material(bpy, p["name"] + "_mat", p["color"])
    curve.materials.append(mat)
    interpolation, easing = EASINGS[p["easing"]]
    start, n = p["start"], 0
    glyphs = max(1, sum(1 for c in p["text"] if not c.isspace()))
    animation = p["animation"]
    if animation in ("mask_wipe", "glyph_stagger"):
        keys = reveal_keys(animation, start, p["reveal"], p["stagger"], glyphs)
        n += _key_value(reveal.outputs[0], keys)
        _ease(mat.node_tree, interpolation, easing)
    elif animation == "tracking_reveal":
        for frame, spacing in ((start, TRACKING_FROM), (start + p["reveal"], 1.0)):
            curve.space_character = spacing
            curve.keyframe_insert("space_character", frame=frame)
            n += 1
        n += _key_value(reveal.outputs[0], [(start, 0.0), (start + p["reveal"], 1.0)])
        _ease(curve, interpolation, easing)
        _ease(mat.node_tree, interpolation, easing)
    else:  # match_cut: from the UI text's box to the title's own
        mx, my, _mw, mh = p["match_from"]
        ratio = mh / p["box"][3]
        frames = (
            (start, pixel_to_camera(mx, my, width, height, scale, distance), ratio),
            (start + p["reveal"], home, 1.0),
        )
        for frame, location, s in frames:
            obj.location = location
            obj.scale = (s, s, s)
            obj.keyframe_insert(data_path="location", frame=frame)
            obj.keyframe_insert(data_path="scale", frame=frame)
            n += 2
        _ease(obj, interpolation, easing)
    report: dict[str, Any] = {
        "name": p["name"],
        "dimension": dimension,
        "animation": animation,
        "easing": p["easing"],
        "font": p["font"],
        "box": p["box"],
        "glyphs": glyphs,
        "keys": n,
        "pixel_scale": scale,
    }
    if dimension == "3d" and extra["dof"]:
        cam.dof.use_dof = True
        cam.dof.focus_object = obj
        report["dof_focus"] = p["name"]
    return report


def type_add_2d(bpy: Any, args: dict[str, Any]) -> Report:
    p = _parse(args, "type.add_2d", ARGS_2D)
    return _build(bpy, p, "type.add_2d", "2d", {"distance": 1.0})


def type_add_3d(bpy: Any, args: dict[str, Any]) -> Report:
    p = _parse(args, "type.add_3d", ARGS_3D)
    extra = {
        "distance": as_num(args.get("distance", 1.0), "distance", 0.01, 100.0),
        "extrude": as_num(args.get("extrude", 0.0), "extrude", 0.0, 1.0),
        "bevel": as_num(args.get("bevel", 0.0), "bevel", 0.0, 0.2),
        "dof": as_bool(args.get("dof", False), "dof"),
    }
    return _build(bpy, p, "type.add_3d", "3d", extra)
