# SPDX-License-Identifier: AGPL-3.0-or-later
"""World ops: an HDRI, a vertical gradient background, and volumetric haze.

``world.set_hdri`` lights the scene from a sha256-pinned HDRI.

The HDRI is loaded only from the host assets dir, after its sha256 matches the
recipe. The world tree is Texture Coordinate -> Mapping (Z rotation) ->
Environment Texture -> Background (strength) -> World Output. Arguments are
checked, and the file hashed, before ``bpy`` is touched.

``world.set_gradient`` replaces the world surface with a vertical gradient
(Texture Coordinate -> Separate XYZ (Z) -> Color Ramp (bottom, top) -> Background
(strength) -> World Output Surface) and keeps an existing Volume link.

``world.set_haze`` adds (or replaces) a Volume Scatter node on the World Output
Volume socket only, so it composes after any background op. Its density is capped
at :data:`HAZE_DENSITY_MAX`, a Look-safe ceiling (clarity over atmosphere); the
master is still gated by its Look's ``haze_max`` QA threshold. Place it after
``world.set_hdri``, which rebuilds the whole world tree.
"""

from __future__ import annotations

import math
from typing import Any

from bpy_runtime.ops import Report, ToolError, as_num, check_only
from bpy_runtime.ops.material import as_color, file_sha256
from bpy_runtime.ops.scene import assets_root, resolve_asset

TOOL = "world.set_hdri"
HDRI_SUFFIXES = (".hdr", ".exr")
_HEX = frozenset("0123456789abcdef")
HAZE_DENSITY_MAX = 0.05


def world_set_hdri(bpy: Any, args: dict[str, Any]) -> Report:
    check_only(args, {"path", "sha256", "strength", "rotation"}, TOOL)
    want = args.get("sha256")
    if not isinstance(want, str) or len(want) != 64 or not set(want) <= _HEX:
        raise ToolError(f"{TOOL}: sha256 must be 64 lowercase hex characters")
    strength = as_num(args.get("strength", 1.0), "strength", 0, 1000)
    rotation = as_num(args.get("rotation", 0.0), "rotation", -360, 360)
    full = resolve_asset(args.get("path"), assets_root(), HDRI_SUFFIXES)
    got = file_sha256(full)
    if got != want:
        raise ToolError(
            f"{TOOL}: {args['path']!r} has sha256 {got}, the recipe expects {want}",
            "asset.hash_mismatch",
        )
    scene = bpy.context.scene
    world = scene.world
    if world is None:
        world = bpy.data.worlds.new("World")
        scene.world = world
    world.use_nodes = True
    tree = world.node_tree
    tree.nodes.clear()
    coords = tree.nodes.new("ShaderNodeTexCoord")
    mapping = tree.nodes.new("ShaderNodeMapping")
    mapping.inputs["Rotation"].default_value = (0.0, 0.0, math.radians(rotation))
    env = tree.nodes.new("ShaderNodeTexEnvironment")
    env.image = bpy.data.images.load(str(full), check_existing=True)
    background = tree.nodes.new("ShaderNodeBackground")
    background.inputs["Strength"].default_value = strength
    output = tree.nodes.new("ShaderNodeOutputWorld")
    tree.links.new(coords.outputs["Generated"], mapping.inputs["Vector"])
    tree.links.new(mapping.outputs["Vector"], env.inputs["Vector"])
    tree.links.new(env.outputs["Color"], background.inputs["Color"])
    tree.links.new(background.outputs["Background"], output.inputs["Surface"])
    return {"path": args["path"], "sha256": want, "strength": strength, "rotation": rotation}


def _world(bpy: Any) -> Any:
    scene = bpy.context.scene
    world = scene.world
    if world is None:
        world = bpy.data.worlds.new("World")
        scene.world = world
    world.use_nodes = True
    return world


def _output(tree: Any) -> Any:
    for node in list(tree.nodes):
        if getattr(node, "bl_idname", None) == "ShaderNodeOutputWorld":
            return node
    return tree.nodes.new("ShaderNodeOutputWorld")


def world_set_gradient(bpy: Any, args: dict[str, Any]) -> Report:
    tool = "world.set_gradient"
    check_only(args, {"bottom", "top", "strength"}, tool)
    if "bottom" not in args or "top" not in args:
        raise ToolError(f"{tool}: bottom and top colours are required")
    bottom = as_color(args["bottom"], "bottom")
    top = as_color(args["top"], "top")
    strength = as_num(args.get("strength", 1.0), "strength", 0, 1000)
    tree = _world(bpy).node_tree
    keep = ("ShaderNodeOutputWorld", "ShaderNodeVolumeScatter")
    for node in list(tree.nodes):
        if getattr(node, "bl_idname", None) not in keep:
            tree.nodes.remove(node)
    output = _output(tree)
    coords = tree.nodes.new("ShaderNodeTexCoord")
    split = tree.nodes.new("ShaderNodeSeparateXYZ")
    ramp = tree.nodes.new("ShaderNodeValToRGB")
    stops = ramp.color_ramp.elements
    stops[0].position, stops[0].color = 0.0, bottom
    stops[1].position, stops[1].color = 1.0, top
    background = tree.nodes.new("ShaderNodeBackground")
    background.inputs["Strength"].default_value = strength
    tree.links.new(coords.outputs["Generated"], split.inputs["Vector"])
    tree.links.new(split.outputs["Z"], ramp.inputs["Fac"])
    tree.links.new(ramp.outputs["Color"], background.inputs["Color"])
    tree.links.new(background.outputs["Background"], output.inputs["Surface"])
    return {"bottom": list(bottom), "top": list(top), "strength": strength}


def world_set_haze(bpy: Any, args: dict[str, Any]) -> Report:
    tool = "world.set_haze"
    check_only(args, {"density", "color", "anisotropy"}, tool)
    density = as_num(args.get("density"), "density", 0, HAZE_DENSITY_MAX)
    if density <= 0:
        raise ToolError(f"density must be within (0, {HAZE_DENSITY_MAX}]")
    color = as_color(args.get("color", [1.0, 1.0, 1.0]), "color")
    anisotropy = as_num(args.get("anisotropy", 0.0), "anisotropy", -1, 1)
    tree = _world(bpy).node_tree
    for node in list(tree.nodes):
        if getattr(node, "bl_idname", None) == "ShaderNodeVolumeScatter":
            tree.nodes.remove(node)
    output = _output(tree)
    scatter = tree.nodes.new("ShaderNodeVolumeScatter")
    scatter.inputs["Density"].default_value = density
    scatter.inputs["Color"].default_value = color
    scatter.inputs["Anisotropy"].default_value = anisotropy
    tree.links.new(scatter.outputs["Volume"], output.inputs["Volume"])
    return {"density": density, "color": list(color), "anisotropy": anisotropy}
