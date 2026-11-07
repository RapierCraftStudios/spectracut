# SPDX-License-Identifier: AGPL-3.0-or-later
"""Material ops.

``material.apply_preset``: Principled BSDF presets plus a base colour, with
optional typed overrides (roughness, IOR, transmission, emission, anisotropy,
metallic) applied after the preset, and optional seeded surface detail (#335):
``bump_strength``/``bump_kind``/``bump_scale`` (noise or voronoi micro-texture),
``brushing``/``brushing_angle`` (stretched-noise streaks plus anisotropy),
``scratches`` (stretched voronoi edges) and ``edge_wear`` (a Bevel edge highlight,
Cycles only). Every height source feeds one Bump node into the BSDF Normal; each
pattern's coordinates are offset by ``random.Random(seed)``, so a seed always gives
the same surface. Without a detail strength the material is the plain preset.
``material.apply_screen``: an emissive image texture (a product UI on a device
screen), loaded only from the host's assets directory and checked against the
recipe's sha256 before Blender reads it.
"""

from __future__ import annotations

import hashlib
import math
import random
from pathlib import Path
from typing import Any

from bpy_runtime.ops import (
    Report,
    ToolError,
    as_choice,
    as_int,
    as_name,
    as_num,
    check_only,
    find_object,
)
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


# Surface-detail arguments: argument -> (low, high); bump_kind and seed are checked apart.
DETAIL: dict[str, tuple[float, float]] = {
    "bump_strength": (0.0, 1.0),
    "bump_scale": (0.0, 1000.0),
    "brushing": (0.0, 1.0),
    "brushing_angle": (0.0, 360.0),
    "scratches": (0.0, 1.0),
    "edge_wear": (0.0, 1.0),
}
DETAIL_KEYS = frozenset({*DETAIL, "bump_kind", "seed"})
BUMP_KINDS = {"noise": "noise", "voronoi": "voronoi"}
SEED_MAX = 2**31 - 1
BUMP_SCALE_DEFAULT = 150.0
BUMP_DISTANCE = 0.002  # m: bump height range, fine enough to read as micro-texture
BRUSH_SCALE = (1.0, 900.0, 900.0)  # long along x (rotated by brushing_angle), fine across
SCRATCH_SCALE = (3.0, 40.0, 40.0)
SCRATCH_WIDTH = 0.015  # voronoi edge distance that counts as a scratch line
EDGE_RADIUS = 0.03  # m of bevel at edge_wear 1


def as_detail(args: dict[str, Any]) -> dict[str, Any]:
    """The given detail arguments, range-checked before bpy is touched."""
    detail: dict[str, Any] = {
        key: as_num(args[key], key, lo, hi) for key, (lo, hi) in DETAIL.items() if key in args
    }
    if detail.get("bump_scale") == 0.0:
        raise ToolError("bump_scale must be greater than 0")
    if detail.get("brushing_angle") == 360.0:
        raise ToolError("brushing_angle must be below 360")
    if "bump_kind" in args:
        detail["bump_kind"] = as_choice(args["bump_kind"], "bump_kind", BUMP_KINDS)
    if "seed" in args:
        detail["seed"] = as_int(args["seed"], "seed", 0, SEED_MAX)
    return detail


def wants_detail(detail: dict[str, Any]) -> bool:
    return any(
        detail.get(k, 0.0) > 0 for k in ("bump_strength", "brushing", "scratches", "edge_wear")
    )


def build_detail(tree: Any, bsdf: Any, detail: dict[str, Any]) -> int:
    """Wire the procedural surface detail into ``bsdf``; returns the seed used."""
    seed = int(detail.get("seed", 0))
    rng = random.Random(seed)
    nodes, links = tree.nodes, tree.links
    coord = nodes.new("ShaderNodeTexCoord")

    def mapping(scale: tuple[float, float, float], angle: float = 0.0) -> Any:
        node = nodes.new("ShaderNodeMapping")
        node.inputs["Location"].default_value = tuple(
            round(rng.uniform(-100.0, 100.0), 4) for _ in range(3)
        )
        node.inputs["Rotation"].default_value = (0.0, 0.0, math.radians(angle))
        node.inputs["Scale"].default_value = scale
        links.new(coord.outputs["Object"], node.inputs["Vector"])
        return node

    heights: list[tuple[Any, float]] = []
    bump_strength = detail.get("bump_strength", 0.0)
    if bump_strength > 0:
        scale = detail.get("bump_scale", BUMP_SCALE_DEFAULT)
        mapped = mapping((1.0, 1.0, 1.0))
        if detail.get("bump_kind", "noise") == "voronoi":
            tex = nodes.new("ShaderNodeTexVoronoi")
            tex.feature = "F1"
            out = tex.outputs["Distance"]
        else:
            tex = nodes.new("ShaderNodeTexNoise")
            tex.inputs["Detail"].default_value = 8.0
            tex.inputs["Roughness"].default_value = 0.6
            out = tex.outputs["Fac"]
        tex.inputs["Scale"].default_value = scale
        links.new(mapped.outputs["Vector"], tex.inputs["Vector"])
        heights.append((out, bump_strength))
    brushing = detail.get("brushing", 0.0)
    angle = detail.get("brushing_angle", 0.0)
    if brushing > 0:
        mapped = mapping(BRUSH_SCALE, angle)
        tex = nodes.new("ShaderNodeTexNoise")
        tex.inputs["Scale"].default_value = 1.0
        tex.inputs["Detail"].default_value = 2.0
        links.new(mapped.outputs["Vector"], tex.inputs["Vector"])
        heights.append((tex.outputs["Fac"], 0.5 * brushing))
        bsdf.inputs["Anisotropic"].default_value = max(
            float(bsdf.inputs["Anisotropic"].default_value or 0.0), brushing
        )
        bsdf.inputs["Anisotropic Rotation"].default_value = angle / 360.0
    scratches = detail.get("scratches", 0.0)
    if scratches > 0:
        mapped = mapping(SCRATCH_SCALE, angle + 30.0)
        tex = nodes.new("ShaderNodeTexVoronoi")
        tex.feature = "DISTANCE_TO_EDGE"
        tex.inputs["Scale"].default_value = 1.0
        links.new(mapped.outputs["Vector"], tex.inputs["Vector"])
        line = nodes.new("ShaderNodeMapRange")
        line.inputs["From Min"].default_value = 0.0
        line.inputs["From Max"].default_value = SCRATCH_WIDTH
        line.inputs["To Min"].default_value = 1.0
        line.inputs["To Max"].default_value = 0.0
        links.new(tex.outputs["Distance"], line.inputs["Value"])
        heights.append((line.outputs["Result"], -0.4 * scratches))
        rough = nodes.new("ShaderNodeMath")
        rough.operation = "MULTIPLY_ADD"
        rough.use_clamp = True
        rough.inputs[1].default_value = 0.3 * scratches
        rough.inputs[2].default_value = float(bsdf.inputs["Roughness"].default_value or 0.0)
        links.new(line.outputs["Result"], rough.inputs[0])
        links.new(rough.outputs["Value"], bsdf.inputs["Roughness"])
    normal_in = bsdf.inputs["Normal"]
    if heights:
        total: Any = None
        for out, weight in heights:
            term = nodes.new("ShaderNodeMath")
            term.operation = "MULTIPLY_ADD"
            term.inputs[1].default_value = weight
            term.inputs[2].default_value = 0.0
            links.new(out, term.inputs[0])
            if total is not None:
                links.new(total, term.inputs[2])
            total = term.outputs["Value"]
        bump = nodes.new("ShaderNodeBump")
        bump.inputs["Strength"].default_value = 1.0
        bump.inputs["Distance"].default_value = BUMP_DISTANCE
        links.new(total, bump.inputs["Height"])
        links.new(bump.outputs["Normal"], bsdf.inputs["Normal"])
        normal_in = bump.inputs["Normal"]
    edge_wear = detail.get("edge_wear", 0.0)
    if edge_wear > 0:
        bevel = nodes.new("ShaderNodeBevel")
        bevel.samples = 8
        bevel.inputs["Radius"].default_value = EDGE_RADIUS * edge_wear
        links.new(bevel.outputs["Normal"], normal_in)
    return seed


def as_color(value: Any, what: str = "color") -> tuple[float, float, float, float]:
    if not isinstance(value, list) or len(value) not in (3, 4):
        raise ToolError(f"{what} must be a list of 3 or 4 numbers in [0, 1]")
    rgba = [as_num(v, f"{what}[{i}]", 0, 1) for i, v in enumerate(value)]
    if len(rgba) == 3:
        rgba.append(1.0)
    return (rgba[0], rgba[1], rgba[2], rgba[3])


#: Object types whose data carries material slots (``text.add`` makes FONT objects).
MATERIAL_TYPES = ("MESH", "CURVE", "FONT", "SURFACE", "META")


def material_apply_preset(bpy: Any, args: dict[str, Any]) -> Report:
    tool = "material.apply_preset"
    check_only(
        args,
        {"object", "preset", "color", "name", "emission_color", *OVERRIDES, *DETAIL_KEYS},
        tool,
    )
    obj = find_object(bpy, args.get("object"), tool)
    preset = as_choice(args.get("preset"), "preset", {k: k for k in PRESETS})
    color = as_color(args["color"]) if "color" in args else (*DEFAULT_COLORS[preset], 1.0)
    name = as_name(args.get("name", f"{preset}.{args['object']}"), "name")
    overrides = {
        key: as_num(args[key], key, lo, hi) for key, (_, lo, hi) in OVERRIDES.items() if key in args
    }
    detail = as_detail(args)
    emission: tuple[float, float, float, float] | None = None
    if "emission_color" in args:
        emission = as_color(args["emission_color"], "emission_color")
    elif preset in EMISSIVE:
        emission = color
    if obj.type not in MATERIAL_TYPES or not hasattr(obj.data, "materials"):
        raise ToolError(f"{tool}: {args['object']!r} has no material slots (type {obj.type})")
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
    if wants_detail(detail):
        detail["seed"] = build_detail(mat.node_tree, bsdf, detail)
    mat.diffuse_color = color
    obj.data.materials.clear()
    obj.data.materials.append(mat)
    report: dict[str, Any] = {"object": args["object"], "material": str(mat.name), "preset": preset}
    report.update(overrides)
    report.update(detail)
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
