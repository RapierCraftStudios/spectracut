# SPDX-License-Identifier: AGPL-3.0-or-later
"""Material presets with overrides, light.add size/colour/spot angle, world gradient and haze.

These run the real ``bpy_runtime/runner.py`` against a fake ``bpy`` (the ``Run`` pattern of
``tests/host/test_bpy_ops.py``) whose materials, lights and world node trees are plain
objects, so the test can read back socket values and links.
"""

from __future__ import annotations

import json
import math
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from spectracut_host.guard import runtime_tool_names

ROOT = Path(__file__).resolve().parents[2]
HOST = ROOT / "host"
RUNTIME = HOST / "bpy_runtime"
RUNNER = RUNTIME / "runner.py"

FAKE_BPY = r"""
import atexit, json, os
from pathlib import Path
from unittest.mock import MagicMock

_bpy = MagicMock(name="bpy")
_bpy.app.version = (4, 5, 3)
_objects = []
_materials = []
_lights = []
_bpy_calls = []

PRINCIPLED = ["Base Color", "Metallic", "Roughness", "IOR", "Coat Weight", "Anisotropic",
              "Transmission Weight", "Emission Color", "Emission Strength"]
NODE_INPUTS = {
    "ShaderNodeBackground": ["Color", "Strength"],
    "ShaderNodeVolumeScatter": ["Color", "Density", "Anisotropy"],
    "ShaderNodeOutputWorld": ["Surface", "Volume"],
    "ShaderNodeSeparateXYZ": ["Vector"],
    "ShaderNodeValToRGB": ["Fac"],
    "ShaderNodeMapping": ["Vector", "Rotation"],
    "ShaderNodeTexEnvironment": ["Vector"],
}


class Socket:
    def __init__(self, node, name):
        self.node, self.name, self.default_value = node, name, None


class Sockets(dict):
    def __init__(self, node, names):
        super().__init__((n, Socket(node, n)) for n in names)

    def __getitem__(self, key):
        if key not in self:
            self.setdefault(key, Socket(self.node, key))
        return dict.__getitem__(self, key)


class Stop:
    def __init__(self):
        self.position, self.color = None, None


class Node:
    def __init__(self, bl_idname, name=None):
        self.bl_idname, self.name = bl_idname, name or bl_idname
        self.inputs = Sockets(self, NODE_INPUTS.get(bl_idname, PRINCIPLED if name else []))
        self.inputs.node = self
        self.outputs = Sockets(self, [])
        self.outputs.node = self
        self.color_ramp = MagicMock()
        self.color_ramp.elements = [Stop(), Stop()]
        self.image = None


class Nodes(list):
    def new(self, bl_idname):
        node = Node(bl_idname)
        self.append(node)
        return node

    def get(self, name):
        return next((n for n in self if n.name == name), None)


class Links(list):
    def new(self, out, inp):
        self[:] = [l for l in self if l[1] is not inp]
        self.append((out, inp))


class Tree:
    def __init__(self):
        self.nodes, self.links = Nodes(), Links()


class Material:
    def __init__(self, name):
        self.name, self.use_nodes, self.diffuse_color = name, False, None
        self.node_tree = Tree()
        self.node_tree.nodes.append(Node("ShaderNodeBsdfPrincipled", "Principled BSDF"))


class World:
    def __init__(self, name):
        self.name, self.use_nodes, self.node_tree = name, False, Tree()


class Light:
    def __init__(self, name, type):
        self.name, self.type = name, type
        self.energy = self.size = self.color = self.spot_size = None


class Points(list):
    def __init__(self):
        super().__init__([MagicMock()])

    def add(self, n):
        self.extend(MagicMock() for _ in range(n))


class Spline:
    def __init__(self):
        self.points, self.bezier_points, self.use_cyclic_u = Points(), Points(), False


class Splines:
    def new(self, kind):
        return Spline()


class CurveData:
    def __init__(self, name, type):
        self.name, self.type, self.splines, self.materials = name, type, Splines(), []


def _new_object(name, data=None):
    o = MagicMock(name="obj_" + name)
    o.name = name
    o.type = "MESH"
    if isinstance(data, Light):
        o.type = "LIGHT"
    elif isinstance(data, CurveData):
        o.type = "FONT" if data.type == "FONT" else "CURVE"
    o.data = data if data is not None else MagicMock(name="meshdata_" + name)
    o.location, o.rotation_euler, o.scale = (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (1.0, 1.0, 1.0)
    _objects.append(o)
    return o


def _primitive(location=(0, 0, 0)):
    o = _new_object("Cube")
    o.location = tuple(location)
    _bpy.context.active_object = o


def _material(name):
    m = Material(name)
    _materials.append(m)
    return m


def _light(name, type):
    l = Light(name, type)
    _lights.append(l)
    return l


def _remove_node(self, node):
    self[:] = [n for n in self if n is not node]
    tree = _bpy.context.scene.world.node_tree
    tree.links[:] = [l for l in tree.links if l[0].node is not node and l[1].node is not node]


Nodes.remove = _remove_node
_bpy.data.objects.new.side_effect = _new_object
_bpy.data.objects.get.side_effect = lambda n: next((o for o in _objects if o.name == n), None)
_bpy.data.objects.__iter__.side_effect = lambda: iter(list(_objects))
_bpy.data.materials.new.side_effect = _material
_bpy.data.lights.new.side_effect = _light
_bpy.data.curves.new.side_effect = CurveData
_bpy.data.worlds.new.side_effect = World
_bpy.ops.mesh.primitive_cube_add.side_effect = _primitive
_bpy.context.scene.camera = None
_bpy.context.scene.world = None


def _render(animation=False):
    scene = _bpy.context.scene
    out = str(scene.render.filepath)
    for frame in range(int(scene.frame_start), int(scene.frame_end) + 1):
        Path(f"{out}{frame:04d}.png").write_bytes(b"png")


_bpy.ops.render.render.side_effect = _render


def _val(v):
    return list(v) if isinstance(v, tuple) else v


def _dump():
    record = os.environ.get("FAKE_BPY_RECORD")
    if not record:
        return
    world = _bpy.context.scene.world
    state = {
        "materials": {
            m.name: {s.name: _val(s.default_value)
                     for s in m.node_tree.nodes.get("Principled BSDF").inputs.values()
                     if s.default_value is not None}
            for m in _materials
        },
        "material_trees": {
            m.name: {
                "nodes": [n.bl_idname for n in m.node_tree.nodes],
                "links": [[o.node.bl_idname, str(o.name), i.node.bl_idname, str(i.name)]
                          for o, i in m.node_tree.links],
                "values": [{str(s.name): _val(s.default_value) for s in n.inputs.values()
                            if s.default_value is not None} for n in m.node_tree.nodes],
                "attrs": [{k: getattr(n, k) for k in ("feature", "operation", "samples")
                           if isinstance(getattr(n, k, None), str | int)}
                          for n in m.node_tree.nodes],
            }
            for m in _materials
        },
        "lights": {l.name: {"type": l.type, "energy": l.energy, "size": l.size,
                            "color": _val(l.color), "spot_size": l.spot_size}
                   for l in _lights},
        "world": None,
    }
    if isinstance(world, World):
        tree = world.node_tree
        state["world"] = {
            "nodes": [n.bl_idname for n in tree.nodes],
            "links": [[o.node.bl_idname, o.name, i.node.bl_idname, i.name] for o, i in tree.links],
            "values": {n.bl_idname + "." + s.name: _val(s.default_value)
                       for n in tree.nodes for s in n.inputs.values()
                       if s.default_value is not None},
            "ramp": [[e.position, _val(e.color)] for n in tree.nodes
                     if n.bl_idname == "ShaderNodeValToRGB" for e in n.color_ramp.elements],
        }
    with open(record, "w") as fh:
        json.dump(state, fh, default=str)


atexit.register(_dump)
import sys
sys.modules[__name__] = _bpy
"""

FAKE_BLENDER = r"""#!{python}
import os, runpy, sys
argv = sys.argv[1:]
script = argv[argv.index("--python") + 1]
sys.path.insert(0, {fake_bpy_dir!r})
sys.argv = ["blender"] + argv
try:
    runpy.run_path(script, run_name="__main__")
except SystemExit as exc:
    sys.exit(exc.code if isinstance(exc.code, int) else 1)
"""


@pytest.fixture
def blender(tmp_path: Path) -> Path:
    bpy_dir = tmp_path / "fake_bpy"
    bpy_dir.mkdir()
    (bpy_dir / "bpy.py").write_text(FAKE_BPY)
    exe = tmp_path / "blender"
    exe.write_text(FAKE_BLENDER.format(python=sys.executable, fake_bpy_dir=str(bpy_dir)))
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return exe


class Run:
    def __init__(self, proc: subprocess.CompletedProcess[str], out: Path, record: Path) -> None:
        self.proc = proc
        self.out = out
        self.state: dict[str, Any] = json.loads(record.read_text()) if record.exists() else {}

    def report(self, tool: str) -> dict[str, Any]:
        steps = json.loads((self.out / "report.json").read_text())["steps"]
        found: dict[str, Any] = next(s["report"] for s in steps if s["tool"] == tool)
        return found


def run_steps(blender: Path, tmp_path: Path, steps: list[tuple[str, dict[str, Any]]]) -> Run:
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "token": "tok",
                "steps": [{"index": i, "tool": t, "args": a} for i, (t, a) in enumerate(steps)],
                "frame_start": 1,
                "frame_end": 1,
                "output_dir": str(out),
                "assets_dir": None,
            }
        )
    )
    record = tmp_path / "bpy.json"
    env = {k: v for k, v in os.environ.items() if not k.startswith("SPECTRACUT_")}
    env.update(SPECTRACUT_EXECUTION_TOKEN="tok", FAKE_BPY_RECORD=str(record))
    cmd = [str(blender), "-b", "--factory-startup", "--python", str(RUNNER), "--", str(manifest)]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, check=False)
    return Run(proc, out, record)


CUBE = ("object.add_primitive", {"kind": "cube", "name": "Slab"})


def ok(run: Run) -> Run:
    assert run.proc.returncode == 0, run.proc.stderr
    return run


def refused(run: Run) -> str:
    assert run.proc.returncode == 1, run.proc.stderr
    assert "[args.invalid]" in run.proc.stderr, run.proc.stderr
    return run.proc.stderr


# --- registry -------------------------------------------------------------------------------


def test_registry_and_guard_list_world_gradient_and_haze() -> None:
    names = runtime_tool_names(RUNTIME / "tools.py")
    assert {"world.set_gradient", "world.set_haze", "world.set_hdri"} <= names


# --- material.apply_preset ------------------------------------------------------------------

NEW_PRESETS = {
    "clear_glass": {"Transmission Weight": 1.0, "IOR": 1.5, "Roughness": 0.0},
    "frosted_glass": {"Transmission Weight": 1.0, "IOR": 1.5, "Roughness": 0.35},
    "smoked_glass": {"Transmission Weight": 1.0, "IOR": 1.5, "Roughness": 0.05},
    "emissive": {"Emission Strength": 10.0},
    "anodised_metal": {"Metallic": 1.0, "Anisotropic": 0.5, "Roughness": 0.3},
}


@pytest.mark.parametrize("preset", sorted(NEW_PRESETS))
def test_new_presets_set_their_sockets(blender: Path, tmp_path: Path, preset: str) -> None:
    step = {"object": "Slab", "preset": preset}
    run = ok(run_steps(blender, tmp_path, [CUBE, ("material.apply_preset", step)]))
    sockets = run.state["materials"][f"{preset}.Slab"]
    for socket, value in NEW_PRESETS[preset].items():
        assert sockets[socket] == value, socket
    assert run.report("material.apply_preset") == {
        "object": "Slab",
        "material": f"{preset}.Slab",
        "preset": preset,
    }


def test_emissive_preset_applies_to_a_curve_object(blender: Path, tmp_path: Path) -> None:
    beam = ("curve.add", {"name": "Beam", "kind": "poly", "points": [[0, 0, 0], [0, 0, 3]]})
    step = {"object": "Beam", "preset": "emissive", "color": [0.1, 0.4, 1.0]}
    run = ok(run_steps(blender, tmp_path, [beam, ("material.apply_preset", step)]))
    assert run.report("material.apply_preset")["material"] == "emissive.Beam"
    assert run.state["materials"]["emissive.Beam"]["Emission Color"] == [0.1, 0.4, 1.0, 1.0]


def test_apply_preset_on_a_text_object_succeeds(blender: Path, tmp_path: Path) -> None:
    # #330: text.add makes a FONT object; the runtime refused it as "not a mesh or curve".
    text = ("text.add", {"body": "ONE BEAM", "name": "Tok_url"})
    step = {"object": "Tok_url", "preset": "emissive", "color": [1.0, 0.5, 0.1]}
    run = ok(run_steps(blender, tmp_path, [text, ("material.apply_preset", step)]))
    assert run.report("material.apply_preset")["material"] == "emissive.Tok_url"
    assert run.state["materials"]["emissive.Tok_url"]["Emission Color"] == [1.0, 0.5, 0.1, 1.0]


def test_apply_preset_refuses_an_object_without_material_slots(
    blender: Path, tmp_path: Path
) -> None:
    light = ("light.add", {"kind": "point", "name": "P"})
    run = run_steps(
        blender, tmp_path, [light, ("material.apply_preset", {"object": "P", "preset": "emissive"})]
    )
    assert "'P' has no material slots (type LIGHT)" in refused(run)


def test_emissive_glows_in_its_base_colour(blender: Path, tmp_path: Path) -> None:
    step = {"object": "Slab", "preset": "emissive", "color": [0.1, 0.4, 1.0]}
    run = ok(run_steps(blender, tmp_path, [CUBE, ("material.apply_preset", step)]))
    sockets = run.state["materials"]["emissive.Slab"]
    assert sockets["Emission Color"] == [0.1, 0.4, 1.0, 1.0]
    assert sockets["Base Color"] == [0.1, 0.4, 1.0, 1.0]


def test_overrides_land_on_sockets_and_are_reported(blender: Path, tmp_path: Path) -> None:
    step = {
        "object": "Slab",
        "preset": "frosted_glass",
        "roughness": 0.5,
        "ior": 1.33,
        "transmission": 0.9,
        "emission_color": [0, 0, 1],
        "emission_strength": 3,
        "anisotropy": 0.2,
        "metallic": 0.1,
    }
    run = ok(run_steps(blender, tmp_path, [CUBE, ("material.apply_preset", step)]))
    sockets = run.state["materials"]["frosted_glass.Slab"]
    assert sockets["Roughness"] == 0.5
    assert sockets["IOR"] == 1.33
    assert sockets["Transmission Weight"] == 0.9
    assert sockets["Emission Color"] == [0.0, 0.0, 1.0, 1.0]
    assert sockets["Emission Strength"] == 3.0
    assert sockets["Anisotropic"] == 0.2
    assert sockets["Metallic"] == 0.1
    report = run.report("material.apply_preset")
    assert report["roughness"] == 0.5 and report["ior"] == 1.33
    assert report["emission_color"] == [0.0, 0.0, 1.0, 1.0]


def test_existing_preset_without_overrides_is_unchanged(blender: Path, tmp_path: Path) -> None:
    step = {"object": "Slab", "preset": "glass", "color": [1, 1, 1]}
    run = ok(run_steps(blender, tmp_path, [CUBE, ("material.apply_preset", step)]))
    sockets = run.state["materials"]["glass.Slab"]
    assert sockets == {
        "Base Color": [1.0, 1.0, 1.0, 1.0],
        "Metallic": 0.0,
        "Roughness": 0.0,
        "IOR": 1.5,
        "Transmission Weight": 1.0,
    }
    assert run.report("material.apply_preset") == {
        "object": "Slab",
        "material": "glass.Slab",
        "preset": "glass",
    }


@pytest.mark.parametrize(
    "extra",
    [
        {"roughness": 1.5},
        {"ior": 0.5},
        {"transmission": -0.1},
        {"emission_strength": 5000},
        {"emission_color": [2, 0, 0]},
        {"anisotropy": True},
        {"metallic": "1"},
        {"brand_color": "#ffffff"},
    ],
)
def test_material_overrides_are_validated_before_bpy(
    blender: Path, tmp_path: Path, extra: dict[str, Any]
) -> None:
    step = {"object": "Slab", "preset": "clear_glass", **extra}
    refused(run_steps(blender, tmp_path, [CUBE, ("material.apply_preset", step)]))


# --- material.apply_preset surface detail (#335) --------------------------------------------

PRINCIPLED = "ShaderNodeBsdfPrincipled"


def detail_tree(
    blender: Path, tmp_path: Path, extra: dict[str, Any], preset: str = "rubber"
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    step = {"object": "Slab", "preset": preset, **extra}
    run = ok(run_steps(blender, tmp_path, [CUBE, ("material.apply_preset", step)]))
    name = f"{preset}.Slab"
    return (
        run.state["material_trees"][name],
        run.state["materials"][name],
        run.report("material.apply_preset"),
    )


def node_values(tree: dict[str, Any], bl_idname: str) -> list[dict[str, Any]]:
    return [v for n, v in zip(tree["nodes"], tree["values"], strict=True) if n == bl_idname]


def node_attrs(tree: dict[str, Any], bl_idname: str) -> list[dict[str, Any]]:
    return [a for n, a in zip(tree["nodes"], tree["attrs"], strict=True) if n == bl_idname]


def test_no_detail_keeps_the_plain_principled_tree(blender: Path, tmp_path: Path) -> None:
    tree, _, report = detail_tree(blender, tmp_path, {"roughness": 0.5})
    assert tree["nodes"] == [PRINCIPLED]
    assert tree["links"] == []
    assert "seed" not in report


def test_seed_without_a_detail_strength_builds_nothing(blender: Path, tmp_path: Path) -> None:
    tree, _, report = detail_tree(blender, tmp_path, {"seed": 4, "bump_strength": 0})
    assert tree["nodes"] == [PRINCIPLED]
    assert report["seed"] == 4 and report["bump_strength"] == 0.0


def test_noise_bump_drives_the_bsdf_normal(blender: Path, tmp_path: Path) -> None:
    tree, _, report = detail_tree(blender, tmp_path, {"bump_strength": 0.4, "bump_scale": 300})
    for node in ["ShaderNodeTexCoord", "ShaderNodeMapping", "ShaderNodeTexNoise"]:
        assert node in tree["nodes"]
    assert ["ShaderNodeBump", "Normal", PRINCIPLED, "Normal"] in tree["links"]
    assert ["ShaderNodeTexNoise", "Fac", "ShaderNodeMath", "0"] in tree["links"]
    assert ["ShaderNodeMath", "Value", "ShaderNodeBump", "Height"] in tree["links"]
    [noise] = node_values(tree, "ShaderNodeTexNoise")
    assert noise["Scale"] == 300.0
    assert report["bump_strength"] == 0.4 and report["bump_scale"] == 300.0
    assert report["seed"] == 0


def test_voronoi_bump_uses_the_cell_distance(blender: Path, tmp_path: Path) -> None:
    tree, _, _ = detail_tree(blender, tmp_path, {"bump_strength": 0.3, "bump_kind": "voronoi"})
    assert node_attrs(tree, "ShaderNodeTexVoronoi") == [{"feature": "F1"}]
    assert ["ShaderNodeTexVoronoi", "Distance", "ShaderNodeMath", "0"] in tree["links"]
    [vor] = node_values(tree, "ShaderNodeTexVoronoi")
    assert vor["Scale"] == 150.0


def test_brushing_streaks_and_turns_anisotropy(blender: Path, tmp_path: Path) -> None:
    tree, sockets, _ = detail_tree(
        blender, tmp_path, {"brushing": 0.8, "brushing_angle": 90}, preset="anodised_metal"
    )
    assert sockets["Anisotropic"] == 0.8
    assert sockets["Anisotropic Rotation"] == 0.25
    [mapping] = node_values(tree, "ShaderNodeMapping")
    assert mapping["Scale"] == [1.0, 900.0, 900.0]
    assert math.isclose(mapping["Rotation"][2], math.pi / 2)
    assert ["ShaderNodeBump", "Normal", PRINCIPLED, "Normal"] in tree["links"]


def test_brushing_keeps_a_higher_preset_anisotropy(blender: Path, tmp_path: Path) -> None:
    _, sockets, _ = detail_tree(blender, tmp_path, {"brushing": 0.2}, preset="brushed_metal")
    assert sockets["Anisotropic"] == 0.6
    assert sockets["Anisotropic Rotation"] == 0.0


def test_scratches_vary_roughness_and_bump(blender: Path, tmp_path: Path) -> None:
    tree, _, _ = detail_tree(blender, tmp_path, {"scratches": 0.5}, preset="polished_metal")
    assert node_attrs(tree, "ShaderNodeTexVoronoi") == [{"feature": "DISTANCE_TO_EDGE"}]
    assert ["ShaderNodeTexVoronoi", "Distance", "ShaderNodeMapRange", "Value"] in tree["links"]
    assert ["ShaderNodeMath", "Value", PRINCIPLED, "Roughness"] in tree["links"]
    rough = [v for v in node_values(tree, "ShaderNodeMath") if v.get("2") == 0.08]
    assert rough and rough[0]["1"] == 0.15
    assert ["ShaderNodeBump", "Normal", PRINCIPLED, "Normal"] in tree["links"]


def test_edge_wear_alone_bevels_the_bsdf_normal(blender: Path, tmp_path: Path) -> None:
    tree, _, _ = detail_tree(blender, tmp_path, {"edge_wear": 0.5})
    assert "ShaderNodeBump" not in tree["nodes"]
    assert ["ShaderNodeBevel", "Normal", PRINCIPLED, "Normal"] in tree["links"]
    [bevel] = node_values(tree, "ShaderNodeBevel")
    assert bevel["Radius"] == 0.015
    assert node_attrs(tree, "ShaderNodeBevel") == [{"samples": 8}]


def test_edge_wear_feeds_the_bump_normal(blender: Path, tmp_path: Path) -> None:
    tree, _, _ = detail_tree(blender, tmp_path, {"edge_wear": 1, "bump_strength": 0.2})
    assert ["ShaderNodeBevel", "Normal", "ShaderNodeBump", "Normal"] in tree["links"]
    assert ["ShaderNodeBump", "Normal", PRINCIPLED, "Normal"] in tree["links"]


ALL_DETAIL = {
    "bump_strength": 0.3,
    "bump_kind": "voronoi",
    "brushing": 0.5,
    "brushing_angle": 45,
    "scratches": 0.4,
    "edge_wear": 0.2,
}


def test_detail_is_deterministic_for_a_seed(blender: Path, tmp_path: Path) -> None:
    for sub in "abc":
        (tmp_path / sub).mkdir()
    a, _, _ = detail_tree(blender, tmp_path / "a", {**ALL_DETAIL, "seed": 7})
    b, _, _ = detail_tree(blender, tmp_path / "b", {**ALL_DETAIL, "seed": 7})
    c, _, report = detail_tree(blender, tmp_path / "c", {**ALL_DETAIL, "seed": 8})
    assert a == b
    assert report["seed"] == 8
    where = [v["Location"] for v in node_values(a, "ShaderNodeMapping")]
    other = [v["Location"] for v in node_values(c, "ShaderNodeMapping")]
    assert len(where) == 3 and where != other
    assert a["nodes"] == c["nodes"] and a["links"] == c["links"]


@pytest.mark.parametrize(
    "extra",
    [
        {"bump_strength": 1.5},
        {"bump_scale": 0},
        {"bump_scale": 2000},
        {"bump_kind": "perlin"},
        {"brushing": -0.1},
        {"brushing_angle": 360},
        {"scratches": "0.5"},
        {"edge_wear": True},
        {"seed": -1},
        {"seed": 1.5},
        {"seed": True},
        {"seed": 2**31},
    ],
)
def test_detail_is_validated_before_bpy(
    blender: Path, tmp_path: Path, extra: dict[str, Any]
) -> None:
    step = {"object": "Slab", "preset": "rubber", **extra}
    refused(run_steps(blender, tmp_path, [CUBE, ("material.apply_preset", step)]))


# --- light.add ------------------------------------------------------------------------------


def test_light_add_size_colour_and_spot_angle(blender: Path, tmp_path: Path) -> None:
    area = {"kind": "area", "name": "Soft", "size": 2.5, "color": [1, 0.9, 0.8]}
    spot = {"kind": "spot", "name": "Beam", "spot_angle": 30}
    run = ok(run_steps(blender, tmp_path, [("light.add", area), ("light.add", spot)]))
    lights = run.state["lights"]
    assert lights["Soft"]["type"] == "AREA" and lights["Soft"]["size"] == 2.5
    assert lights["Soft"]["color"] == [1.0, 0.9, 0.8]
    assert math.isclose(lights["Beam"]["spot_size"], math.radians(30))
    assert lights["Beam"]["size"] is None


def test_light_add_without_new_fields_is_unchanged(blender: Path, tmp_path: Path) -> None:
    run = ok(run_steps(blender, tmp_path, [("light.add", {"kind": "point", "name": "P"})]))
    assert run.state["lights"]["P"] == {
        "type": "POINT",
        "energy": 1000.0,
        "size": None,
        "color": None,
        "spot_size": None,
    }


@pytest.mark.parametrize(
    "args",
    [
        {"kind": "point", "size": 1},
        {"kind": "area", "size": 0},
        {"kind": "area", "spot_angle": 30},
        {"kind": "spot", "spot_angle": 0},
        {"kind": "spot", "spot_angle": 200},
        {"kind": "area", "color": [1, 1]},
        {"kind": "area", "color": [1, 2, 1]},
    ],
)
def test_light_add_refusals_before_bpy(blender: Path, tmp_path: Path, args: dict[str, Any]) -> None:
    run = run_steps(blender, tmp_path, [("light.add", args)])
    refused(run)
    assert run.state["lights"] == {}


# --- world.set_gradient / world.set_haze ----------------------------------------------------

GRADIENT = {"bottom": [0.01, 0.02, 0.05], "top": [0.1, 0.2, 0.4], "strength": 0.8}


def test_world_gradient_builds_a_vertical_ramp(blender: Path, tmp_path: Path) -> None:
    run = ok(run_steps(blender, tmp_path, [("world.set_gradient", GRADIENT)]))
    world = run.state["world"]
    assert world["ramp"] == [[0.0, [0.01, 0.02, 0.05, 1.0]], [1.0, [0.1, 0.2, 0.4, 1.0]]]
    assert world["values"]["ShaderNodeBackground.Strength"] == 0.8
    assert ["ShaderNodeSeparateXYZ", "Z", "ShaderNodeValToRGB", "Fac"] in world["links"]
    assert ["ShaderNodeBackground", "Background", "ShaderNodeOutputWorld", "Surface"] in world[
        "links"
    ]
    assert run.report("world.set_gradient") == {
        "bottom": [0.01, 0.02, 0.05, 1.0],
        "top": [0.1, 0.2, 0.4, 1.0],
        "strength": 0.8,
    }


def test_haze_composes_with_the_gradient_in_either_order(blender: Path, tmp_path: Path) -> None:
    haze: dict[str, Any] = {"density": 0.02, "anisotropy": 0.3}
    orders: list[list[tuple[str, dict[str, Any]]]] = [
        [("world.set_gradient", GRADIENT), ("world.set_haze", haze)],
        [("world.set_haze", haze), ("world.set_gradient", GRADIENT)],
    ]
    for order in orders:
        case = tmp_path / str(len(list(tmp_path.iterdir())))
        case.mkdir()
        world = ok(run_steps(blender, case, order)).state["world"]
        links = world["links"]
        assert ["ShaderNodeVolumeScatter", "Volume", "ShaderNodeOutputWorld", "Volume"] in links
        assert ["ShaderNodeBackground", "Background", "ShaderNodeOutputWorld", "Surface"] in links
        assert world["nodes"].count("ShaderNodeOutputWorld") == 1
        assert world["values"]["ShaderNodeVolumeScatter.Density"] == 0.02
        assert world["values"]["ShaderNodeVolumeScatter.Anisotropy"] == 0.3


def test_haze_twice_replaces_the_scatter(blender: Path, tmp_path: Path) -> None:
    steps = [("world.set_haze", {"density": 0.01}), ("world.set_haze", {"density": 0.04})]
    run = ok(run_steps(blender, tmp_path, steps))
    world = run.state["world"]
    assert world["nodes"].count("ShaderNodeVolumeScatter") == 1
    assert world["values"]["ShaderNodeVolumeScatter.Density"] == 0.04
    reports = json.loads((run.out / "report.json").read_text())["steps"]
    assert reports[-1]["report"] == {
        "density": 0.04,
        "color": [1.0, 1.0, 1.0, 1.0],
        "anisotropy": 0.0,
    }


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("world.set_haze", {"density": 0.5}),
        ("world.set_haze", {"density": 0}),
        ("world.set_haze", {}),
        ("world.set_haze", {"density": 0.01, "anisotropy": 2}),
        ("world.set_haze", {"density": 0.01, "seed": 1}),
        ("world.set_gradient", {"bottom": [0, 0, 0]}),
        ("world.set_gradient", {**GRADIENT, "strength": -1}),
        ("world.set_gradient", {**GRADIENT, "top": [0, 0, 9]}),
        ("world.set_gradient", {**GRADIENT, "python": "import os"}),
    ],
)
def test_world_refusals_before_bpy(
    blender: Path, tmp_path: Path, tool: str, args: dict[str, Any]
) -> None:
    run = run_steps(blender, tmp_path, [(tool, args)])
    refused(run)
    assert run.state["world"] is None
