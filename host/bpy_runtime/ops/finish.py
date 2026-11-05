# SPDX-License-Identifier: AGPL-3.0-or-later
"""``finish.apply_look``: grade and finish a render's linear EXR frames in the compositor.

The compositor tree has no Render Layers node, so Blender does not re-render
the 3D scene: an Image node plays the staged EXR sequence (scene-linear
Rec.709) through

    Color Balance (ASC CDL, offset/power/slope) -> Hue/Saturation (CDL sat)
    -> Lens Distortion (distortion, dispersion = chromatic aberration)
    -> Glare BLOOM (bloom > 0) -> Glare FOG_GLOW tinted red (halation)
    -> ellipse-mask vignette multiply -> seeded noise grain overlay -> Composite

and the scene's view transform and display (set by earlier steps) encode the
8-bit master. A LUT grade (``.cube``, display-referred Rec.709) cannot be a
compositor node, so it is applied after the view transform to the written
masters with Blender's bundled numpy (:func:`apply_pending_lut`, called by the
runner). Frames and the LUT are read only from the confined assets dir.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from bpy_runtime.ops import Report, ToolError, as_int, as_num, check_only
from bpy_runtime.ops.scene import assets_root, resolve_asset

GRAIN_SEED = 1153
"""Fixed grain seed: the same Look on the same frames gives the same master."""
HALATION_TINT = (1.0, 0.25, 0.12, 1.0)
EXR_SUFFIXES = (".exr",)
LUT_SUFFIXES = (".cube",)
MAX_FRAMES = 100000
ARGS = {"look_id", "look_version", "grade", "lens", "bloom", "grain", "frames", "lut"}

_PENDING: dict[str, Any] = {"lut": None}


def _unit(value: Any, what: str) -> float:
    return as_num(value, what, 0.0, 1.0)


def _rgb(value: Any, what: str, lo: float, hi: float) -> tuple[float, float, float]:
    if not isinstance(value, list | tuple) or len(value) != 3:
        raise ToolError(f"finish.apply_look: {what} must be [r, g, b]")
    r, g, b = (as_num(v, what, lo, hi) for v in value)
    return (r, g, b)


def _cdl(grade: Any) -> dict[str, Any] | None:
    if not isinstance(grade, dict):
        raise ToolError("finish.apply_look: grade must be an object")
    check_only(grade, {"cdl", "lut"}, "finish.apply_look grade")
    cdl = grade.get("cdl")
    if cdl is None:
        return None
    if not isinstance(cdl, dict):
        raise ToolError("finish.apply_look: grade.cdl must be an object")
    check_only(cdl, {"slope", "offset", "power", "saturation"}, "finish.apply_look grade.cdl")
    return {
        "slope": _rgb(cdl.get("slope", [1, 1, 1]), "grade.cdl.slope", 0.0, 100.0),
        "offset": _rgb(cdl.get("offset", [0, 0, 0]), "grade.cdl.offset", -10.0, 10.0),
        "power": _rgb(cdl.get("power", [1, 1, 1]), "grade.cdl.power", 0.0, 100.0),
        "saturation": as_num(cdl.get("saturation", 1.0), "grade.cdl.saturation", 0.0, 100.0),
    }


def _lens(lens: Any) -> dict[str, float]:
    if not isinstance(lens, dict):
        raise ToolError("finish.apply_look: lens must be an object")
    names = {"chromatic_aberration", "vignette", "distortion", "halation"}
    check_only(lens, names, "finish.apply_look lens")
    out = {n: _unit(lens.get(n, 0.0), f"lens.{n}") for n in names - {"distortion"}}
    out["distortion"] = as_num(lens.get("distortion", 0.0), "lens.distortion", -1.0, 1.0)
    return out


def _frames(value: Any) -> list[Path]:
    if not isinstance(value, list) or not 0 < len(value) <= MAX_FRAMES:
        raise ToolError("finish.apply_look: frames must be a non-empty list of relative paths")
    root = assets_root()
    return [resolve_asset(v, root, EXR_SUFFIXES) for v in value]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _lut(value: Any) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ToolError("finish.apply_look: lut must be {path, sha256}")
    check_only(value, {"path", "sha256"}, "finish.apply_look lut")
    path = resolve_asset(value.get("path"), assets_root(), LUT_SUFFIXES)
    want = value.get("sha256")
    if not isinstance(want, str) or _sha256(path) != want:
        raise ToolError(
            f"LUT {value.get('path')!r} does not match its pinned sha256", "asset.hash_mismatch"
        )
    return path


def _put(node: Any, name: str, value: Any) -> None:
    """Set input socket ``name`` when the node has one, else the node property of that name."""
    sockets = node.inputs
    if name in sockets:
        sockets[name].default_value = value
    else:
        setattr(node, name.lower().replace(" ", "_"), value)


class _Chain:
    """Builds a linear node chain, recording each node's role for the report."""

    def __init__(self, tree: Any) -> None:
        self.tree = tree
        self.roles: list[str] = []
        self.tail: Any = None

    def node(self, kind: str, role: str) -> Any:
        node = self.tree.nodes.new(kind)
        node.label = role
        self.roles.append(role)
        return node

    def then(self, node: Any, socket: int | str = 0) -> Any:
        self.tree.links.new(self.tail.outputs[0], node.inputs[socket])
        self.tail = node
        return node


def _mix(chain: _Chain, blend: str, fac: float, role: str) -> Any:
    mix = chain.node("CompositorNodeMixRGB", role)
    mix.blend_type = blend
    mix.inputs[0].default_value = fac
    chain.then(mix, 1)
    return mix


def _build(
    bpy: Any,
    frames: list[Path],
    cdl: dict[str, Any] | None,
    lens: dict[str, float],
    bloom: float,
    grain: float,
) -> list[str]:
    scene = bpy.context.scene
    scene.use_nodes = True
    scene.render.use_compositing = True
    tree = scene.node_tree
    tree.nodes.clear()
    chain = _Chain(tree)

    image = bpy.data.images.load(str(frames[0]), check_existing=False)
    image.source = "SEQUENCE" if len(frames) > 1 else "FILE"
    src = chain.node("CompositorNodeImage", "image")
    src.image = image
    src.frame_start = 1
    src.frame_offset = 0
    src.frame_duration = len(frames)
    chain.tail = src

    if cdl is not None:
        balance = chain.node("CompositorNodeColorBalance", "cdl")
        balance.correction_method = "OFFSET_POWER_SLOPE"
        balance.slope = cdl["slope"]
        balance.offset = cdl["offset"]
        balance.power = cdl["power"]
        balance.offset_basis = 0.0
        chain.then(balance, 1)
        sat = chain.node("CompositorNodeHueSat", "saturation")
        _put(sat, "Saturation", cdl["saturation"])
        chain.then(sat, "Image")

    if lens["distortion"] or lens["chromatic_aberration"]:
        dist = chain.node("CompositorNodeLensdist", "lens")
        _put(dist, "Distortion", lens["distortion"])
        _put(dist, "Dispersion", lens["chromatic_aberration"])
        chain.then(dist, "Image")

    if bloom > 0:
        glow = chain.node("CompositorNodeGlare", "bloom")
        glow.glare_type = "BLOOM"
        _put(glow, "Strength", bloom)
        chain.then(glow, "Image")

    if lens["halation"] > 0:
        base = chain.tail
        fog = chain.node("CompositorNodeGlare", "halation_glow")
        fog.glare_type = "FOG_GLOW"
        chain.then(fog, "Image")
        tint = _mix(chain, "MULTIPLY", 1.0, "halation_tint")
        tint.inputs[2].default_value = HALATION_TINT
        add = chain.node("CompositorNodeMixRGB", "halation")
        add.blend_type = "ADD"
        add.inputs[0].default_value = lens["halation"]
        tree.links.new(base.outputs[0], add.inputs[1])
        tree.links.new(chain.tail.outputs[0], add.inputs[2])
        chain.tail = add

    if lens["vignette"] > 0:
        mask = chain.node("CompositorNodeEllipseMask", "vignette_mask")
        mask.width, mask.height = 0.95, 0.95
        soft = chain.node("CompositorNodeBlur", "vignette_soften")
        soft.use_relative = True
        soft.factor_x, soft.factor_y = 25.0, 25.0
        tree.links.new(mask.outputs[0], soft.inputs[0])
        vig = _mix(chain, "MULTIPLY", lens["vignette"], "vignette")
        tree.links.new(soft.outputs[0], vig.inputs[2])

    if grain > 0:
        texture = bpy.data.textures.new("spectracut_grain", type="CLOUDS")
        texture.noise_scale = 0.01
        texture.noise_basis = "BLENDER_ORIGINAL"
        noise = chain.node("CompositorNodeTexture", "grain_noise")
        noise.texture = texture
        _put(noise, "Offset", (GRAIN_SEED % 997 / 7.0, GRAIN_SEED % 991 / 11.0, 0.0))
        over = _mix(chain, "OVERLAY", grain, "grain")
        tree.links.new(noise.outputs[1], over.inputs[2])

    out = chain.node("CompositorNodeComposite", "composite")
    chain.then(out, "Image")
    return chain.roles


def finish_apply_look(bpy: Any, args: dict[str, Any]) -> Report:
    check_only(args, ARGS, "finish.apply_look")
    look_id = args.get("look_id")
    if not isinstance(look_id, str) or not look_id:
        raise ToolError("finish.apply_look: look_id must be a non-empty string")
    version = as_int(args.get("look_version", 1), "look_version", 1, 1_000_000)
    grade = args.get("grade", {})
    cdl = _cdl(grade)
    lens = _lens(args.get("lens", {}))
    bloom = _unit(args.get("bloom", 0.0), "bloom")
    grain = _unit(args.get("grain", 0.0), "grain")
    frames = _frames(args.get("frames"))
    lut = _lut(args.get("lut"))
    if isinstance(grade, dict) and grade.get("lut") is not None and lut is None:
        raise ToolError("finish.apply_look: a LUT grade needs lut {path, sha256}")
    nodes = _build(bpy, frames, cdl, lens, bloom, grain)
    _PENDING["lut"] = lut
    report: dict[str, Any] = {"look_id": look_id, "look_version": version, "nodes": nodes}
    if lut is not None:
        report["lut"] = args["lut"]["path"]
    return report


# ---- the LUT pass (display-referred, after the view transform) ----------------------


def _read_cube(path: Path, np: Any) -> tuple[int, Any, Any, Any]:
    size, dims = 0, 0
    lo, hi = [0.0, 0.0, 0.0], [1.0, 1.0, 1.0]
    rows: list[list[float]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        head, *rest = line.split()
        key = head.upper()
        if key in ("LUT_1D_SIZE", "LUT_3D_SIZE"):
            size, dims = int(rest[0]), 1 if key == "LUT_1D_SIZE" else 3
        elif key == "DOMAIN_MIN":
            lo = [float(v) for v in rest]
        elif key == "DOMAIN_MAX":
            hi = [float(v) for v in rest]
        elif key[0].isdigit() or key[0] in "+-.":
            rows.append([float(v) for v in line.split()])
        elif key != "TITLE":
            raise ToolError(f"LUT {path.name}: unknown keyword {head!r}")
    want = size if dims == 1 else size**3
    if size < 2 or len(rows) != want:
        raise ToolError(f"LUT {path.name}: declares {want} entries, has {len(rows)}")
    return dims, np.asarray(rows, dtype=np.float32), np.asarray(lo), np.asarray(hi)


def apply_cube_array(rgb: Any, path: Path, np: Any) -> Any:
    """Trilinear (3D) or per-channel linear (1D) ``.cube`` lookup on an ``(N, 3)`` array."""
    dims, table, lo, hi = _read_cube(path, np)
    n = int(round(len(table) ** (1 / 3))) if dims == 3 else len(table)
    x = np.clip((rgb - lo) / (hi - lo), 0.0, 1.0) * (n - 1)
    i0 = np.minimum(np.floor(x).astype(np.int64), n - 2)
    f = x - i0
    if dims == 1:
        out = np.empty_like(rgb)
        for ch in range(3):
            a, b = table[i0[:, ch], ch], table[i0[:, ch] + 1, ch]
            out[:, ch] = a + (b - a) * f[:, ch]
        return out
    r0, g0, b0 = i0[:, 0], i0[:, 1], i0[:, 2]
    fr, fg, fb = f[:, 0:1], f[:, 1:2], f[:, 2:3]

    def at(dr: int, dg: int, db: int) -> Any:
        return table[(r0 + dr) + n * (g0 + dg) + n * n * (b0 + db)]

    c00 = at(0, 0, 0) * (1 - fr) + at(1, 0, 0) * fr
    c10 = at(0, 1, 0) * (1 - fr) + at(1, 1, 0) * fr
    c01 = at(0, 0, 1) * (1 - fr) + at(1, 0, 1) * fr
    c11 = at(0, 1, 1) * (1 - fr) + at(1, 1, 1) * fr
    return (c00 * (1 - fg) + c10 * fg) * (1 - fb) + (c01 * (1 - fg) + c11 * fg) * fb


def apply_pending_lut(bpy: Any, frames: list[Path]) -> None:
    """Apply the LUT ``finish.apply_look`` pinned to each written display-referred master."""
    lut = _PENDING["lut"]
    _PENDING["lut"] = None
    if lut is None:
        return
    import numpy as np  # Blender's bundled numpy; render hosts only

    for frame in frames:
        image = bpy.data.images.load(str(frame), check_existing=False)
        pixels = np.empty(len(image.pixels), dtype=np.float32)
        image.pixels.foreach_get(pixels)
        channels = int(image.channels)
        if channels < 3:
            raise ToolError(f"LUT pass needs an RGB master, {frame.name} has {channels} channel(s)")
        px = pixels.reshape(-1, channels)
        px[:, :3] = apply_cube_array(px[:, :3], lut, np)
        image.pixels.foreach_set(px.reshape(-1))
        image.filepath_raw = str(frame)
        image.file_format = "PNG"
        image.save()
        bpy.data.images.remove(image)
