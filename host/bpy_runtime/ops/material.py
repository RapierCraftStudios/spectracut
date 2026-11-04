# SPDX-License-Identifier: AGPL-3.0-or-later
"""``material.apply_preset``: Principled BSDF presets plus a base colour."""

from __future__ import annotations

from typing import Any

from bpy_runtime.ops import Report, ToolError, as_choice, as_name, as_num, check_only, find_object

# Principled BSDF socket values per preset (Blender 4.x socket names).
PRESETS: dict[str, dict[str, float]] = {
    "matte_plastic": {"Metallic": 0.0, "Roughness": 0.6, "IOR": 1.46},
    "glossy_plastic": {"Metallic": 0.0, "Roughness": 0.15, "IOR": 1.46, "Coat Weight": 0.3},
    "brushed_metal": {"Metallic": 1.0, "Roughness": 0.35, "Anisotropic": 0.6},
    "polished_metal": {"Metallic": 1.0, "Roughness": 0.08},
    "glass": {"Metallic": 0.0, "Roughness": 0.0, "IOR": 1.5, "Transmission Weight": 1.0},
    "rubber": {"Metallic": 0.0, "Roughness": 0.9, "IOR": 1.52},
    "ceramic": {"Metallic": 0.0, "Roughness": 0.25, "IOR": 1.5, "Coat Weight": 0.5},
}
DEFAULT_COLORS: dict[str, tuple[float, float, float]] = {
    "matte_plastic": (0.8, 0.8, 0.8),
    "glossy_plastic": (0.8, 0.8, 0.8),
    "brushed_metal": (0.75, 0.75, 0.76),
    "polished_metal": (0.9, 0.9, 0.9),
    "glass": (1.0, 1.0, 1.0),
    "rubber": (0.05, 0.05, 0.05),
    "ceramic": (0.95, 0.95, 0.93),
}


def _color(value: Any) -> tuple[float, float, float, float]:
    if not isinstance(value, list) or len(value) not in (3, 4):
        raise ToolError("color must be a list of 3 or 4 numbers in [0, 1]")
    rgba = [as_num(v, f"color[{i}]", 0, 1) for i, v in enumerate(value)]
    if len(rgba) == 3:
        rgba.append(1.0)
    return (rgba[0], rgba[1], rgba[2], rgba[3])


def material_apply_preset(bpy: Any, args: dict[str, Any]) -> Report:
    check_only(args, {"object", "preset", "color", "name"}, "material.apply_preset")
    obj = find_object(bpy, args.get("object"), "material.apply_preset")
    preset = as_choice(args.get("preset"), "preset", {k: k for k in PRESETS})
    color = _color(args["color"]) if "color" in args else (*DEFAULT_COLORS[preset], 1.0)
    name = as_name(args.get("name", f"{preset}.{args['object']}"), "name")
    if obj.type != "MESH":
        raise ToolError(f"material.apply_preset: {args['object']!r} is not a mesh object")
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf is None:
        raise ToolError("material.apply_preset: new material has no Principled BSDF node")
    bsdf.inputs["Base Color"].default_value = color
    for socket, value in PRESETS[preset].items():
        if socket in bsdf.inputs:
            bsdf.inputs[socket].default_value = value
    mat.diffuse_color = color
    obj.data.materials.clear()
    obj.data.materials.append(mat)
    return {"object": args["object"], "material": str(mat.name), "preset": preset}
