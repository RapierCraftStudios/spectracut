# SPDX-License-Identifier: AGPL-3.0-or-later
"""World-space text op: ``text.add`` adds extruded depth type or a cutout.

``mode="depth"`` adds a font object with ``extrude``. ``mode="cutout"`` turns that
text into a hidden mesh cutter and adds a Boolean DIFFERENCE modifier to the
named ``plate``, so the letters are cut through it. ``align`` sets which edge of
the line sits on ``location`` (``left`` for key/value columns, ``center`` by
default) and ``bevel`` rounds the extruded edges.

Fonts: an optional ``font`` asset is loaded only from the host assets dir after its
sha256 matches. Without one, the brand font shipped with the host package
(Inter, SIL OFL, ``bpy_runtime/fonts``) is loaded at ``weight`` 400 or 600 after its
pinned sha256 matches. A missing or altered font fails the step (``font.missing`` /
``asset.hash_mismatch``); Blender's built-in font is never used as a silent fallback.
The body is set verbatim. Arguments are checked before ``bpy`` is touched.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from bpy_runtime.ops import (
    Report,
    ToolError,
    as_choice,
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
ALIGNS = {"left": "LEFT", "center": "CENTER", "right": "RIGHT"}
FONTS_DIR = Path(__file__).resolve().parent.parent / "fonts"
"""The brand fonts shipped with the host package (Inter 4.1, SIL Open Font License)."""
BRAND_FONTS: dict[int, tuple[str, str]] = {
    400: ("Inter-Regular.ttf", "40d692fce188e4471e2b3cba937be967878f631ad3ebbbdcd587687c7ebe0c82"),
    600: ("Inter-SemiBold.ttf", "78a843fade9d4612a5567302fb595b56976eb5fcebf4fea5a5912d638bafcde3"),
}
"""weight -> (file in :data:`FONTS_DIR`, pinned sha256)."""


def brand_font(weight: int, fonts_dir: Path = FONTS_DIR) -> Path:
    """The shipped brand font file for ``weight``, checked against its pinned sha256."""
    if weight not in BRAND_FONTS:
        raise ToolError(f"{TOOL}: weight must be one of {sorted(BRAND_FONTS)}")
    name, want = BRAND_FONTS[weight]
    path = Path(fonts_dir) / name
    if not path.is_file():
        raise ToolError(
            f"{TOOL}: brand font {name} is missing from {fonts_dir}; reinstall spectracut-host "
            "(spectracut setup) or pass a pinned font",
            "font.missing",
        )
    got = file_sha256(path)
    if got != want:
        raise ToolError(
            f"{TOOL}: brand font {path} has sha256 {got}, expected {want}",
            "asset.hash_mismatch",
        )
    return path


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
        {
            "body",
            "name",
            "size",
            "extrude",
            "bevel",
            "mode",
            "plate",
            "location",
            "rotation",
            "font",
            "align",
            "weight",
        },
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
    if "font" in args and "weight" in args:
        raise ToolError(f"{TOOL}: weight picks the host brand font and is not allowed with font")
    weight = args.get("weight", 400)
    if isinstance(weight, bool) or weight not in BRAND_FONTS:
        raise ToolError(f"{TOOL}: weight must be one of {sorted(BRAND_FONTS)}")
    return {
        "body": body,
        "name": as_name(args.get("name", "Text"), "name"),
        "size": as_num(args.get("size", 1.0), "size", 0.001, 1000),
        "extrude": as_num(args.get("extrude", 0.1), "extrude", 0, 100),
        "bevel": as_num(args.get("bevel", 0.0), "bevel", 0, 10),
        "align": as_choice(args.get("align", "center"), "align", ALIGNS),
        "mode": mode,
        "plate": as_name(args["plate"], "plate") if "plate" in args else None,
        "location": as_vec3(args.get("location", [0, 0, 0]), "location"),
        "rotation": as_vec3(args.get("rotation", [0, 0, 0]), "rotation"),
        "font": _font(args["font"]) if "font" in args else None,
        "brand_font": None if "font" in args else brand_font(int(weight)),
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
    if p["bevel"]:
        curve.bevel_depth = p["bevel"]
    curve.align_x = p["align"]
    curve.align_y = "CENTER"
    font = p["font"] if p["font"] is not None else p["brand_font"]
    curve.font = bpy.data.fonts.load(str(font), check_existing=True)
    obj = bpy.data.objects.new(p["name"], curve)
    bpy.context.scene.collection.objects.link(obj)
    obj.location = p["location"]
    obj.rotation_euler = p["rotation"]
    report: dict[str, Any] = {
        "mode": p["mode"],
        "font": args["font"]["path"] if p["font"] is not None else p["brand_font"].name,
    }
    if "align" in args:
        report["align"] = args["align"]
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
