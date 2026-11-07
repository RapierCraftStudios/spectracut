# SPDX-License-Identifier: AGPL-3.0-or-later
"""Keyframe and particle ops: object.keyframe, render.set_frame_range, particles.add.

These run the real ``bpy_runtime/runner.py`` against a fake ``bpy`` (the ``Run`` pattern of
``tests/host/test_bpy_ops.py``) whose objects, materials, f-curves and particle systems are
plain objects, so the test can read back every key, easing and particle setting.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from spectracut_host.guard import runtime_tool_names

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "host" / "bpy_runtime"
RUNNER = RUNTIME / "runner.py"
NEW_OPS = ["object.keyframe", "render.set_frame_range", "particles.add"]

FAKE_BPY = r"""
import atexit, json, os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

_bpy = MagicMock(name="bpy")
_bpy.app.version = (4, 5, 3)
_objects = []
_events = []


class Point:
    def __init__(self, frame, value):
        self.frame, self.value = frame, value
        self.interpolation, self.easing = "BEZIER", "AUTO"


class FCurve:
    def __init__(self, data_path):
        self.data_path, self.keyframe_points = data_path, []


class Keyable:
    # keyframe_insert snapshots the value into an f-curve of this block's action.
    def _block(self):
        return self

    def _value(self, data_path):
        v = getattr(self, data_path)
        return list(v) if isinstance(v, (list, tuple)) else v

    def keyframe_insert(self, data_path, frame):
        block = self._block()
        if block.animation_data is None:
            block.animation_data = SimpleNamespace(action=SimpleNamespace(fcurves=[]))
        path = self._path(data_path)
        curves = block.animation_data.action.fcurves
        curve = next((c for c in curves if c.data_path == path), None)
        if curve is None:
            curve = FCurve(path)
            curves.append(curve)
        curve.keyframe_points.append(Point(frame, self._value(data_path)))
        return True

    def _path(self, data_path):
        return data_path


class Socket(Keyable):
    def __init__(self, tree, name):
        self.tree, self.name, self.default_value = tree, name, 0.0

    def _block(self):
        return self.tree

    def _path(self, data_path):
        return f'nodes["Principled BSDF"].inputs["{self.name}"].{data_path}'


class Inputs(dict):
    pass


class Node:
    def __init__(self, tree):
        self.inputs = Inputs({n: Socket(tree, n) for n in
                              ("Emission Strength", "Alpha", "Roughness", "Transmission Weight")})


class Tree:
    def __init__(self):
        self.animation_data = None
        self.nodes = {"Principled BSDF": Node(self)}


class Material:
    def __init__(self, name, bsdf=True):
        self.name = name
        self.node_tree = Tree()
        if not bsdf:
            self.node_tree.nodes = {}


class CameraData(Keyable):
    def __init__(self):
        self.lens, self.animation_data = 50.0, None
        self.sensor_width, self.sensor_height, self.sensor_fit = 36.0, 24.0, "AUTO"
        self.dof = SimpleNamespace(
            use_dof=False, focus_object=None, focus_distance=10.0, aperture_fstop=2.8
        )


class Settings:
    def __init__(self):
        self.instance_object = None


class Modifier:
    def __init__(self, name, type):
        self.name, self.type = name, type
        self.particle_system = SimpleNamespace(seed=None, settings=Settings())


class Modifiers(list):
    def new(self, name, type):
        m = Modifier(name, type)
        self.append(m)
        _events.append(f"modifier:{name}")
        return m


class Obj(Keyable):
    def __init__(self, name, type="MESH", data=None):
        self.name, self.type, self.data = name, type, data
        self.location, self.rotation_euler, self.scale = (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (1, 1, 1)
        self.animation_data = None
        self.modifiers = Modifiers()
        self.matrix_world = [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0],
                             [0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0]]
        self.active_material = Material("Base") if type == "MESH" else None
        self.material_slots = (
            [SimpleNamespace(material=self.active_material),
             SimpleNamespace(material=Material("Glow"))]
            if type == "MESH" else []
        )


def _primitive(location=(0, 0, 0)):
    o = Obj("Cube")
    o.location = tuple(location)
    _objects.append(o)
    _bpy.context.active_object = o


def _camera_new(name, data):
    o = Obj(name, "CAMERA", CameraData())
    _objects.append(o)
    return o


_bpy.data.objects.new.side_effect = _camera_new
_bpy.data.objects.get.side_effect = lambda n: next((o for o in _objects if o.name == n), None)
_bpy.data.objects.__iter__.side_effect = lambda: iter(list(_objects))
_bpy.data.materials.__iter__.side_effect = lambda: iter([])
_bpy.ops.mesh.primitive_cube_add.side_effect = _primitive
_bpy.context.scene.camera = None
_bpy.context.scene.world = None


def _render(animation=False):
    scene = _bpy.context.scene
    out = str(scene.render.filepath)
    for frame in range(int(scene.frame_start), int(scene.frame_end) + 1):
        Path(f"{out}{frame:04d}.png").write_bytes(b"png")


_bpy.ops.render.render.side_effect = _render


def _curves(block):
    if block is None or block.animation_data is None:
        return []
    return [
        {"path": c.data_path,
         "points": [[p.frame, p.value, p.interpolation, p.easing] for p in c.keyframe_points]}
        for c in block.animation_data.action.fcurves
    ]


def _dump():
    record = os.environ.get("FAKE_BPY_RECORD")
    if not record:
        return
    state = {"events": _events, "objects": {}}
    for o in _objects:
        entry = {"type": o.type, "curves": _curves(o)}
        if o.type == "CAMERA":
            entry["data_curves"] = _curves(o.data)
        if o.active_material is not None:
            entry["material_curves"] = {
                s.material.name: _curves(s.material.node_tree) for s in o.material_slots
            }
        entry["particles"] = [
            {"name": m.name, "type": m.type, "seed": m.particle_system.seed,
             "settings": {k: (v.name if isinstance(v, Obj) else v)
                          for k, v in vars(m.particle_system.settings).items()}}
            for m in o.modifiers
        ]
        state["objects"][o.name] = entry
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

    def obj(self, name: str) -> dict[str, Any]:
        found: dict[str, Any] = self.state["objects"][name]
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


def ok(run: Run) -> Run:
    assert run.proc.returncode == 0, run.proc.stderr
    return run


def refused(run: Run, code: str = "args.invalid") -> str:
    assert run.proc.returncode == 1, run.proc.stderr
    assert f"[{code}]" in run.proc.stderr, run.proc.stderr
    return run.proc.stderr


def cube(name: str) -> tuple[str, dict[str, Any]]:
    return ("object.add_primitive", {"kind": "cube", "name": name})


def curve(entry: list[dict[str, Any]], path: str) -> list[list[Any]]:
    found: list[list[Any]] = next(c["points"] for c in entry if c["path"] == path)
    return found


# --- registry -------------------------------------------------------------------------------


def test_registry_and_guard_list_the_new_ops() -> None:
    assert set(NEW_OPS) <= runtime_tool_names(RUNTIME / "tools.py")


# --- object.keyframe ------------------------------------------------------------------------


def test_keyframes_transforms_with_easing(blender: Path, tmp_path: Path) -> None:
    keys = [
        {"frame": 1, "location": [0, 0, 0], "scale": [1, 1, 1]},
        {"frame": 24, "location": [0, 0, 2], "rotation": [0, 0, 1.5], "scale": [2, 2, 2]},
    ]
    step = {"object": "Box", "keys": keys, "easing": "ease_out"}
    run = ok(run_steps(blender, tmp_path, [cube("Box"), ("object.keyframe", step)]))
    assert run.report("object.keyframe") == {
        "object": "Box",
        "easing": "ease_out",
        "frames": [1, 24],
        "channels": ["location", "rotation", "scale"],
    }
    curves = run.obj("Box")["curves"]
    assert curve(curves, "location") == [
        [1, [0.0, 0.0, 0.0], "BEZIER", "EASE_OUT"],
        [24, [0.0, 0.0, 2.0], "BEZIER", "EASE_OUT"],
    ]
    assert curve(curves, "rotation_euler") == [[24, [0.0, 0.0, 1.5], "BEZIER", "EASE_OUT"]]
    assert [p[1] for p in curve(curves, "scale")] == [[1.0, 1.0, 1.0], [2.0, 2.0, 2.0]]


@pytest.mark.parametrize(
    ("easing", "interp", "ease"),
    [
        ("linear", "LINEAR", "AUTO"),
        ("ease_in", "BEZIER", "EASE_IN"),
        ("ease_in_out", "BEZIER", "EASE_IN_OUT"),
    ],
)
def test_every_easing_matches_camera_move(
    blender: Path, tmp_path: Path, easing: str, interp: str, ease: str
) -> None:
    keys = [{"frame": 0, "location": [0, 0, 0]}, {"frame": 10, "location": [1, 0, 0]}]
    step = {"object": "Box", "keys": keys, "easing": easing}
    run = ok(run_steps(blender, tmp_path, [cube("Box"), ("object.keyframe", step)]))
    assert {(p[2], p[3]) for p in curve(run.obj("Box")["curves"], "location")} == {(interp, ease)}


def test_default_easing_is_ease_in_out(blender: Path, tmp_path: Path) -> None:
    keys = [{"frame": 0, "location": [0, 0, 0]}, {"frame": 10, "location": [1, 0, 0]}]
    run = ok(
        run_steps(
            blender, tmp_path, [cube("Box"), ("object.keyframe", {"object": "Box", "keys": keys})]
        )
    )
    assert run.report("object.keyframe")["easing"] == "ease_in_out"


def test_keys_material_inputs_on_the_active_and_a_named_material(
    blender: Path, tmp_path: Path
) -> None:
    keys = [
        {"frame": 1, "inputs": {"emission_strength": 0, "alpha": 1}},
        {"frame": 30, "inputs": {"emission_strength": 40, "alpha": 0.2}},
    ]
    glow = [{"frame": 1, "inputs": {"roughness": 0.1}}, {"frame": 5, "inputs": {"roughness": 0.9}}]
    steps = [
        cube("Box"),
        ("object.keyframe", {"object": "Box", "keys": keys, "easing": "linear"}),
        ("object.keyframe", {"object": "Box", "keys": glow, "material": "Glow"}),
    ]
    run = ok(run_steps(blender, tmp_path, steps))
    assert run.report("object.keyframe")["channels"] == [
        "inputs.alpha",
        "inputs.emission_strength",
    ]
    mats = run.obj("Box")["material_curves"]
    emission = curve(
        mats["Base"], 'nodes["Principled BSDF"].inputs["Emission Strength"].default_value'
    )
    assert emission == [[1, 0.0, "LINEAR", "AUTO"], [30, 40.0, "LINEAR", "AUTO"]]
    alpha = curve(mats["Base"], 'nodes["Principled BSDF"].inputs["Alpha"].default_value')
    assert [p[1] for p in alpha] == [1.0, 0.2]
    rough = curve(mats["Glow"], 'nodes["Principled BSDF"].inputs["Roughness"].default_value')
    assert rough == [[1, 0.1, "BEZIER", "EASE_IN_OUT"], [5, 0.9, "BEZIER", "EASE_IN_OUT"]]
    assert run.obj("Box")["curves"] == []


def test_keys_a_camera_transform_and_lens(blender: Path, tmp_path: Path) -> None:
    keys = [
        {"frame": 1, "location": [0, -5, 1], "lens": 35},
        {"frame": 48, "location": [0, -3, 1], "lens": 85},
    ]
    steps = [
        ("camera.add", {"name": "Cam"}),
        ("object.keyframe", {"object": "Cam", "keys": keys, "easing": "ease_in"}),
    ]
    run = ok(run_steps(blender, tmp_path, steps))
    assert run.report("object.keyframe")["channels"] == ["location", "lens"]
    cam = run.obj("Cam")
    assert curve(cam["data_curves"], "lens") == [
        [1, 35.0, "BEZIER", "EASE_IN"],
        [48, 85.0, "BEZIER", "EASE_IN"],
    ]
    assert [p[3] for p in curve(cam["curves"], "location")] == ["EASE_IN", "EASE_IN"]


TWO = [{"frame": 0, "location": [0, 0, 0]}, {"frame": 5, "location": [1, 0, 0]}]


@pytest.mark.parametrize(
    ("args", "code"),
    [
        ({"object": "Box", "keys": TWO, "python": "x"}, "args.invalid"),
        ({"object": "Box", "keys": TWO[:1]}, "args.invalid"),
        ({"object": "Box", "keys": [TWO[1], TWO[0]]}, "args.invalid"),
        (
            {"object": "Box", "keys": [{"frame": 0}, {"frame": 1, "location": [0, 0, 0]}]},
            "args.invalid",
        ),
        ({"object": "Box", "keys": TWO, "easing": "bounce"}, "args.invalid"),
        (
            {"object": "Box", "keys": [{"frame": 0, "lens": 50}, {"frame": 1, "lens": 60}]},
            "args.invalid",
        ),
        (
            {
                "object": "Box",
                "keys": [
                    {"frame": 0, "inputs": {"ior": 1.4}},
                    {"frame": 1, "inputs": {"alpha": 1}},
                ],
            },
            "args.invalid",
        ),
        (
            {
                "object": "Box",
                "keys": [
                    {"frame": 0, "inputs": {"alpha": 2}},
                    {"frame": 1, "inputs": {"alpha": 1}},
                ],
            },
            "args.invalid",
        ),
        ({"object": "Box", "keys": TWO, "material": "Glow"}, "args.invalid"),
        (
            {
                "object": "Box",
                "material": "Nope",
                "keys": [
                    {"frame": 0, "inputs": {"alpha": 0}},
                    {"frame": 1, "inputs": {"alpha": 1}},
                ],
            },
            "args.invalid",
        ),
        ({"object": "Ghost", "keys": TWO}, "scene.object_not_found"),
    ],
)
def test_bad_keyframe_args_are_refused_before_any_key(
    blender: Path, tmp_path: Path, args: dict[str, Any], code: str
) -> None:
    run = run_steps(blender, tmp_path, [cube("Box"), ("object.keyframe", args)])
    refused(run, code)
    assert run.obj("Box")["curves"] == []
    assert all(c == [] for c in run.obj("Box")["material_curves"].values())


# --- render.set_frame_range -----------------------------------------------------------------


def test_frame_range_is_set_and_reported(blender: Path, tmp_path: Path) -> None:
    step = {"frame_start": 10, "frame_end": 120}
    run = ok(run_steps(blender, tmp_path, [("render.set_frame_range", step)]))
    assert run.report("render.set_frame_range") == {"frame_start": 10, "frame_end": 120}


@pytest.mark.parametrize(
    "args",
    [
        {"frame_start": 5, "frame_end": 5},
        {"frame_start": -1, "frame_end": 5},
        {"frame_start": 1},
        {"frame_start": 1, "frame_end": 2, "fps": 24},
    ],
)
def test_bad_frame_ranges_are_refused(blender: Path, tmp_path: Path, args: dict[str, Any]) -> None:
    refused(run_steps(blender, tmp_path, [("render.set_frame_range", args)]))


# --- particles.add --------------------------------------------------------------------------

DUST = {
    "emitter": "Box",
    "name": "Dust",
    "count": 500,
    "seed": 7,
    "lifetime": 120,
    "size": 0.005,
    "velocity": 0.2,
    "frame_start": 1,
    "frame_end": 96,
    "instance": "Mote",
}


def test_particles_on_an_emitter_with_an_instance(blender: Path, tmp_path: Path) -> None:
    run = ok(run_steps(blender, tmp_path, [cube("Box"), cube("Mote"), ("particles.add", DUST)]))
    assert run.report("particles.add") == {
        "emitter": "Box",
        "system": "Dust",
        "seed": 7,
        "count": 500,
        "lifetime": 120.0,
        "size": 0.005,
        "velocity": 0.2,
        "frame_start": 1,
        "frame_end": 96,
        "instance": "Mote",
    }
    (system,) = run.obj("Box")["particles"]
    assert system["type"] == "PARTICLE_SYSTEM" and system["seed"] == 7
    assert system["settings"] == {
        "instance_object": "Mote",
        "name": "Dust",
        "type": "EMITTER",
        "count": 500,
        "frame_start": 1,
        "frame_end": 96,
        "lifetime": 120.0,
        "particle_size": 0.005,
        "normal_factor": 0.2,
        "render_type": "OBJECT",
    }


def test_particles_defaults_render_halos(blender: Path, tmp_path: Path) -> None:
    run = ok(run_steps(blender, tmp_path, [cube("Box"), ("particles.add", {"emitter": "Box"})]))
    report = run.report("particles.add")
    assert report["seed"] == 0 and report["system"] == "Dust" and report["instance"] is None
    (system,) = run.obj("Box")["particles"]
    assert system["settings"]["render_type"] == "HALO"


def test_particles_are_deterministic_for_a_seed(blender: Path, tmp_path: Path) -> None:
    steps = [cube("Box"), cube("Mote"), ("particles.add", DUST)]
    runs = []
    for i in range(2):
        work = tmp_path / f"run{i}"
        work.mkdir()
        runs.append(ok(run_steps(blender, work, steps)))
    a, b = runs
    assert a.report("particles.add") == b.report("particles.add")
    assert a.obj("Box")["particles"] == b.obj("Box")["particles"]


@pytest.mark.parametrize(
    ("args", "code"),
    [
        ({**DUST, "count": 0}, "args.invalid"),
        ({**DUST, "seed": -1}, "args.invalid"),
        ({**DUST, "size": 0}, "args.invalid"),
        ({**DUST, "frame_start": 10, "frame_end": 5}, "args.invalid"),
        ({**DUST, "python": "import os"}, "args.invalid"),
        ({**DUST, "instance": "Box"}, "args.invalid"),
        ({**DUST, "emitter": "Cam", "instance": "Mote"}, "args.invalid"),
        ({**DUST, "instance": "Ghost"}, "scene.object_not_found"),
    ],
)
def test_bad_particle_args_are_refused(
    blender: Path, tmp_path: Path, args: dict[str, Any], code: str
) -> None:
    steps = [cube("Box"), cube("Mote"), ("camera.add", {"name": "Cam"}), ("particles.add", args)]
    run = run_steps(blender, tmp_path, steps)
    refused(run, code)
    assert run.obj("Box")["particles"] == []
