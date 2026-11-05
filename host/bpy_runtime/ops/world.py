# SPDX-License-Identifier: AGPL-3.0-or-later
"""World op: ``world.set_hdri`` lights the scene from a sha256-pinned HDRI.

The HDRI is loaded only from the host assets dir, after its sha256 matches the
recipe. The world tree is Texture Coordinate -> Mapping (Z rotation) ->
Environment Texture -> Background (strength) -> World Output. Arguments are
checked, and the file hashed, before ``bpy`` is touched.
"""

from __future__ import annotations

import math
from typing import Any

from bpy_runtime.ops import Report, ToolError, as_num, check_only
from bpy_runtime.ops.material import file_sha256
from bpy_runtime.ops.scene import assets_root, resolve_asset

TOOL = "world.set_hdri"
HDRI_SUFFIXES = (".hdr", ".exr")
_HEX = frozenset("0123456789abcdef")


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
