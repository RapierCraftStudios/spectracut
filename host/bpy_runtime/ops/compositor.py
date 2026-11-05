# SPDX-License-Identifier: AGPL-3.0-or-later
"""``comp.apply_graph``: run a shot's declarative compositing graph on its multilayer EXRs.

The compositor tree has no Render Layers node, so Blender does not re-render
the 3D scene. An Image node plays the staged multilayer EXR sequence and a
Cryptomatte node (source IMAGE, the same image) keyed on ``screen_matte``
gives the device-screen matte. Then each op runs in order:

* screen ops are mixed in only inside the matte: ``screen_grade`` (Color
  Balance offset/power/slope + Hue/Saturation), ``ui_sharpen`` (Filter
  SHARPEN), ``screen_glow`` (Glare FOG_GLOW on the matte-isolated screen,
  added back);
* scene ops are mixed in only outside it (inverted matte): ``glints`` (Glare
  STREAKS), ``light_wrap`` (blurred plate screened over the matte edge),
  ``lens`` (Lens Distortion with dispersion);

and a Composite node writes the result. Arguments are checked in full before
bpy is touched; frames are read only from the confined assets dir. The op
allowlist and bounds mirror ``spectracut.finishing.comp`` (no import of it:
the runtime is stdlib-only).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from bpy_runtime.ops import Report, ToolError, as_int, as_name, as_num, check_only
from bpy_runtime.ops.scene import assets_root, resolve_asset

TOOL = "comp.apply_graph"
ARGS = {"comp_id", "comp_version", "screen_matte", "ops", "frames"}
EXR_SUFFIXES = (".exr",)
MAX_FRAMES = 100000
MAX_OPS = 16
SCREEN_OPS = ("screen_grade", "ui_sharpen", "screen_glow")
SCENE_OPS = ("glints", "light_wrap", "lens")


def _num(params: dict[str, Any], name: str, lo: float, hi: float, default: float) -> float:
    return as_num(params.get(name, default), f"{TOOL} {params['op']}.{name}", lo, hi)


def _rgb(params: dict[str, Any], name: str, lo: float, hi: float, default: float) -> list[float]:
    value = params.get(name, [default] * 3)
    if not isinstance(value, list) or len(value) != 3:
        raise ToolError(f"{TOOL}: {params['op']}.{name} must be [r, g, b]")
    return [as_num(v, f"{TOOL} {params['op']}.{name}", lo, hi) for v in value]


def _screen_grade(p: dict[str, Any]) -> dict[str, Any]:
    check_only(p, {"op", "slope", "offset", "power", "saturation"}, TOOL)
    return {
        "slope": _rgb(p, "slope", 0.5, 2.0, 1.0),
        "offset": _rgb(p, "offset", -0.2, 0.2, 0.0),
        "power": _rgb(p, "power", 0.5, 2.0, 1.0),
        "saturation": _num(p, "saturation", 0.0, 2.0, 1.0),
    }


def _ui_sharpen(p: dict[str, Any]) -> dict[str, Any]:
    check_only(p, {"op", "amount", "radius"}, TOOL)
    return {"amount": _num(p, "amount", 0.0, 1.0, 0.0), "radius": _num(p, "radius", 0, 3, 1)}


def _screen_glow(p: dict[str, Any]) -> dict[str, Any]:
    check_only(p, {"op", "strength", "size", "threshold"}, TOOL)
    return {
        "strength": _num(p, "strength", 0.0, 0.5, 0.0),
        "size": as_int(p.get("size", 6), f"{TOOL} screen_glow.size", 1, 9),
        "threshold": _num(p, "threshold", 0.0, 2.0, 0.9),
    }


def _glints(p: dict[str, Any]) -> dict[str, Any]:
    check_only(p, {"op", "threshold", "strength", "streaks"}, TOOL)
    return {
        "threshold": _num(p, "threshold", 0.0, 10.0, 1.0),
        "strength": _num(p, "strength", 0.0, 1.0, 0.0),
        "streaks": as_int(p.get("streaks", 4), f"{TOOL} glints.streaks", 2, 8),
    }


def _light_wrap(p: dict[str, Any]) -> dict[str, Any]:
    check_only(p, {"op", "amount", "blur"}, TOOL)
    return {"amount": _num(p, "amount", 0.0, 0.5, 0.0), "blur": _num(p, "blur", 0, 64, 8)}


def _lens(p: dict[str, Any]) -> dict[str, Any]:
    check_only(p, {"op", "distortion", "dispersion"}, TOOL)
    return {
        "distortion": _num(p, "distortion", -0.1, 0.1, 0.0),
        "dispersion": _num(p, "dispersion", 0.0, 0.05, 0.0),
    }


OPS = {
    "screen_grade": _screen_grade,
    "ui_sharpen": _ui_sharpen,
    "screen_glow": _screen_glow,
    "glints": _glints,
    "light_wrap": _light_wrap,
    "lens": _lens,
}


def _ops(value: Any) -> list[tuple[str, dict[str, Any]]]:
    if not isinstance(value, list) or len(value) > MAX_OPS:
        raise ToolError(f"{TOOL}: ops must be a list of at most {MAX_OPS} ops")
    out: list[tuple[str, dict[str, Any]]] = []
    for params in value:
        if not isinstance(params, dict) or params.get("op") not in OPS:
            raise ToolError(f"{TOOL}: every op must be an object with op among {sorted(OPS)}")
        name = params["op"]
        if any(name == seen for seen, _ in out):
            raise ToolError(f"{TOOL}: op {name!r} appears twice")
        out.append((name, OPS[name](params)))
    return out


def _frames(value: Any) -> list[Path]:
    if not isinstance(value, list) or not 0 < len(value) <= MAX_FRAMES:
        raise ToolError(f"{TOOL}: frames must be a non-empty list of relative paths")
    root = assets_root()
    return [resolve_asset(v, root, EXR_SUFFIXES) for v in value]


# ---- the node tree ---------------------------------------------------------------------


def _put(node: Any, name: str, value: Any) -> None:
    """Set input socket ``name`` when the node has one, else the node property of that name."""
    sockets = node.inputs
    if name in sockets:
        sockets[name].default_value = value
    else:
        setattr(node, name.lower().replace(" ", "_"), value)


class _Tree:
    """Adds nodes with a role label, keeps the running image tail and the screen mattes."""

    def __init__(self, tree: Any) -> None:
        self.tree = tree
        self.roles: list[str] = []
        self.tail: Any = None
        self.matte: Any = None
        self.outside: Any = None

    def node(self, kind: str, role: str) -> Any:
        node = self.tree.nodes.new(kind)
        node.label = role
        self.roles.append(role)
        return node

    def link(self, out: Any, node: Any, socket: int | str) -> None:
        self.tree.links.new(out, node.inputs[socket])

    def mix(self, blend: str, role: str, fac: Any, a: Any, b: Any) -> Any:
        """A MixRGB ``blend`` of sockets ``a`` and ``b``; ``fac`` is a number or a socket."""
        mix = self.node("CompositorNodeMixRGB", role)
        mix.blend_type = blend
        if isinstance(fac, int | float):
            mix.inputs[0].default_value = fac
        else:
            self.link(fac, mix, 0)
        self.link(a, mix, 1)
        self.link(b, mix, 2)
        return mix

    def masked(self, effect: Any, role: str, screen: bool) -> None:
        """Keep ``effect`` only inside the screen matte (or only outside it)."""
        mask = self.matte if screen else self.outside
        self.tail = self.mix("MIX", role, mask, self.tail.outputs[0], effect.outputs[0])


def _screen_grade_nodes(t: _Tree, p: dict[str, Any]) -> None:
    balance = t.node("CompositorNodeColorBalance", "screen_grade_cdl")
    balance.correction_method = "OFFSET_POWER_SLOPE"
    balance.slope, balance.offset, balance.power = p["slope"], p["offset"], p["power"]
    balance.offset_basis = 0.0
    t.link(t.tail.outputs[0], balance, 1)
    sat = t.node("CompositorNodeHueSat", "screen_grade_saturation")
    _put(sat, "Saturation", p["saturation"])
    t.link(balance.outputs[0], sat, "Image")
    t.masked(sat, "screen_grade", screen=True)


def _ui_sharpen_nodes(t: _Tree, p: dict[str, Any]) -> None:
    sharpen = t.node("CompositorNodeFilter", "ui_sharpen_filter")
    sharpen.filter_type = "SHARPEN"
    sharpen.inputs[0].default_value = p["amount"]
    t.link(t.tail.outputs[0], sharpen, 1)
    t.masked(sharpen, "ui_sharpen", screen=True)


def _screen_glow_nodes(t: _Tree, p: dict[str, Any]) -> None:
    base = t.tail.outputs[0]
    iso = t.mix("MULTIPLY", "screen_glow_isolate", 1.0, base, t.matte)
    glare = t.node("CompositorNodeGlare", "screen_glow_glare")
    glare.glare_type = "FOG_GLOW"
    glare.size = p["size"]
    glare.threshold = p["threshold"]
    glare.mix = 1.0
    t.link(iso.outputs[0], glare, "Image")
    t.tail = t.mix("ADD", "screen_glow", p["strength"], base, glare.outputs[0])


def _glints_nodes(t: _Tree, p: dict[str, Any]) -> None:
    glare = t.node("CompositorNodeGlare", "glints_glare")
    glare.glare_type = "STREAKS"
    glare.streaks = p["streaks"]
    glare.threshold = p["threshold"]
    _put(glare, "Strength", p["strength"])
    t.link(t.tail.outputs[0], glare, "Image")
    t.masked(glare, "glints", screen=False)


def _light_wrap_nodes(t: _Tree, p: dict[str, Any]) -> None:
    base = t.tail.outputs[0]
    plate = t.node("CompositorNodeBlur", "light_wrap_blur")
    plate.size_x = plate.size_y = int(p["blur"])
    t.link(base, plate, "Image")
    soft = t.node("CompositorNodeBlur", "light_wrap_edge_blur")
    soft.size_x = soft.size_y = int(p["blur"])
    t.link(t.matte, soft, "Image")
    edge = t.mix("DIFFERENCE", "light_wrap_edge", 1.0, soft.outputs[0], t.matte)
    wrap = t.mix("SCREEN", "light_wrap_screen", p["amount"], base, plate.outputs[0])
    on_edge = t.mix("MIX", "light_wrap_on_edge", edge.outputs[0], base, wrap.outputs[0])
    t.masked(on_edge, "light_wrap", screen=False)


def _lens_nodes(t: _Tree, p: dict[str, Any]) -> None:
    dist = t.node("CompositorNodeLensdist", "lens_distortion")
    _put(dist, "Distortion", p["distortion"])
    _put(dist, "Dispersion", p["dispersion"])
    t.link(t.tail.outputs[0], dist, "Image")
    t.masked(dist, "lens", screen=False)


BUILDERS = {
    "screen_grade": _screen_grade_nodes,
    "ui_sharpen": _ui_sharpen_nodes,
    "screen_glow": _screen_glow_nodes,
    "glints": _glints_nodes,
    "light_wrap": _light_wrap_nodes,
    "lens": _lens_nodes,
}


def _build(
    bpy: Any, frames: list[Path], screen_matte: str, ops: list[tuple[str, dict[str, Any]]]
) -> list[str]:
    scene = bpy.context.scene
    scene.use_nodes = True
    scene.render.use_compositing = True
    tree = scene.node_tree
    tree.nodes.clear()
    t = _Tree(tree)

    image = bpy.data.images.load(str(frames[0]), check_existing=False)
    image.source = "SEQUENCE" if len(frames) > 1 else "FILE"
    src = t.node("CompositorNodeImage", "image")
    src.image = image
    src.frame_start = 1
    src.frame_offset = 0
    src.frame_duration = len(frames)
    t.tail = src

    crypto = t.node("CompositorNodeCryptomatteV2", "screen_matte")
    crypto.source = "IMAGE"
    crypto.image = image
    crypto.matte_id = screen_matte
    t.link(src.outputs[0], crypto, "Image")
    t.matte = crypto.outputs["Matte"]
    invert = t.node("CompositorNodeInvert", "scene_matte")
    t.link(t.matte, invert, "Color")
    t.outside = invert.outputs[0]

    for name, params in ops:
        BUILDERS[name](t, params)

    out = t.node("CompositorNodeComposite", "composite")
    t.link(t.tail.outputs[0], out, "Image")
    return t.roles


def comp_apply_graph(bpy: Any, args: dict[str, Any]) -> Report:
    check_only(args, ARGS, TOOL)
    comp_id = as_name(args.get("comp_id"), f"{TOOL} comp_id")
    version = as_int(args.get("comp_version", 1), f"{TOOL} comp_version", 1, 1_000_000)
    screen_matte = as_name(args.get("screen_matte", "device_screen"), f"{TOOL} screen_matte")
    ops = _ops(args.get("ops"))
    frames = _frames(args.get("frames"))
    nodes = _build(bpy, frames, screen_matte, ops)
    return {
        "comp_id": comp_id,
        "comp_version": version,
        "nodes": nodes,
        "screen_matte": screen_matte,
    }
