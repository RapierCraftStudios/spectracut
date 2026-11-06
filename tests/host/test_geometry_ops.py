# SPDX-License-Identifier: AGPL-3.0-or-later
"""Geometry ops: curve.add, object.add_slab, object.array, object.boolean, object.parent.

These run the real ``bpy_runtime/runner.py`` against a fake ``bpy`` (the ``Run`` pattern of
``tests/host/test_bpy_ops.py``) whose objects, curves, meshes and modifiers are plain
objects, so the test can read back what each op built.
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
GEOMETRY = ["curve.add", "object.add_slab", "object.array", "object.boolean", "object.parent"]

FAKE_BPY = r"""
import atexit, json, os
from pathlib import Path
from unittest.mock import MagicMock

_bpy = MagicMock(name="bpy")
_bpy.app.version = (4, 5, 3)
_objects = []
_linked = []
_events = []


class Matrix(list):
    # A translation-only stand-in: enough to check the keep-transform algebra.
    def copy(self):
        return Matrix(self)

    def inverted(self):
        return Matrix(-v for v in self)


class Point:
    def __init__(self):
        self.co = None
        self.handle_left_type = self.handle_right_type = None


class Points(list):
    def __init__(self):
        super().__init__([Point()])

    def add(self, n):
        self.extend(Point() for _ in range(n))


class Spline:
    def __init__(self, kind):
        self.type, self.use_cyclic_u = kind, False
        self.bezier_points, self.points = Points(), Points()


class Splines(list):
    def new(self, kind):
        s = Spline(kind)
        self.append(s)
        return s


class Curve:
    def __init__(self, name, type):
        self.name, self.type, self.splines = name, type, Splines()
        self.dimensions = self.resolution_u = self.bevel_depth = None
        self.bevel_resolution = self.use_fill_caps = None


class Mesh:
    def __init__(self, name):
        self.name, self.verts, self.faces, self.updated = name, None, None, False

    def from_pydata(self, verts, edges, faces):
        self.verts, self.faces = [list(v) for v in verts], [list(f) for f in faces]

    def update(self):
        self.updated = True


class Modifier:
    def __init__(self, name, type):
        self.name, self.type = name, type


class Modifiers(list):
    def new(self, name, type):
        m = Modifier(name, type)
        self.append(m)
        return m


class Obj:
    def __init__(self, name, data):
        self.name, self.data = name, data
        self.type = "CURVE" if isinstance(data, Curve) else "MESH" if data is not None else "EMPTY"
        self.location, self.rotation_euler, self.scale = (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (1, 1, 1)
        self.modifiers, self.parent = Modifiers(), None
        self.hide_render, self.display_type = False, "TEXTURED"
        self.matrix_parent_inverse = self.matrix_basis = None
        self.material_slots = []

    @property
    def matrix_world(self):
        return Matrix(self.location)

    def copy(self):
        o = Obj(self.name + "_copy", self.data)
        o.location = self.location
        _objects.append(o)
        return o


def _new_object(name, data=None):
    o = Obj(name, data)
    _objects.append(o)
    return o


def _primitive(location=(0, 0, 0)):
    o = _new_object("Cube", Mesh("Cube"))
    o.location = tuple(location)
    _bpy.context.active_object = o


def _remove(obj, do_unlink=True):
    _objects.remove(obj)


_bpy.data.objects.new.side_effect = _new_object
_bpy.data.objects.get.side_effect = lambda n: next((o for o in _objects if o.name == n), None)
_bpy.data.objects.__iter__.side_effect = lambda: iter(list(_objects))
_bpy.data.objects.remove.side_effect = _remove
_bpy.data.curves.new.side_effect = Curve
_bpy.data.meshes.new.side_effect = Mesh
_bpy.data.materials.__iter__.side_effect = lambda: iter([])
_bpy.ops.mesh.primitive_cube_add.side_effect = _primitive
_bpy.context.scene.collection.objects.link.side_effect = lambda o: _linked.append(o.name)
_bpy.context.view_layer.update.side_effect = lambda: _events.append("view_layer.update")
_bpy.context.scene.camera = None
_bpy.context.scene.world = None


def _render(animation=False):
    scene = _bpy.context.scene
    out = str(scene.render.filepath)
    for frame in range(int(scene.frame_start), int(scene.frame_end) + 1):
        Path(f"{out}{frame:04d}.png").write_bytes(b"png")


_bpy.ops.render.render.side_effect = _render


def _plain(v):
    if isinstance(v, (Curve, Mesh)):
        return v.name
    if isinstance(v, Obj):
        return v.name
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    return v


def _attrs(o):
    return {k: _plain(v) for k, v in vars(o).items()
            if k not in ("splines", "modifiers", "bezier_points", "points")}


def _dump():
    record = os.environ.get("FAKE_BPY_RECORD")
    if not record:
        return
    state = {"linked": _linked, "events": _events, "objects": {}}
    for o in _objects:
        entry = _attrs(o)
        entry["modifiers"] = [_attrs(m) for m in o.modifiers]
        if isinstance(o.data, Curve):
            entry["curve"] = _attrs(o.data)
            entry["splines"] = [
                dict(_attrs(s), bezier=[_attrs(p) for p in s.bezier_points],
                     poly=[_attrs(p) for p in s.points])
                for s in o.data.splines
            ]
        elif isinstance(o.data, Mesh):
            entry["mesh"] = _attrs(o.data)
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

    def reports(self, tool: str) -> list[dict[str, Any]]:
        steps = json.loads((self.out / "report.json").read_text())["steps"]
        return [s["report"] for s in steps if s["tool"] == tool]

    def report(self, tool: str) -> dict[str, Any]:
        return self.reports(tool)[0]

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


def cube(name: str, location: list[float] | None = None) -> tuple[str, dict[str, Any]]:
    return (
        "object.add_primitive",
        {"kind": "cube", "name": name, "location": location or [0, 0, 0]},
    )


# --- registry -------------------------------------------------------------------------------


def test_registry_and_guard_list_the_geometry_ops() -> None:
    assert set(GEOMETRY) <= runtime_tool_names(RUNTIME / "tools.py")


# --- curve.add ------------------------------------------------------------------------------


def test_bezier_tube_through_control_points(blender: Path, tmp_path: Path) -> None:
    points = [[0, 0, 0], [1, 0, 1], [2, 0, 0]]
    step = {"name": "Beam", "points": points, "bevel_depth": 0.05, "bevel_resolution": 6}
    run = ok(run_steps(blender, tmp_path, [("curve.add", {**step, "location": [0, 1, 0]})]))
    assert run.report("curve.add") == {"object": "Beam", "kind": "bezier", "points": 3}
    beam = run.obj("Beam")
    assert beam["type"] == "CURVE" and beam["location"] == [0.0, 1.0, 0.0]
    assert run.state["linked"] == ["Beam"]
    curve = beam["curve"]
    assert curve["type"] == "CURVE" and curve["dimensions"] == "3D"
    assert curve["bevel_depth"] == 0.05 and curve["bevel_resolution"] == 6
    assert curve["resolution_u"] == 12 and curve["use_fill_caps"] is True
    (spline,) = beam["splines"]
    assert spline["type"] == "BEZIER" and spline["use_cyclic_u"] is False
    assert [p["co"] for p in spline["bezier"]] == [[float(v) for v in p] for p in points]
    assert {p["handle_left_type"] for p in spline["bezier"]} == {"AUTO"}


def test_poly_curve_uses_weighted_points(blender: Path, tmp_path: Path) -> None:
    step = {"name": "Cable", "points": [[0, 0, 0], [0, 2, 0]], "kind": "poly", "cyclic": True}
    run = ok(run_steps(blender, tmp_path, [("curve.add", step)]))
    (spline,) = run.obj("Cable")["splines"]
    assert spline["type"] == "POLY" and spline["use_cyclic_u"] is True
    assert [p["co"] for p in spline["poly"]] == [[0.0, 0.0, 0.0, 1.0], [0.0, 2.0, 0.0, 1.0]]


# --- object.add_slab ------------------------------------------------------------------------


def test_slab_is_built_at_true_size_with_a_bevel(blender: Path, tmp_path: Path) -> None:
    step = {"name": "Tier", "dimensions": [2, 1, 0.1], "bevel_width": 0.02, "segments": 4}
    run = ok(run_steps(blender, tmp_path, [("object.add_slab", step)]))
    assert run.report("object.add_slab") == {
        "object": "Tier",
        "dimensions": [2.0, 1.0, 0.1],
        "modifier": "bevel",
    }
    tier = run.obj("Tier")
    assert tier["scale"] == [1, 1, 1]
    verts = tier["mesh"]["verts"]
    assert len(verts) == 8 and len(tier["mesh"]["faces"]) == 6 and tier["mesh"]["updated"]
    for axis, size in enumerate([2.0, 1.0, 0.1]):
        values = [v[axis] for v in verts]
        assert max(values) - min(values) == pytest.approx(size)
    (bevel,) = tier["modifiers"]
    assert bevel == {
        "name": "bevel",
        "type": "BEVEL",
        "width": 0.02,
        "segments": 4,
        "limit_method": "ANGLE",
    }


def test_slab_without_bevel_has_no_modifier(blender: Path, tmp_path: Path) -> None:
    run = ok(run_steps(blender, tmp_path, [("object.add_slab", {"dimensions": [1, 1, 1]})]))
    assert run.report("object.add_slab") == {"object": "Slab", "dimensions": [1.0, 1.0, 1.0]}
    assert run.obj("Slab")["modifiers"] == []


# --- object.array ---------------------------------------------------------------------------


def test_array_modifier_with_constant_offset(blender: Path, tmp_path: Path) -> None:
    step = {"object": "Box", "count": 5, "offset": [0, 0, 0.3]}
    run = ok(run_steps(blender, tmp_path, [cube("Box"), ("object.array", step)]))
    assert run.report("object.array") == {
        "object": "Box",
        "mode": "modifier",
        "count": 5,
        "modifier": "array.Box",
    }
    (mod,) = run.obj("Box")["modifiers"]
    assert mod["type"] == "ARRAY" and mod["count"] == 5
    assert mod["use_relative_offset"] is False and mod["use_constant_offset"] is True
    assert mod["constant_offset_displace"] == [0.0, 0.0, 0.3]


def test_array_linked_duplicates_share_the_mesh(blender: Path, tmp_path: Path) -> None:
    step = {"object": "Box", "count": 3, "offset": [1, 0, 0], "mode": "linked"}
    run = ok(run_steps(blender, tmp_path, [cube("Box", [0, 0, 1]), ("object.array", step)]))
    assert run.report("object.array")["duplicates"] == ["Box.001", "Box.002"]
    assert run.obj("Box.001")["location"] == [1.0, 0.0, 1.0]
    assert run.obj("Box.002")["location"] == [2.0, 0.0, 1.0]
    assert run.obj("Box.002")["data"] == run.obj("Box")["data"]
    assert run.state["linked"] == ["Box.001", "Box.002"]


# --- object.boolean -------------------------------------------------------------------------


@pytest.mark.parametrize("op", ["difference", "union", "intersect"])
def test_boolean_against_a_hidden_cutter(blender: Path, tmp_path: Path, op: str) -> None:
    step = {"object": "Plate", "cutter": "Bore", "operation": op}
    run = ok(run_steps(blender, tmp_path, [cube("Plate"), cube("Bore"), ("object.boolean", step)]))
    assert run.report("object.boolean") == {
        "object": "Plate",
        "cutter": "Bore",
        "operation": op,
        "modifier": "boolean.Bore",
    }
    (mod,) = run.obj("Plate")["modifiers"]
    assert mod == {
        "name": "boolean.Bore",
        "type": "BOOLEAN",
        "operation": op.upper(),
        "object": "Bore",
    }
    bore = run.obj("Bore")
    assert bore["hide_render"] is True and bore["display_type"] == "WIRE"
    assert run.obj("Plate")["hide_render"] is False


# --- object.parent --------------------------------------------------------------------------


def test_parent_keeps_the_child_world_transform(blender: Path, tmp_path: Path) -> None:
    steps = [cube("Rig", [1, 2, 3]), cube("Lamp", [5, 0, 0])]
    run = ok(
        run_steps(
            blender, tmp_path, [*steps, ("object.parent", {"child": "Lamp", "parent": "Rig"})]
        )
    )
    assert run.report("object.parent") == {"child": "Lamp", "parent": "Rig"}
    lamp = run.obj("Lamp")
    assert lamp["parent"] == "Rig"
    assert lamp["matrix_parent_inverse"] == [-1.0, -2.0, -3.0]
    assert lamp["matrix_basis"] == [5.0, 0.0, 0.0]
    assert run.state["events"] == ["view_layer.update"]


# --- refusals: checked before bpy is touched -------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("curve.add", {"points": [[0, 0, 0]]}),
        ("curve.add", {"points": [[0, 0, 0], [1, 1]]}),
        ("curve.add", {"points": [[0, 0, 0], [1, 1, 1]], "kind": "nurbs"}),
        ("curve.add", {"points": [[0, 0, 0], [1, 1, 1]], "bevel_depth": 11}),
        ("curve.add", {"points": [[0, 0, 0], [1, 1, 1]], "python": "x"}),
        ("object.add_slab", {"dimensions": [1, 0, 1]}),
        ("object.add_slab", {"dimensions": [1, 1, 0.1], "bevel_width": 0.06}),
        ("object.add_slab", {"dimensions": [1, 1, 1], "segments": 0}),
        ("object.array", {"object": "Box", "count": 1, "offset": [1, 0, 0]}),
        ("object.array", {"object": "Box", "count": 2, "offset": [1, 0, 0], "mode": "copy"}),
        ("object.boolean", {"object": "Box", "cutter": "Box"}),
        ("object.boolean", {"object": "Box", "cutter": "Bore", "operation": "xor"}),
        ("object.parent", {"child": "Box", "parent": "Box"}),
        ("object.parent", {"child": "Box"}),
    ],
)
def test_bad_args_are_refused_before_bpy(
    blender: Path, tmp_path: Path, tool: str, args: dict[str, Any]
) -> None:
    run = run_steps(blender, tmp_path, [(tool, args)])
    refused(run)
    assert run.state["linked"] == [] and run.state["objects"] == {}


@pytest.mark.parametrize(
    "step",
    [
        ("object.array", {"object": "Missing", "count": 2, "offset": [1, 0, 0]}),
        ("object.boolean", {"object": "Box", "cutter": "Missing"}),
        ("object.parent", {"child": "Missing", "parent": "Box"}),
    ],
)
def test_missing_objects_are_reported(
    blender: Path, tmp_path: Path, step: tuple[str, dict[str, Any]]
) -> None:
    refused(run_steps(blender, tmp_path, [cube("Box"), step]), "scene.object_not_found")


def test_boolean_needs_mesh_operands(blender: Path, tmp_path: Path) -> None:
    curve = ("curve.add", {"name": "Wire", "points": [[0, 0, 0], [1, 0, 0]]})
    step = ("object.boolean", {"object": "Box", "cutter": "Wire"})
    assert "not a mesh" in refused(run_steps(blender, tmp_path, [cube("Box"), curve, step]))


def test_parent_refuses_a_cycle(blender: Path, tmp_path: Path) -> None:
    steps = [
        cube("A"),
        cube("B"),
        ("object.parent", {"child": "B", "parent": "A"}),
        ("object.parent", {"child": "A", "parent": "B"}),
    ]
    assert "descendant" in refused(run_steps(blender, tmp_path, steps))


def test_existing_names_are_refused(blender: Path, tmp_path: Path) -> None:
    slab = ("object.add_slab", {"name": "Box", "dimensions": [1, 1, 1]})
    assert "already exists" in refused(run_steps(blender, tmp_path, [cube("Box"), slab]))
