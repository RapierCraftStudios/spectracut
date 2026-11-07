# SPDX-License-Identifier: AGPL-3.0-or-later
"""Material ops.

``material.apply_preset``: Principled BSDF presets plus a base colour, with
optional typed overrides (roughness, IOR, transmission, emission, anisotropy,
metallic) applied after the preset.
``material.apply_screen``: an emissive image texture (a product UI on a device
screen), loaded only from the host's assets directory and checked against the
recipe's sha256 before Blender reads it.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from bpy_runtime.ops import Report, ToolError, as_choice, as_name, as_num, check_only, find_object
from bpy_runtime.ops.scene import assets_root, resolve_asset

# Principled BSDF socket values per preset (Blender 4.x socket names).
PRESETS: dict[str, dict[str, float]] = {
    "matte_plastic": {"Metallic": 0.0, "Roughness": 0.6, "IOR": 1.46},
    "glossy_plastic": {"Metallic": 0.0, "Roughness": 0.15, "IOR": 1.46, "Coat Weight": 0.3},
    "brushed_metal": {"Metallic": 1.0, "Roughness": 0.35, "Anisotropic": 0.6},
    "polished_metal": {"Metallic": 1.0, "Roughness": 0.08},
    "glass": {"Metallic": 0.0, "Roughness": 0.0, "IOR": 1.5, "Transmission Weight": 1.0},
    "rubber": {"Metallic": 0.0, "Roughness": 0.9, "IOR": 1.52},
    "ceramic": {"Metallic": 0.0, "Roughness": 0.25, "IOR": 1.5, "Coat Weight": 0.5},
    "clear_glass": {"Metallic": 0.0, "Roughness": 0.0, "IOR": 1.5, "Transmission Weight": 1.0},
    "frosted_glass": {
        "Metallic": 0.0,
        "Roughness": 0.35,
        "IOR": 1.5,
        "Transmission Weight": 1.0,
    },
    "smoked_glass": {
        "Metallic": 0.0,
        "Roughness": 0.05,
        "IOR": 1.5,
        "Transmission Weight": 1.0,
    },
    "emissive": {"Metallic": 0.0, "Roughness": 0.5, "Emission Strength": 10.0},
    "anodised_metal": {"Metallic": 1.0, "Roughness": 0.3, "Anisotropic": 0.5},
}
DEFAULT_COLORS: dict[str, tuple[float, float, float]] = {
    "matte_plastic": (0.8, 0.8, 0.8),
    "glossy_plastic": (0.8, 0.8, 0.8),
    "brushed_metal": (0.75, 0.75, 0.76),
    "polished_metal": (0.9, 0.9, 0.9),
    "glass": (1.0, 1.0, 1.0),
    "rubber": (0.05, 0.05, 0.05),
    "ceramic": (0.95, 0.95, 0.93),
    "clear_glass": (1.0, 1.0, 1.0),
    "frosted_glass": (0.95, 0.96, 0.97),
    "smoked_glass": (0.25, 0.25, 0.27),
    "emissive": (1.0, 1.0, 1.0),
    "anodised_metal": (0.2, 0.3, 0.6),
}
# Presets whose emission colour follows the base colour unless emission_color is given.
EMISSIVE = frozenset({"emissive"})

# Optional overrides: argument -> (Principled BSDF socket, low, high).
OVERRIDES: dict[str, tuple[str, float, float]] = {
    "roughness": ("Roughness", 0.0, 1.0),
    "ior": ("IOR", 1.0, 3.0),
    "transmission": ("Transmission Weight", 0.0, 1.0),
    "emission_strength": ("Emission Strength", 0.0, 1000.0),
    "anisotropy": ("Anisotropic", 0.0, 1.0),
    "metallic": ("Metallic", 0.0, 1.0),
}


def as_color(value: Any, what: str = "color") -> tuple[float, float, float, float]:
    if not isinstance(value, list) or len(value) not in (3, 4):
        raise ToolError(f"{what} must be a list of 3 or 4 numbers in [0, 1]")
    rgba = [as_num(v, f"{what}[{i}]", 0, 1) for i, v in enumerate(value)]
    if len(rgba) == 3:
        rgba.append(1.0)
    return (rgba[0], rgba[1], rgba[2], rgba[3])


def material_apply_preset(bpy: Any, args: dict[str, Any]) -> Report:
    tool = "material.apply_preset"
    check_only(args, {"object", "preset", "color", "name", "emission_color", *OVERRIDES}, tool)
    obj = find_object(bpy, args.get("object"), tool)
    preset = as_choice(args.get("preset"), "preset", {k: k for k in PRESETS})
    color = as_color(args["color"]) if "color" in args else (*DEFAULT_COLORS[preset], 1.0)
    name = as_name(args.get("name", f"{preset}.{args['object']}"), "name")
    overrides = {
        key: as_num(args[key], key, lo, hi) for key, (_, lo, hi) in OVERRIDES.items() if key in args
    }
    emission: tuple[float, float, float, float] | None = None
    if "emission_color" in args:
        emission = as_color(args["emission_color"], "emission_color")
    elif preset in EMISSIVE:
        emission = color
    if obj.type not in ("MESH", "CURVE"):
        raise ToolError(f"{tool}: {args['object']!r} is not a mesh or curve object")
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf is None:
        raise ToolError(f"{tool}: new material has no Principled BSDF node")
    bsdf.inputs["Base Color"].default_value = color
    for socket, value in PRESETS[preset].items():
        if socket in bsdf.inputs:
            bsdf.inputs[socket].default_value = value
    if emission is not None and "Emission Color" in bsdf.inputs:
        bsdf.inputs["Emission Color"].default_value = emission
    for key, value in overrides.items():
        socket = OVERRIDES[key][0]
        if socket in bsdf.inputs:
            bsdf.inputs[socket].default_value = value
    mat.diffuse_color = color
    obj.data.materials.clear()
    obj.data.materials.append(mat)
    report: dict[str, Any] = {"object": args["object"], "material": str(mat.name), "preset": preset}
    report.update(overrides)
    if "emission_color" in args and emission is not None:
        report["emission_color"] = list(emission)
    return report


IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".exr", ".webp")
_HEX = frozenset("0123456789abcdef")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def material_apply_screen(bpy: Any, args: dict[str, Any]) -> Report:
    tool = "material.apply_screen"
    check_only(args, {"object", "image", "image_sha256", "strength"}, tool)
    want = args.get("image_sha256")
    if not isinstance(want, str) or len(want) != 64 or not set(want) <= _HEX:
        raise ToolError(f"{tool}: image_sha256 must be 64 lowercase hex characters")
    strength = as_num(args.get("strength", 1.0), "strength", 0, 100)
    obj = find_object(bpy, args.get("object"), tool)
    if obj.type != "MESH":
        raise ToolError(f"{tool}: {args['object']!r} is not a mesh object")
    full = resolve_asset(args.get("image"), assets_root(), IMAGE_SUFFIXES)
    got = file_sha256(full)
    if got != want:
        raise ToolError(
            f"{tool}: image {args['image']!r} has sha256 {got}, the recipe expects {want}",
            "asset.hash_mismatch",
        )
    image = bpy.data.images.load(str(full), check_existing=True)
    mat = bpy.data.materials.new(f"screen.{args['object']}"[:63])
    mat.use_nodes = True
    tree = mat.node_tree
    tree.nodes.clear()
    output = tree.nodes.new("ShaderNodeOutputMaterial")
    emission = tree.nodes.new("ShaderNodeEmission")
    texture = tree.nodes.new("ShaderNodeTexImage")
    texture.image = image
    emission.inputs["Strength"].default_value = strength
    tree.links.new(texture.outputs["Color"], emission.inputs["Color"])
    tree.links.new(emission.outputs["Emission"], output.inputs["Surface"])
    obj.data.materials.clear()
    obj.data.materials.append(mat)
    return {
        "object": args["object"],
        "material": str(mat.name),
        "image": args["image"],
        "image_sha256": want,
        "strength": strength,
    }
