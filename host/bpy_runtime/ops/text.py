# SPDX-License-Identifier: AGPL-3.0-or-later
"""World-space text op: ``text.add`` adds extruded depth type or a cutout.

``mode="depth"`` adds a font object with ``extrude``. ``mode="cutout"`` turns that
text into a hidden mesh cutter and adds a Boolean DIFFERENCE modifier to the
named ``plate``, so the letters are cut through it. An optional font asset is
loaded only from the host assets dir after its sha256 matches. Without one,
Blender's built-in font is used. Arguments are checked before ``bpy`` is touched.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from bpy_runtime.ops import (
    Report,
    ToolError,
    as_name,
    as_num,
    as_vec3,
    check_only,
    find_object,
)
from bpy_runtime.ops.material import file_sha256
from bpy_runtime.ops.scene import assets_root, resolve_asset
from bpy_runtime.type3d import FONT_SUFFIXES

TOOL = "text.add"
MODES = ("depth", "cutout")
MAX_BODY = 500
_HEX = frozenset("0123456789abcdef")


def _font(value: Any) -> Path:
    if not isinstance(value, dict):
        raise ToolError(f"{TOOL}: font must be {{path, sha256}}")
    check_only(value, {"path", "sha256"}, f"{TOOL} font")
    want = value.get("sha256")
    if not isinstance(want, str) or len(want) != 64 or not set(want) <= _HEX:
        raise ToolError(f"{TOOL}: font.sha256 must be 64 lowercase hex characters")
    full = resolve_asset(value.get("path"), assets_root(), FONT_SUFFIXES)
    got = file_sha256(full)
    if got != want:
        raise ToolError(
            f"{TOOL}: font {value.get('path')!r} has sha256 {got}, the recipe expects {want}",
            "asset.hash_mismatch",
        )
    return full


def _parse(args: dict[str, Any]) -> dict[str, Any]:
    check_only(
        args,
        {"body", "name", "size", "extrude", "mode", "plate", "location", "rotation", "font"},
        TOOL,
    )
    body = args.get("body")
    if not isinstance(body, str) or not 0 < len(body) <= MAX_BODY or "\x00" in body:
        raise ToolError(f"{TOOL}: body must be a non-empty string of at most {MAX_BODY} chars")
    mode = args.get("mode", "depth")
    if mode not in MODES:
        raise ToolError(f"{TOOL}: mode must be one of {list(MODES)}")
    if (mode == "cutout") != ("plate" in args):
        raise ToolError(f"{TOOL}: plate is required for mode 'cutout' and only allowed there")
    return {
        "body": body,
        "name": as_name(args.get("name", "Text"), "name"),
        "size": as_num(args.get("size", 1.0), "size", 0.001, 1000),
        "extrude": as_num(args.get("extrude", 0.1), "extrude", 0, 100),
        "mode": mode,
        "plate": as_name(args["plate"], "plate") if "plate" in args else None,
        "location": as_vec3(args.get("location", [0, 0, 0]), "location"),
        "rotation": as_vec3(args.get("rotation", [0, 0, 0]), "rotation"),
        "font": _font(args["font"]) if "font" in args else None,
    }


def text_add(bpy: Any, args: dict[str, Any]) -> Report:
    p = _parse(args)
    plate = find_object(bpy, p["plate"], TOOL, "plate") if p["plate"] else None
    if plate is not None and plate.type != "MESH":
        raise ToolError(f"{TOOL}: plate {p['plate']!r} is not a mesh object")
    curve = bpy.data.curves.new(p["name"], type="FONT")
    curve.body = p["body"]
    curve.size = p["size"]
    curve.extrude = p["extrude"]
    curve.align_x = "CENTER"
    curve.align_y = "CENTER"
    if p["font"] is not None:
        curve.font = bpy.data.fonts.load(str(p["font"]), check_existing=True)
    obj = bpy.data.objects.new(p["name"], curve)
    bpy.context.scene.collection.objects.link(obj)
    obj.location = p["location"]
    obj.rotation_euler = p["rotation"]
    report: dict[str, Any] = {
        "mode": p["mode"],
        "font": args["font"]["path"] if p["font"] is not None else "builtin",
    }
    if plate is not None:
        # A Boolean operand must be a mesh: bake the text into a hidden cutter.
        depsgraph = bpy.context.evaluated_depsgraph_get()
        mesh = bpy.data.meshes.new_from_object(obj.evaluated_get(depsgraph))
        bpy.data.objects.remove(obj, do_unlink=True)
        obj = bpy.data.objects.new(p["name"], mesh)
        bpy.context.scene.collection.objects.link(obj)
        obj.location = p["location"]
        obj.rotation_euler = p["rotation"]
        obj.hide_render = True
        obj.display_type = "WIRE"
        mod_name = f"cutout.{p['name']}"[:63]
        mod = plate.modifiers.new(name=mod_name, type="BOOLEAN")
        mod.operation = "DIFFERENCE"
        mod.object = obj
        report["plate"] = p["plate"]
        report["modifier"] = mod_name
    report["object"] = str(obj.name)
    return report
