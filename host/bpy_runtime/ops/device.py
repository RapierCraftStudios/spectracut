# SPDX-License-Identifier: AGPL-3.0-or-later
"""Device rig op: ``device.add`` builds a logo-free laptop or phone with a live screen.

The rig is plain geometry (a bevelled body, bezel, glass cover and, for a laptop, a
base deck with an untextured keyboard well and trackpad) with no text, logos or OEM
marks. The body takes a brushed-aluminium (or graphite) principled finish and the
glass a reflective, low-roughness IOR 1.5 material. The screen image is read as
sRGB so the UI keeps its display gamma. The screen plane uses the ``SC_Screen`` node group:
the product UI (a sha256-pinned image sequence or movie from the host assets dir)
drives an Emission shader, darkened by a pixel-grid mask. The grid's contrast is
multiplied by :func:`grid_fade` of its screen-space footprint, estimated as a box
filter from the camera's view distance, so the grid fades to flat before it can
alias (moire). Every argument is checked, and every asset resolved and hashed,
before ``bpy`` is touched.

With ``model`` (#362) the device is a sha256-pinned glTF kit model instead: the glb
is imported, its animation cleared, scaled uniformly so its world-bounds width is
``model.real_width`` and lifted so its lowest point is on the floor. The display
faces (``model.screen``: the faces of a node that use a material) get a planar UV
layer along the face plane, upright and unmirrored seen from the front, and a screen
material: the sRGB capture, cover-cropped from the top, as emission mixed by fresnel
with a glossy coat. ``finish`` (space-black, silver) dresses ``model.body_material``.
"""

from __future__ import annotations

import re
from pathlib import Path
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
)
from bpy_runtime.ops.material import IMAGE_SUFFIXES, file_sha256
from bpy_runtime.ops.scene import assets_root, resolve_asset

TOOL = "device.add"
MOVIE_SUFFIXES = (".mp4", ".mov", ".webm", ".mkv")
NODE_GROUP = "SC_Screen"
MAX_FRAMES = 10000

# Metres: body (width, depth, height), screen (width, height), default pixel grid.
_DIMS: dict[str, dict[str, Any]] = {
    "phone": {"body": (0.072, 0.008, 0.150), "screen": (0.066, 0.143), "grid": (1170, 2532)},
    "laptop": {"body": (0.310, 0.007, 0.210), "screen": (0.290, 0.181), "grid": (2560, 1600)},
}
_LAPTOP_BASE = (0.310, 0.215, 0.012)
# Keyboard well and trackpad on the laptop deck: (width, depth, height), y offset.
_KEYBOARD = ((0.270, 0.105, 0.0008), -0.055)
_TRACKPAD = ((0.110, 0.068, 0.0004), -0.165)
# Default corner bevel (metres) per kind; bevel 0 keeps sharp corners.
BEVEL: dict[str, float] = {"phone": 0.004, "laptop": 0.003}
MAX_BEVEL = 0.01
# Body finishes: principled values for the shell (brushed metal reads as anisotropy).
FINISHES: dict[str, dict[str, Any]] = {
    "aluminium": {
        "Base Color": (0.80, 0.80, 0.81, 1.0),
        "Metallic": 1.0,
        "Roughness": 0.28,
        "Anisotropic": 0.6,
    },
    "graphite": {
        "Base Color": (0.09, 0.09, 0.10, 1.0),
        "Metallic": 1.0,
        "Roughness": 0.32,
        "Anisotropic": 0.4,
    },
}
GLASS = {
    "Roughness": 0.02,
    "IOR": 1.5,
    "Transmission Weight": 1.0,
    "Specular IOR Level": 0.5,
    "Alpha": 0.08,
}

# Default angular size of one render pixel (36 mm sensor, 50 mm lens, 1920 px wide).
PIXEL_ANGLE = 36.0 / 50.0 / 1920.0
FADE_START = 0.25
FADE_END = 0.5
_HEX = frozenset("0123456789abcdef")
Geometry = tuple[list[tuple[float, float, float]], list[tuple[int, ...]]]
_NUMBERED = r"^(.*?)(\d+)(\.[A-Za-z0-9]+)$"


def grid_fade(footprint_cells: float) -> float:
    """Grid contrast for a render pixel covering ``footprint_cells`` grid cells.

    1 up to a quarter cell, a smoothstep to 0 by half a cell (the Nyquist limit of
    the grid), 0 above. The ``SC_Screen`` Map Range node computes the same curve.
    """
    t = min(max((footprint_cells - FADE_START) / (FADE_END - FADE_START), 0.0), 1.0)
    return 1.0 - t * t * (3.0 - 2.0 * t)


def _sha(value: Any, what: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or not set(value) <= _HEX:
        raise ToolError(f"{TOOL}: {what} must be 64 lowercase hex characters")
    return value


def _pinned(value: Any, what: str) -> tuple[str, str]:
    if not isinstance(value, dict):
        raise ToolError(f"{TOOL}: {what} must be {{path, sha256}}")
    check_only(value, {"path", "sha256"}, f"{TOOL} {what}")
    path = value.get("path")
    if not isinstance(path, str):
        raise ToolError(f"{TOOL}: {what}.path must be a string", "asset.path_refused")
    return path, _sha(value.get("sha256"), f"{what}.sha256")


def check_sequence(paths: list[str]) -> None:
    """Refuse frames that are not consecutively numbered files in one directory."""
    parts = []
    for path in paths:
        m = re.match(_NUMBERED, path)
        if m is None:
            raise ToolError(f"{TOOL}: sequence frame {path!r} must end in a frame number")
        parts.append((m.group(1), m.group(2), m.group(3)))
    head, digits, tail = parts[0]
    first = int(digits)
    for i, (h, d, t) in enumerate(parts):
        if h != head or t != tail or len(d) != len(digits) or int(d) != first + i:
            raise ToolError(
                f"{TOOL}: screen frames must be consecutively numbered files in one directory"
            )


def _screen(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ToolError(f"{TOOL}: screen must be {{frames[, frame_offset]}} or {{movie}}")
    check_only(value, {"frames", "frame_offset", "movie"}, f"{TOOL} screen")
    if ("frames" in value) == ("movie" in value):
        raise ToolError(f"{TOOL}: screen takes exactly one of frames or movie")
    if "movie" in value:
        if "frame_offset" in value:
            raise ToolError(f"{TOOL}: frame_offset applies to frames only")
        return {"movie": _pinned(value["movie"], "screen.movie")}
    frames = value["frames"]
    if not isinstance(frames, list) or not 0 < len(frames) <= MAX_FRAMES:
        raise ToolError(f"{TOOL}: screen.frames must be a list of 1 to {MAX_FRAMES} items")
    pinned = [_pinned(f, f"screen.frames[{i}]") for i, f in enumerate(frames)]
    offset = as_int(value.get("frame_offset", 0), "screen.frame_offset", 0, 1_000_000)
    if len(pinned) > 1:
        check_sequence([p for p, _ in pinned])
    return {"frames": pinned, "frame_offset": offset}


def _grid(value: Any, kind: str) -> tuple[int, int, float]:
    if value is None:
        res = _DIMS[kind]["grid"]
        return res[0], res[1], 0.15
    if not isinstance(value, dict):
        raise ToolError(f"{TOOL}: grid must be {{resolution, strength}}")
    check_only(value, {"resolution", "strength"}, f"{TOOL} grid")
    res = value.get("resolution", list(_DIMS[kind]["grid"]))
    if not isinstance(res, list) or len(res) != 2:
        raise ToolError(f"{TOOL}: grid.resolution must be [width, height]")
    w = as_int(res[0], "grid.resolution[0]", 16, 16384)
    h = as_int(res[1], "grid.resolution[1]", 16, 16384)
    return w, h, as_num(value.get("strength", 0.15), "grid.strength", 0, 1)


def _resolve(path: str, want: str, suffixes: tuple[str, ...]) -> Path:
    full = resolve_asset(path, assets_root(), suffixes)
    got = file_sha256(full)
    if got != want:
        raise ToolError(
            f"{TOOL}: {path!r} has sha256 {got}, the recipe expects {want}",
            "asset.hash_mismatch",
        )
    return full


def _parse(args: dict[str, Any]) -> dict[str, Any]:
    check_only(
        args,
        {
            "kind",
            "name",
            "location",
            "rotation",
            "screen",
            "emission_strength",
            "grid",
            "bevel",
            "finish",
            "model",
        },
        TOOL,
    )
    if "model" in args:
        return _parse_model(args)
    kind = args.get("kind")
    if not isinstance(kind, str) or kind not in _DIMS:
        raise ToolError(f"{TOOL}: kind must be one of {sorted(_DIMS)}")
    if "screen" not in args:
        raise ToolError(f"{TOOL}: screen is required")
    p: dict[str, Any] = {
        "kind": kind,
        "name": as_name(args.get("name", kind.capitalize()), "name"),
        "location": as_vec3(args.get("location", [0, 0, 0]), "location"),
        "rotation": as_vec3(args.get("rotation", [0, 0, 0]), "rotation"),
        "strength": as_num(args.get("emission_strength", 1.0), "emission_strength", 0, 100),
        "grid": _grid(args.get("grid"), kind),
        "screen": _screen(args["screen"]),
        "bevel": as_num(args.get("bevel", BEVEL[kind]), "bevel", 0, MAX_BEVEL),
        "finish": as_choice(args.get("finish", "aluminium"), "finish", {k: k for k in FINISHES}),
    }
    if len(p["name"]) > 48:
        raise ToolError(f"{TOOL}: name must be at most 48 characters")
    screen = p["screen"]
    if "movie" in screen:
        movie_path, movie_sha = screen["movie"]
        screen["paths"] = [_resolve(movie_path, movie_sha, MOVIE_SUFFIXES)]
    else:
        screen["paths"] = [_resolve(path, sha, IMAGE_SUFFIXES) for path, sha in screen["frames"]]
    return p


# --- geometry -----------------------------------------------------------------------------


def box(w: float, d: float, h: float) -> Geometry:
    x, y, z = w / 2, d / 2, h / 2
    verts = [(sx * x, sy * y, sz * z) for sz in (-1, 1) for sy in (-1, 1) for sx in (-1, 1)]
    # Counter-clockwise seen from outside, so every normal points outward.
    faces: list[tuple[int, ...]] = [
        (0, 2, 3, 1),
        (4, 5, 7, 6),
        (0, 1, 5, 4),
        (2, 6, 7, 3),
        (0, 4, 6, 2),
        (1, 3, 7, 5),
    ]
    return verts, faces


def quad(w: float, h: float) -> Geometry:
    """A quad in the XZ plane facing -Y (towards a camera on -Y)."""
    x, z = w / 2, h / 2
    faces: list[tuple[int, ...]] = [(0, 1, 2, 3)]
    return [(-x, 0.0, -z), (x, 0.0, -z), (x, 0.0, z), (-x, 0.0, z)], faces


def _mesh_object(bpy: Any, name: str, geo: Geometry, parent: Any, offset: tuple[float, ...]) -> Any:
    verts, faces = geo
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts, [], faces)
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    if parent is not None:
        obj.parent = parent
    obj.location = offset
    return obj


def _bevel(obj: Any, width: float) -> None:
    """Round the body's corners with a bevel modifier (no-op for width 0)."""
    if width <= 0:
        return
    mod = obj.modifiers.new(name="bevel", type="BEVEL")
    mod.width = width
    mod.segments = 4
    mod.limit_method = "ANGLE"


def _principled(bpy: Any, name: str, values: dict[str, Any]) -> Any:
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf is not None:
        for socket, value in values.items():
            if socket in bsdf.inputs:
                bsdf.inputs[socket].default_value = value
    return mat


def _assign(obj: Any, mat: Any) -> None:
    obj.data.materials.clear()
    obj.data.materials.append(mat)


# --- SC_Screen node group -----------------------------------------------------------------

_GROUP_INPUTS = (
    ("Color", "NodeSocketColor"),
    ("Strength", "NodeSocketFloat"),
    ("Grid X", "NodeSocketFloat"),
    ("Grid Y", "NodeSocketFloat"),
    ("Grid Strength", "NodeSocketFloat"),
    ("Cell Pitch", "NodeSocketFloat"),
    ("Pixel Angle", "NodeSocketFloat"),
)


def _math(tree: Any, op: str, a: Any = None, b: Any = None, value: float | None = None) -> Any:
    node = tree.nodes.new("ShaderNodeMath")
    node.operation = op
    for i, src in enumerate((a, b)):
        if src is not None:
            tree.links.new(src, node.inputs[i])
    if value is not None:
        node.inputs[1].default_value = value
    return node


def _grid_axis(tree: Any, coord: Any, res: Any) -> Any:
    """1 inside a pixel cell, 0 on the gap between cells, along one UV axis."""
    scaled = _math(tree, "MULTIPLY", coord, res)
    frac = _math(tree, "FRACT", scaled.outputs[0])
    return _math(tree, "LESS_THAN", frac.outputs[0], value=0.85)


def screen_node_group(bpy: Any) -> Any:
    """The shared ``SC_Screen`` shader group, built once per file."""
    group = bpy.data.node_groups.get(NODE_GROUP)
    if group is not None:
        return group
    group = bpy.data.node_groups.new(NODE_GROUP, "ShaderNodeTree")
    for name, socket in _GROUP_INPUTS:
        group.interface.new_socket(name=name, in_out="INPUT", socket_type=socket)
    group.interface.new_socket(name="Shader", in_out="OUTPUT", socket_type="NodeSocketShader")
    tree = group
    inp = tree.nodes.new("NodeGroupInput")
    out = tree.nodes.new("NodeGroupOutput")
    coords = tree.nodes.new("ShaderNodeTexCoord")
    split = tree.nodes.new("ShaderNodeSeparateXYZ")
    tree.links.new(coords.outputs["UV"], split.inputs[0])
    mask_x = _grid_axis(tree, split.outputs["X"], inp.outputs["Grid X"])
    mask_y = _grid_axis(tree, split.outputs["Y"], inp.outputs["Grid Y"])
    mask = _math(tree, "MULTIPLY", mask_x.outputs[0], mask_y.outputs[0])
    gap = _math(tree, "SUBTRACT", None, mask.outputs[0])
    gap.inputs[0].default_value = 1.0
    # Box-filter footprint: cells covered by one render pixel at this view distance.
    camera = tree.nodes.new("ShaderNodeCameraData")
    span = _math(tree, "MULTIPLY", camera.outputs["View Distance"], inp.outputs["Pixel Angle"])
    footprint = _math(tree, "DIVIDE", span.outputs[0], inp.outputs["Cell Pitch"])
    fade = tree.nodes.new("ShaderNodeMapRange")
    fade.interpolation_type = "SMOOTHSTEP"
    fade.clamp = True
    tree.links.new(footprint.outputs[0], fade.inputs["Value"])
    fade.inputs["From Min"].default_value = FADE_START
    fade.inputs["From Max"].default_value = FADE_END
    fade.inputs["To Min"].default_value = 1.0
    fade.inputs["To Max"].default_value = 0.0
    contrast = _math(tree, "MULTIPLY", fade.outputs["Result"], inp.outputs["Grid Strength"])
    darken = _math(tree, "MULTIPLY", contrast.outputs[0], gap.outputs[0])
    factor = _math(tree, "SUBTRACT", None, darken.outputs[0])
    factor.inputs[0].default_value = 1.0
    strength = _math(tree, "MULTIPLY", factor.outputs[0], inp.outputs["Strength"])
    emission = tree.nodes.new("ShaderNodeEmission")
    tree.links.new(inp.outputs["Color"], emission.inputs["Color"])
    tree.links.new(strength.outputs[0], emission.inputs["Strength"])
    tree.links.new(emission.outputs["Emission"], out.inputs["Shader"])
    return group


def _screen_image(bpy: Any, screen: dict[str, Any]) -> Any:
    """The screen capture image (sequence, still or movie), set up on ``texture``."""
    image = bpy.data.images.load(str(screen["paths"][0]), check_existing=True)
    # UI captures are display-referred sRGB: read them as such so the emissive screen
    # keeps the product's gamma under the scene view transform.
    image.colorspace_settings.name = "sRGB"
    if "movie" in screen:
        image.source = "MOVIE"
    else:
        image.source = "SEQUENCE" if len(screen["paths"]) > 1 else "FILE"
    return image


def _image_user(texture: Any, screen: dict[str, Any]) -> None:
    user = texture.image_user
    if "movie" in screen:
        user.frame_duration = int(texture.image.frame_duration or 1)
        user.frame_offset = 0
    else:
        user.frame_duration = len(screen["paths"])
        user.frame_offset = screen["frame_offset"]
    user.frame_start = 1
    user.use_auto_refresh = True


def _screen_material(bpy: Any, p: dict[str, Any], screen_w: float) -> Any:
    screen = p["screen"]
    mat = bpy.data.materials.new(f"screen.{p['name']}")
    mat.use_nodes = True
    tree = mat.node_tree
    tree.nodes.clear()
    output = tree.nodes.new("ShaderNodeOutputMaterial")
    texture = tree.nodes.new("ShaderNodeTexImage")
    texture.image = _screen_image(bpy, screen)
    _image_user(texture, screen)
    texture.interpolation = "Closest"
    group = tree.nodes.new("ShaderNodeGroup")
    group.node_tree = screen_node_group(bpy)
    grid_x, grid_y, grid_strength = p["grid"]
    values = {
        "Strength": p["strength"],
        "Grid X": float(grid_x),
        "Grid Y": float(grid_y),
        "Grid Strength": grid_strength,
        "Cell Pitch": screen_w / grid_x,
        "Pixel Angle": PIXEL_ANGLE,
    }
    for name, value in values.items():
        group.inputs[name].default_value = value
    tree.links.new(texture.outputs["Color"], group.inputs["Color"])
    tree.links.new(group.outputs["Shader"], output.inputs["Surface"])
    return mat


def device_add(bpy: Any, args: dict[str, Any]) -> Report:
    p = _parse(args)
    if "model" in p:
        return device_model(bpy, p)
    name, kind = p["name"], p["kind"]
    bw, bd, bh = _DIMS[kind]["body"]
    sw, sh = _DIMS[kind]["screen"]
    front = -bd / 2
    body = _mesh_object(bpy, f"{name}.body", box(bw, bd, bh), None, p["location"])
    body.rotation_euler = p["rotation"]
    _bevel(body, p["bevel"])
    parts = [body]
    bezel = _mesh_object(
        bpy, f"{name}.bezel", box(bw - 0.002, 0.0005, bh - 0.002), body, (0.0, front, 0.0)
    )
    parts.append(bezel)
    screen = _mesh_object(bpy, f"{name}.screen", quad(sw, sh), body, (0.0, front - 0.0004, 0.0))
    uv = screen.data.uv_layers.new(name="UVMap")
    for i, coord in enumerate(((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))):
        uv.data[i].uv = coord
    parts.append(screen)
    glass = _mesh_object(
        bpy, f"{name}.glass", quad(bw - 0.002, bh - 0.002), body, (0.0, front - 0.0008, 0.0)
    )
    parts.append(glass)
    if kind == "laptop":
        base = _mesh_object(
            bpy,
            f"{name}.base",
            box(*_LAPTOP_BASE),
            body,
            (0.0, -_LAPTOP_BASE[1] / 2, -bh / 2 - _LAPTOP_BASE[2] / 2),
        )
        _bevel(base, p["bevel"])
        _assign(base, _principled(bpy, f"base.{name}", FINISHES[p["finish"]]))
        parts.append(base)
        deck_top = -bh / 2  # the base's top face, in body space
        recess = _principled(
            bpy, f"deck.{name}", {"Base Color": (0.03, 0.03, 0.035, 1.0), "Roughness": 0.6}
        )
        for part, (dims, y) in (("keyboard", _KEYBOARD), ("trackpad", _TRACKPAD)):
            obj = _mesh_object(
                bpy, f"{name}.{part}", box(*dims), body, (0.0, y, deck_top + dims[2] / 2)
            )
            _assign(obj, recess)
            parts.append(obj)
    _assign(body, _principled(bpy, f"body.{name}", FINISHES[p["finish"]]))
    _assign(
        bezel,
        _principled(
            bpy, f"bezel.{name}", {"Base Color": (0.01, 0.01, 0.01, 1.0), "Roughness": 0.5}
        ),
    )
    _assign(
        glass,
        _principled(
            bpy,
            f"glass.{name}",
            GLASS,
        ),
    )
    mat = _screen_material(bpy, p, sw)
    _assign(screen, mat)
    report: dict[str, Any] = {
        "device": f"{name}.body",
        "kind": kind,
        "objects": [str(o.name) for o in parts],
        "screen_object": str(screen.name),
        "screen_material": str(mat.name),
        "node_group": NODE_GROUP,
        "emission_strength": p["strength"],
        "bevel": p["bevel"],
        "finish": p["finish"],
    }
    if "movie" in p["screen"]:
        report["movie"] = p["screen"]["movie"][0]
    else:
        report["frames"] = len(p["screen"]["frames"])
        report["frame_offset"] = p["screen"]["frame_offset"]
    return report


# --- kit models (#362) ---------------------------------------------------------------------

MODEL_SUFFIXES = (".glb",)
SCREEN_UV = "SC_ScreenUV"
SCREEN_IOR = 1.5
COAT_ROUGHNESS = 0.04
FRONT = (0.0, -1.0, 0.0)
# Principled overrides on a kit model's body material (the control plane's device kit
# keeps the same values in ``MACBOOK_FINISHES``).
MODEL_FINISHES: dict[str, dict[str, Any]] = {
    "space-black": {"Base Color": (0.028, 0.029, 0.032, 1.0), "Metallic": 1.0, "Roughness": 0.34},
    "silver": {"Base Color": (0.80, 0.80, 0.81, 1.0), "Metallic": 1.0, "Roughness": 0.26},
}
Vec = tuple[float, float, float]


def _parse_model(args: dict[str, Any]) -> dict[str, Any]:
    """``device.add`` with ``model``: a pinned glb dressed with the screen capture."""
    for key in ("grid", "bevel"):
        if key in args:
            raise ToolError(f"{TOOL}: {key} applies to the procedural rig only, not a model")
    kind = args.get("kind")
    if not isinstance(kind, str) or kind not in _DIMS:
        raise ToolError(f"{TOOL}: kind must be one of {sorted(_DIMS)}")
    if "screen" not in args:
        raise ToolError(f"{TOOL}: screen is required")
    model = args["model"]
    if not isinstance(model, dict):
        raise ToolError(f"{TOOL}: model must be {{path, sha256, screen, real_width}}")
    check_only(model, {"path", "sha256", "screen", "real_width", "body_material"}, f"{TOOL} model")
    path, sha = _pinned({"path": model.get("path"), "sha256": model.get("sha256")}, "model")
    select = model.get("screen")
    if not isinstance(select, dict):
        raise ToolError(f"{TOOL}: model.screen must be {{node, material}}")
    check_only(select, {"node", "material"}, f"{TOOL} model.screen")
    node = select.get("node")
    material = select.get("material")
    if not isinstance(node, str) or not node or not isinstance(material, str) or not material:
        raise ToolError(f"{TOOL}: model.screen node and material must be names")
    body = model.get("body_material")
    if body is not None and (not isinstance(body, str) or not body):
        raise ToolError(f"{TOOL}: model.body_material must be a material name")
    rotation = as_vec3(args.get("rotation", [0, 0, 0]), "rotation")
    if any(rotation):
        raise ToolError(f"{TOOL}: a model is placed upright; rotation must be [0, 0, 0]")
    finish = args.get("finish")
    p: dict[str, Any] = {
        "kind": kind,
        "name": as_name(args.get("name", kind.capitalize()), "name"),
        "location": as_vec3(args.get("location", [0, 0, 0]), "location"),
        "strength": as_num(args.get("emission_strength", 1.0), "emission_strength", 0, 100),
        "screen": _screen(args["screen"]),
        "finish": None
        if finish is None
        else as_choice(finish, "finish", {k: k for k in MODEL_FINISHES}),
        "model": {
            "path": path,
            "sha256": sha,
            "node": node,
            "material": material,
            "body_material": body,
            "real_width": as_num(model.get("real_width"), "model.real_width", 0.001, 10),
        },
    }
    if len(p["name"]) > 48:
        raise ToolError(f"{TOOL}: name must be at most 48 characters")
    screen = p["screen"]
    if "movie" in screen:
        movie_path, movie_sha = screen["movie"]
        screen["paths"] = [_resolve(movie_path, movie_sha, MOVIE_SUFFIXES)]
    else:
        screen["paths"] = [_resolve(path, sha, IMAGE_SUFFIXES) for path, sha in screen["frames"]]
    p["model"]["full"] = _resolve(path, sha, MODEL_SUFFIXES)
    return p


# Pure geometry (no bpy): world points, normalisation, the screen frame and its UVs.


def world_point(matrix: Any, co: Any) -> Vec:
    """``matrix @ co`` for a 4x4 row-indexable matrix and a 3-vector."""
    x, y, z = (float(co[0]), float(co[1]), float(co[2]))
    out = [matrix[i][0] * x + matrix[i][1] * y + matrix[i][2] * z + matrix[i][3] for i in range(3)]
    return (float(out[0]), float(out[1]), float(out[2]))


def world_direction(matrix: Any, d: Any) -> Vec:
    out = [sum(float(matrix[i][j]) * float(d[j]) for j in range(3)) for i in range(3)]
    return (out[0], out[1], out[2])


def _dot(a: Vec, b: Vec) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a: Vec, b: Vec) -> Vec:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _unit(v: Vec) -> Vec:
    n = _dot(v, v) ** 0.5
    if n < 1e-12:
        raise ToolError(f"{TOOL}: the model's screen faces are degenerate")
    return (v[0] / n, v[1] / n, v[2] / n)


def normalise(points: list[Vec], real_width: float) -> tuple[float, float]:
    """``(scale, lift)``: scale the world-bounds width to ``real_width``, then lift the
    lowest point to z = 0 (a point ``p`` lands at ``p * scale + (0, 0, lift)``)."""
    xs = [p[0] for p in points]
    width = max(xs) - min(xs)
    if width <= 0:
        raise ToolError(f"{TOOL}: the model has zero width")
    scale = real_width / width
    return scale, -min(p[2] for p in points) * scale


def screen_axes(points: list[Vec], normal: Vec) -> dict[str, Any]:
    """The screen's frame from its face vertices and mean face normal (world space).

    The normal is turned to face the front (-Y); up is world +Z projected onto the
    screen plane; right is ``up x normal`` (the viewer's right). Returns centre, right,
    up, normal, width and height.
    """
    n = _unit(normal)
    if _dot(n, FRONT) < 0:
        n = (-n[0], -n[1], -n[2])
    world_up = (0.0, 0.0, 1.0) if abs(n[2]) < 0.99 else (0.0, 1.0, 0.0)
    k = _dot(world_up, n)
    up = _unit((world_up[0] - k * n[0], world_up[1] - k * n[1], world_up[2] - k * n[2]))
    right = _cross(up, n)
    rs = [_dot(p, right) for p in points]
    us = [_dot(p, up) for p in points]
    rc, uc, nc = (min(rs) + max(rs)) / 2, (min(us) + max(us)) / 2, _dot(points[0], n)
    centre = tuple(right[i] * rc + up[i] * uc + n[i] * nc for i in range(3))
    return {
        "centre": centre,
        "right": right,
        "up": up,
        "normal": n,
        "width": max(rs) - min(rs),
        "height": max(us) - min(us),
    }


def planar_uv(point: Vec, frame: dict[str, Any]) -> tuple[float, float]:
    """The point's screen UV: (0, 0) bottom-left to (1, 1) top-right seen from the
    front, so the capture is upright and unmirrored."""
    c = frame["centre"]
    rel = (point[0] - c[0], point[1] - c[1], point[2] - c[2])
    return (
        _dot(rel, frame["right"]) / frame["width"] + 0.5,
        _dot(rel, frame["up"]) / frame["height"] + 0.5,
    )


def cover_crop(
    screen_w: float, screen_h: float, image_w: float, image_h: float
) -> tuple[float, float, float, float]:
    """``(scale_u, scale_v, offset_u, offset_v)`` so ``uv * scale + offset`` covers the
    screen with the image: scaled to fill, centred across, anchored at the top and
    cropped at the bottom (or the sides); no letterbox."""
    if min(screen_w, screen_h, image_w, image_h) <= 0:
        return 1.0, 1.0, 0.0, 0.0
    screen_aspect, image_aspect = screen_w / screen_h, image_w / image_h
    if image_aspect >= screen_aspect:  # wider image: crop both sides equally
        f = screen_aspect / image_aspect
        return f, 1.0, (1.0 - f) / 2, 0.0
    f = image_aspect / screen_aspect  # taller image: keep the top, crop the bottom
    return 1.0, f, 0.0, 1.0 - f


# bpy side.


def _named(objs: list[Any], name: str) -> Any:
    exact = [o for o in objs if o.name == name]
    near = [o for o in objs if str(o.name).startswith(f"{name}.")]
    return (exact or near or [None])[0]


def _slot(obj: Any, material: str) -> int:
    for i, slot in enumerate(obj.material_slots):
        mat = slot.material
        if mat is not None and (mat.name == material or str(mat.name).startswith(f"{material}.")):
            return i
    raise ToolError(f"{TOOL}: model node {obj.name!r} has no material {material!r}")


def _apply_finish(bpy: Any, objs: list[Any], material: str, values: dict[str, Any]) -> None:
    mats = [s.material for o in objs for s in o.material_slots if s.material is not None]
    mat = _named(mats, material)
    if mat is None or not mat.use_nodes:
        raise ToolError(f"{TOOL}: the model has no body material {material!r}")
    tree = mat.node_tree
    for node in tree.nodes:
        if node.type != "BSDF_PRINCIPLED":
            continue
        for link in list(tree.links):
            if link.to_node == node and link.to_socket.name in values:
                tree.links.remove(link)
        for socket, value in values.items():
            if socket in node.inputs:
                node.inputs[socket].default_value = value


def _model_screen_material(bpy: Any, p: dict[str, Any], frame: dict[str, Any]) -> Any:
    """Emission from the sRGB capture (cover-cropped from the top) mixed by fresnel
    with a glossy glass coat."""
    screen = p["screen"]
    mat = bpy.data.materials.new(f"screen.{p['name']}")
    mat.use_nodes = True
    tree = mat.node_tree
    tree.nodes.clear()
    output = tree.nodes.new("ShaderNodeOutputMaterial")
    uvmap = tree.nodes.new("ShaderNodeUVMap")
    uvmap.uv_map = SCREEN_UV
    mapping = tree.nodes.new("ShaderNodeMapping")
    mapping.vector_type = "POINT"
    texture = tree.nodes.new("ShaderNodeTexImage")
    texture.image = _screen_image(bpy, screen)
    _image_user(texture, screen)
    texture.interpolation = "Linear"
    texture.extension = "EXTEND"
    try:
        image_w, image_h = float(texture.image.size[0]), float(texture.image.size[1])
    except (TypeError, ValueError, IndexError):
        image_w = image_h = 0.0
    su, sv, ou, ov = cover_crop(frame["width"], frame["height"], image_w, image_h)
    mapping.inputs["Scale"].default_value = (su, sv, 1.0)
    mapping.inputs["Location"].default_value = (ou, ov, 0.0)
    emission = tree.nodes.new("ShaderNodeEmission")
    emission.inputs["Strength"].default_value = p["strength"]
    glossy = tree.nodes.new("ShaderNodeBsdfGlossy")
    glossy.inputs["Roughness"].default_value = COAT_ROUGHNESS
    fresnel = tree.nodes.new("ShaderNodeFresnel")
    fresnel.inputs["IOR"].default_value = SCREEN_IOR
    mix = tree.nodes.new("ShaderNodeMixShader")
    tree.links.new(uvmap.outputs["UV"], mapping.inputs["Vector"])
    tree.links.new(mapping.outputs["Vector"], texture.inputs["Vector"])
    tree.links.new(texture.outputs["Color"], emission.inputs["Color"])
    tree.links.new(fresnel.outputs["Fac"], mix.inputs[0])
    tree.links.new(emission.outputs["Emission"], mix.inputs[1])
    tree.links.new(glossy.outputs["BSDF"], mix.inputs[2])
    tree.links.new(mix.outputs["Shader"], output.inputs["Surface"])
    return mat


def _r(v: Any) -> list[float]:
    return [round(float(c), 5) for c in v]


def device_model(bpy: Any, p: dict[str, Any]) -> Report:
    """Import the pinned glb, normalise it, map the capture onto its display faces."""
    m = p["model"]
    before = {o.name for o in bpy.data.objects}
    actions = {a.name for a in bpy.data.actions}
    bpy.ops.import_scene.gltf(filepath=str(m["full"]))
    new = [o for o in bpy.data.objects if o.name not in before]
    if not new:
        raise ToolError(f"{TOOL}: {m['path']!r} imported no objects")
    for obj in new:
        obj.animation_data_clear()
    for action in list(bpy.data.actions):
        if action.name not in actions:
            bpy.data.actions.remove(action)
    bpy.context.view_layer.update()
    meshes = [o for o in new if o.type == "MESH"]
    points = [world_point(o.matrix_world, c) for o in meshes for c in o.bound_box]
    if not points:
        raise ToolError(f"{TOOL}: {m['path']!r} has no meshes")
    scale, lift = normalise(points, m["real_width"])
    screen_obj = _named(meshes, m["node"])
    if screen_obj is None:
        raise ToolError(f"{TOOL}: the model has no mesh node {m['node']!r}")
    index = _slot(screen_obj, m["material"])
    mesh = screen_obj.data
    polys = [poly for poly in mesh.polygons if poly.material_index == index]
    if not polys:
        raise ToolError(f"{TOOL}: no faces of {m['node']!r} use {m['material']!r}")
    mw = screen_obj.matrix_world
    loops = [li for poly in polys for li in poly.loop_indices]
    world = {li: world_point(mw, mesh.vertices[mesh.loops[li].vertex_index].co) for li in loops}
    normals = [world_direction(mw, poly.normal) for poly in polys]
    mean = tuple(sum(n[i] for n in normals) for i in range(3))
    frame = screen_axes(list(world.values()), (mean[0], mean[1], mean[2]))
    uv = mesh.uv_layers.new(name=SCREEN_UV)
    for li, point in world.items():
        uv.data[li].uv = planar_uv(point, frame)
    # Normalise: scale every root about the world origin, then lift onto the floor.
    names = {o.name for o in new}
    roots = [o for o in new if o.parent is None or o.parent.name not in names]
    ox, oy, oz = p["location"]
    offset = (ox, oy, oz + lift)
    for root in roots:
        root.scale = tuple(float(c) * scale for c in root.scale)
        root.location = tuple(
            float(c) * scale + o for c, o in zip(root.location, offset, strict=True)
        )
    if p["finish"] is not None and m["body_material"] is not None:
        _apply_finish(bpy, new, m["body_material"], MODEL_FINISHES[p["finish"]])
    mat = _model_screen_material(bpy, p, frame)
    screen_obj.material_slots[index].material = mat

    def place(v: Vec) -> list[float]:
        return _r(v[i] * scale + offset[i] for i in range(3))

    c, right, up = frame["centre"], frame["right"], frame["up"]
    hw, hh = frame["width"] / 2, frame["height"] / 2
    corners = [
        place(tuple(c[i] + sr * hw * right[i] + su * hh * up[i] for i in range(3)))  # type: ignore[arg-type]
        for sr, su in ((-1, 1), (1, 1), (1, -1), (-1, -1))
    ]
    report: dict[str, Any] = {
        "device": str(roots[0].name),
        "kind": p["kind"],
        "model": m["path"],
        "objects": sorted(str(o.name) for o in new),
        "screen_object": str(screen_obj.name),
        "screen_material": str(mat.name),
        "emission_strength": p["strength"],
        "finish": p["finish"],
        "scale": round(scale, 6),
        "screen": {
            "centre": place(c),
            "normal": _r(frame["normal"]),
            "up": _r(up),
            "width": round(frame["width"] * scale, 5),
            "height": round(frame["height"] * scale, 5),
            "corners": corners,
        },
    }
    if "movie" in p["screen"]:
        report["movie"] = p["screen"]["movie"][0]
    else:
        report["frames"] = len(p["screen"]["frames"])
        report["frame_offset"] = p["screen"]["frame_offset"]
    return report
