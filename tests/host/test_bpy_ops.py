# SPDX-License-Identifier: AGPL-3.0-or-later
"""Harness host ops in the bpy runtime: glTF, materials, camera, render props, inspect, look.

Pure maths (framing projection, auto-framing) is checked with known numbers in a
separate ``python -I`` interpreter, because the control plane's import blocker
refuses ``bpy_runtime`` in this process. The ops themselves run through a fake
``blender`` whose fake ``bpy`` keeps real objects (names, transforms, bounding
boxes, custom properties) on top of a recording ``MagicMock``.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from spectracut_host.guard import ExecutionGuard, HostRegistration, runtime_tool_names

from spectracut.queue.profile import RenderProfile

ROOT = Path(__file__).resolve().parents[2]
HOST = ROOT / "host"
RUNTIME = HOST / "bpy_runtime"
RUNNER = RUNTIME / "runner.py"

FAKE_BPY = r"""
import atexit, json, math, os
from pathlib import Path
from unittest.mock import MagicMock

_bpy = MagicMock(name="bpy")
_bpy.app.version = (4, 5, 3)
_objects = []
_events = []


def _mat(loc, rot):
    rx, ry, rz = rot
    cx, sx, cy, sy, cz, sz = (math.cos(rx), math.sin(rx), math.cos(ry), math.sin(ry),
                              math.cos(rz), math.sin(rz))
    r = [[cz * cy, cz * sy * sx - sz * cx, cz * sy * cx + sz * sx],
         [sz * cy, sz * sy * sx + cz * cx, sz * sy * cx - cz * sx],
         [-sy, cy * sx, cy * cx]]
    return [r[0] + [loc[0]], r[1] + [loc[1]], r[2] + [loc[2]], [0, 0, 0, 1]]


def _cam_data(name):
    d = MagicMock(name="camdata_" + name)
    d.fake_type = "CAMERA"
    d.lens, d.sensor_width, d.sensor_height, d.sensor_fit = 50.0, 36.0, 24.0, "AUTO"
    d.copy.side_effect = lambda: _cam_data(name + ".copy")
    _bpy.attach_mock(d, "camdata_" + name.replace(".", "_"))
    return d


def _new_object(name, data=None):
    o = MagicMock(name="obj_" + name)
    o.name = name
    o.type = "CAMERA" if getattr(data, "fake_type", None) == "CAMERA" else "MESH"
    o.data = data if data is not None else MagicMock(name="meshdata_" + name)
    o.parent = None
    o.location, o.rotation_euler, o.scale = (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (1.0, 1.0, 1.0)
    props = {}
    o.__getitem__.side_effect = props.__getitem__
    o.__setitem__.side_effect = props.__setitem__
    o.get.side_effect = props.get
    type(o).matrix_world = property(lambda s: _mat(s.location, s.rotation_euler))
    type(o).bound_box = property(lambda s: [
        (x * s.scale[0], y * s.scale[1], z * s.scale[2])
        for x in (-1, 1) for y in (-1, 1) for z in (-1, 1)
    ])
    o.keyframe_insert.side_effect = lambda **kw: _events.append(["key", name, kw])
    _objects.append(o)
    return o


def _get(name):
    return next((o for o in _objects if o.name == name), None)


def _remove(obj, do_unlink=True):
    _objects.remove(obj)


def _primitive(location=(0, 0, 0)):
    o = _new_object("Cube")
    o.location = tuple(location)
    _bpy.context.active_object = o


def _import_gltf(filepath):
    _events.append(["import_gltf", filepath])
    root = _new_object("Phone")
    child = _new_object("Phone.Screen")
    child.parent = root


def _material(name):
    m = MagicMock(name="mat_" + name)
    _bpy.attach_mock(m, "mat_" + name.replace(".", "_"))
    m.name = name
    m.node_tree.nodes.get.return_value.inputs.__contains__.return_value = True
    return m


_bpy.data.objects.new.side_effect = _new_object
_bpy.data.objects.get.side_effect = _get
_bpy.data.objects.remove.side_effect = _remove
_bpy.data.objects.__iter__.side_effect = lambda: iter(list(_objects))
_bpy.data.cameras.new.side_effect = _cam_data
_bpy.data.materials.new.side_effect = _material
_bpy.ops.mesh.primitive_cube_add.side_effect = _primitive
_bpy.ops.import_scene.gltf.side_effect = _import_gltf
_bpy.context.scene.camera = None
_bpy.context.scene.render.resolution_x = 1920
_bpy.context.scene.render.resolution_y = 1080


def _render(animation=False):
    scene = _bpy.context.scene
    out = str(scene.render.filepath)
    for frame in range(int(scene.frame_start), int(scene.frame_end) + 1):
        Path(f"{out}{frame:04d}.png").write_bytes(b"png")


_bpy.ops.render.render.side_effect = _render


def _dump():
    record = os.environ.get("FAKE_BPY_RECORD")
    if record:
        scene = _bpy.context.scene
        state = {
            "calls": [str(c) for c in _bpy.mock_calls],
            "events": _events,
            "camera": getattr(scene.camera, "name", None),
            "resolution": [scene.render.resolution_x, scene.render.resolution_y],
            "engine": str(scene.render.engine),
            "objects": {
                o.name: {"location": list(o.location), "rotation": list(o.rotation_euler),
                         "type": o.type}
                for o in _objects
            },
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

    @property
    def reports(self) -> list[dict[str, Any]]:
        path = self.out / "report.json"
        steps: list[dict[str, Any]] = json.loads(path.read_text())["steps"]
        return steps

    def report(self, tool: str) -> dict[str, Any]:
        found: dict[str, Any] = next(s["report"] for s in self.reports if s["tool"] == tool)
        return found


def run_steps(
    blender: Path,
    tmp_path: Path,
    steps: list[tuple[str, dict[str, Any]]],
    assets_dir: Path | None = None,
) -> Run:
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
                "assets_dir": str(assets_dir) if assets_dir else None,
            }
        )
    )
    record = tmp_path / "bpy.json"
    env = {k: v for k, v in os.environ.items() if not k.startswith("SPECTRACUT_")}
    env.update(SPECTRACUT_EXECUTION_TOKEN="tok", FAKE_BPY_RECORD=str(record))
    cmd = [str(blender), "-b", "--factory-startup", "--python", str(RUNNER), "--", str(manifest)]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, check=False)
    return Run(proc, out, record)


CAM = ("camera.add", {"name": "Cam", "location": [0, -10, 0], "rotation": [1.5707963, 0, 0]})
CUBE = ("object.add_primitive", {"kind": "cube", "name": "Box"})


def pure(code: str) -> Any:
    """Run ``code`` with only ``host/`` importable and return its printed JSON."""
    prelude = "import json, sys; sys.path.insert(0, sys.argv[1])\n"
    result = subprocess.run(
        [sys.executable, "-I", "-c", prelude + code, str(HOST)],
        capture_output=True,
        text=True,
        check=False,
        env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"},
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


# --- pure projection maths -------------------------------------------------------------


def _coverage(corners: list[list[float]], **kw: Any) -> dict[str, Any]:
    args = {"lens": 50, "sw": 36, "sh": 24, "fit": "AUTO", "rx": 1920, "ry": 1080}
    args.update(kw)
    found: dict[str, Any] = pure(
        "from bpy_runtime.ops.inspect import euler_matrix, framing_coverage\n"
        f"a = {args!r}\nc = {corners!r}\n"
        "m = euler_matrix(a.get('loc', (0, 0, 0)), a.get('rot', (0, 0, 0)))\n"
        "print(json.dumps(framing_coverage(m, a['lens'], a['sw'], a['sh'], a['fit'],"
        " a['rx'], a['ry'], c)))\n"
    )
    return found


def _box(
    x: tuple[float, float], y: tuple[float, float], z: tuple[float, float]
) -> list[list[float]]:
    return [[a, b, c] for a in x for b in y for c in z]


def test_framing_coverage_known_numbers() -> None:
    # 50 mm on a 36 mm sensor, 16:9: half-extent at 1 m is 0.36 x 0.2025. A 2x2 square
    # 10 m ahead spans 0.2/0.72 of the width and 0.2/0.405 of the height.
    got = _coverage(_box((-1, 1), (-1, 1), (-10, -10)))
    assert got["coverage_pct"] == pytest.approx(0.2 / 0.72 * 0.2 / 0.405 * 100, abs=1e-9)
    assert got["in_frame_pct"] == pytest.approx(100.0)
    assert got["bbox_ndc"] == pytest.approx(
        [0.5 - 0.1 / 0.72, 0.5 - 0.1 / 0.405, 0.5 + 0.1 / 0.72, 0.5 + 0.1 / 0.405]
    )


def test_framing_coverage_clips_to_frame() -> None:
    # x from 0 to 10 at 10 m runs off the right edge: clipped width is half the frame.
    got = _coverage(_box((0, 10), (-1, 1), (-10, -10)))
    assert got["coverage_pct"] == pytest.approx(0.5 * 0.2 / 0.405 * 100, abs=1e-9)
    assert got["in_frame_pct"] < 100


def test_framing_coverage_vertical_fit_and_behind_camera() -> None:
    # Vertical fit: 24 mm sensor height spans the frame height; a 9:16 portrait frame.
    got = _coverage(_box((-1, 1), (-1, 1), (-10, -10)), fit="VERTICAL", rx=1080, ry=1920)
    half_h = 24 / 100
    half_w = half_h * 1080 / 1920
    assert got["coverage_pct"] == pytest.approx((0.1 / half_w) * (0.1 / half_h) * 100, rel=1e-9)
    behind = _coverage(_box((-1, 1), (-1, 1), (5, 6)))
    assert behind == {"coverage_pct": 0.0, "in_frame_pct": 0.0, "bbox_ndc": None}


def test_framing_coverage_straddling_camera_plane_is_clipped() -> None:
    # A box from 1 m behind to 10 m in front fills the frame instead of blowing up.
    got = _coverage(_box((-1, 1), (-1, 1), (-10, 1)))
    assert got["coverage_pct"] == pytest.approx(100.0)


def test_framing_with_rotated_camera_matches_blender_convention() -> None:
    # Camera at -Y rotated 90 deg about X looks along +Y (Blender's front view).
    got = _coverage(
        _box((-1, 1), (9, 11), (-1, 1)), loc=(0, -10, 0), rot=(1.5707963267948966, 0, 0)
    )
    assert got["bbox_ndc"][0] + got["bbox_ndc"][2] == pytest.approx(1.0)
    assert got["coverage_pct"] > 0


@pytest.mark.parametrize("angle", ["front", "three_quarter", "top"])
@pytest.mark.parametrize("aspect", [16 / 9, 9 / 16, 1.0])
def test_auto_frame_keeps_bounds_in_frame(angle: str, aspect: float) -> None:
    rx, ry = (1920, round(1920 / aspect)) if aspect >= 1 else (round(1920 * aspect), 1920)
    got = pure(
        "from bpy_runtime.ops.look import auto_frame\n"
        "from bpy_runtime.ops.inspect import euler_matrix, project_point\n"
        f"loc, rot = auto_frame((-1, -2, 0), (1, 2, 3), {angle!r}, 50, 36, {aspect!r})\n"
        "m = euler_matrix(loc, rot)\n"
        f"pts = [project_point(m, 50, 36, 24, 'AUTO', {rx}, {ry}, (x, y, z))"
        " for x in (-1, 1) for y in (-2, 2) for z in (0, 3)]\n"
        "print(json.dumps(pts))\n"
    )
    xs, ys, depths = zip(*got, strict=True)
    assert min(depths) > 0
    assert 0 < min(xs) and max(xs) < 1 and 0 < min(ys) and max(ys) < 1
    assert (min(xs) + max(xs)) / 2 == pytest.approx(0.5, abs=0.1)


def test_aspect_resolution_is_even() -> None:
    got = pure(
        "from bpy_runtime.ops.camera import aspect_resolution\n"
        "print(json.dumps([aspect_resolution(a, 1920) for a in ('16:9', '9:16', '1:1', '4:5')]))\n"
    )
    assert got == [[1920, 1080], [1080, 1920], [1920, 1920], [1536, 1920]]


# --- registry, safety -----------------------------------------------------------------

NEW_TOOLS = {
    "object.import_gltf",
    "material.apply_preset",
    "camera.set_dof",
    "camera.move",
    "camera.add_aspect",
    "camera.use",
    "render.set_property",
    "inspect.bounds",
    "inspect.framing",
    "look.set_view",
}


def test_registry_literal_matches_tools() -> None:
    got = pure(
        "from bpy_runtime.tools import TOOLS, TOOL_NAMES\n"
        "print(json.dumps([sorted(TOOLS), TOOL_NAMES == frozenset(TOOLS)]))\n"
    )
    names, same = got
    assert same is True
    assert set(names) == runtime_tool_names(RUNTIME / "tools.py")
    assert NEW_TOOLS <= set(names) and len(names) == 18


def test_error_codes_are_stable() -> None:
    got = pure(
        "from bpy_runtime.ops import ERROR_CODES, ToolError\n"
        "from bpy_runtime.tools import ToolError as T2\n"
        "print(json.dumps([list(ERROR_CODES), ToolError('x').code, T2 is ToolError]))\n"
    )
    assert got == [
        [
            "args.invalid",
            "tool.unknown",
            "scene.no_camera",
            "scene.object_not_found",
            "asset.path_refused",
            "framing.subject_out_of_frame",
        ],
        "args.invalid",
        True,
    ]


def test_runtime_sources_have_no_dynamic_code() -> None:
    sources = sorted(RUNTIME.rglob("*.py"))
    assert RUNTIME / "ops" / "props.py" in sources
    for path in sources:
        text = path.read_text()
        assert "import spectracut" not in text and "from spectracut" not in text, path
        assert not re.search(r"\b(eval|exec|compile|__import__)\(", text), path
    props = (RUNTIME / "ops" / "props.py").read_text()
    assert "getattr" not in props and "setattr" not in props


# --- ops through the fake blender ------------------------------------------------------


def test_ops_run_and_write_report(blender: Path, tmp_path: Path) -> None:
    run = run_steps(
        blender,
        tmp_path,
        [
            ("scene.clear", {}),
            CAM,
            CUBE,
            ("material.apply_preset", {"object": "Box", "preset": "glass", "color": [1, 0, 0]}),
            ("camera.set_dof", {"focus_object": "Box", "fstop": 2.8}),
            (
                "camera.move",
                {
                    "keys": [
                        {"frame": 1, "location": [0, -10, 0]},
                        {"frame": 48, "location": [0, -8, 1], "lens": 85},
                    ],
                    "easing": "ease_in",
                },
            ),
            ("camera.add_aspect", {"aspect": "9:16"}),
            ("camera.use", {"name": "Cam.9x16"}),
            ("render.set_property", {"property": "view_settings.exposure", "value": 0.5}),
            ("render.set_property", {"property": "render.film_transparent", "value": True}),
            ("inspect.bounds", {}),
            ("look.set_view", {"shading": "material", "angle": "front"}),
            ("inspect.framing", {"object": "Box", "min_coverage_pct": 1}),
        ],
    )
    assert run.proc.returncode == 0, run.proc.stderr
    tools = [s["tool"] for s in run.reports]
    assert "scene.clear" not in tools  # tools without a report are not listed
    assert json.loads((run.out / "report.json").read_text())["steps"][0]["index"] == 3
    assert run.report("material.apply_preset") == {
        "material": "glass.Box",
        "object": "Box",
        "preset": "glass",
    }
    assert run.report("camera.set_dof") == {
        "camera": "Cam",
        "enabled": True,
        "focus_object": "Box",
        "fstop": 2.8,
    }
    assert run.report("camera.move")["frames"] == [1, 48]
    assert run.report("camera.add_aspect") == {
        "aspect": "9:16",
        "camera": "Cam.9x16",
        "source": "Cam",
    }
    assert run.report("camera.use") == {
        "aspect": "9:16",
        "camera": "Cam.9x16",
        "resolution": [1080, 1920],
    }
    assert run.report("inspect.bounds") == {
        "min": [-1.0, -1.0, -1.0],
        "max": [1.0, 1.0, 1.0],
        "center": [0.0, 0.0, 0.0],
        "size": [2.0, 2.0, 2.0],
    }
    look = run.report("look.set_view")
    assert look["shading"] == "material" and look["engine"] == "BLENDER_WORKBENCH"
    assert look["camera"]["name"] == "Cam.9x16"
    framing = run.report("inspect.framing")
    assert 0 < framing["coverage_pct"] <= 100 and framing["meets_min"] is True
    state = run.state
    assert state["camera"] == "Cam.9x16" and state["resolution"] == [1080, 1920]
    keys = [e for e in state["events"] if e[0] == "key"]
    assert ["key", "Cam", {"data_path": "location", "frame": 48}] in keys
    calls = state["calls"]
    assert any("keyframe_insert(data_path='lens', frame=48)" in c for c in calls)
    assert any("inputs.__getitem__('Transmission Weight')" in c for c in calls)
    assert any("render.render(animation=True)" in c for c in calls)


def test_import_gltf_from_assets_dir(blender: Path, tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    (assets / "devices").mkdir(parents=True)
    (assets / "devices" / "phone.glb").write_bytes(b"glTF")
    run = run_steps(
        blender,
        tmp_path,
        [("object.import_gltf", {"path": "devices/phone.glb", "location": [1, 2, 3]})],
        assets_dir=assets,
    )
    assert run.proc.returncode == 0, run.proc.stderr
    assert run.report("object.import_gltf") == {
        "objects": ["Phone", "Phone.Screen"],
        "top_level": ["Phone"],
    }
    assert ["import_gltf", str((assets / "devices" / "phone.glb").resolve())] in run.state["events"]
    assert run.state["objects"]["Phone"]["location"] == [1.0, 2.0, 3.0]


@pytest.mark.parametrize(
    "path",
    [
        "/etc/phone.glb",
        "../phone.glb",
        "devices/../../phone.glb",
        "C:\\phone.glb",
        "link/x.glb",
        "devices/missing.glb",
        "devices/phone.blend",
    ],
)
def test_import_gltf_refuses_paths_outside_assets(blender: Path, tmp_path: Path, path: str) -> None:
    assets = tmp_path / "assets"
    (assets / "devices").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.glb").write_bytes(b"glTF")
    (assets / "link").symlink_to(outside)
    run = run_steps(blender, tmp_path, [("object.import_gltf", {"path": path})], assets_dir=assets)
    assert run.proc.returncode == 1
    assert "[asset.path_refused]" in run.proc.stderr
    assert not any("import_scene.gltf" in c for c in run.state.get("calls", []))


def test_import_gltf_refuses_without_assets_dir(blender: Path, tmp_path: Path) -> None:
    run = run_steps(blender, tmp_path, [("object.import_gltf", {"path": "a.glb"})])
    assert run.proc.returncode == 1
    assert "[asset.path_refused]" in run.proc.stderr and "SPECTRACUT_ASSETS_DIR" in run.proc.stderr


@pytest.mark.parametrize(
    ("steps", "code"),
    [
        ([("python.exec", {"code": "print(1)"})], "tool.unknown"),
        ([("camera.set_lens", {"mm": "35; import os"})], "args.invalid"),
        (
            [("render.set_property", {"property": "render.filepath", "value": "/tmp/x"})],
            "args.invalid",
        ),
        ([("render.set_property", {"property": "__class__", "value": 1})], "args.invalid"),
        ([("render.set_property", {"property": "cycles.samples", "value": 1.5})], "args.invalid"),
        (
            [CAM, ("camera.move", {"keys": [{"frame": 5, "lens": 50}, {"frame": 5, "lens": 60}]})],
            "args.invalid",
        ),
        ([("camera.set_dof", {"fstop": 2})], "scene.no_camera"),
        ([("inspect.framing", {"object": "Box"})], "scene.no_camera"),
        ([CAM, ("camera.use", {"name": "Nope"})], "scene.object_not_found"),
        (
            [CAM, ("material.apply_preset", {"object": "Nope", "preset": "rubber"})],
            "scene.object_not_found",
        ),
        ([("look.set_view", {"shading": "solid", "angle": "top"})], "scene.object_not_found"),
        (
            [
                CAM,
                CUBE,
                ("camera.set_transform", {"rotation": [0, 0, 0]}),
                ("inspect.framing", {"object": "Box"}),
            ],
            "framing.subject_out_of_frame",
        ),
        ([("look.set_view", {"shading": "solid", "engine": "cycles"})], "args.invalid"),
    ],
)
def test_refusals_print_stable_codes(
    blender: Path, tmp_path: Path, steps: list[tuple[str, dict[str, Any]]], code: str
) -> None:
    run = run_steps(blender, tmp_path, steps)
    assert run.proc.returncode == 1
    assert f"[{code}]" in run.proc.stderr, run.proc.stderr
    assert not any("render.render" in c for c in run.state.get("calls", []))
    assert not (run.out / "report.json").exists()


def test_no_report_file_without_reports(blender: Path, tmp_path: Path) -> None:
    run = run_steps(blender, tmp_path, [CAM, CUBE])
    assert run.proc.returncode == 0, run.proc.stderr
    assert sorted(p.name for p in run.out.iterdir()) == ["frame_0001.png"]


def test_look_rendered_and_auto_camera(blender: Path, tmp_path: Path) -> None:
    run = run_steps(
        blender,
        tmp_path,
        [CUBE, ("look.set_view", {"shading": "rendered", "engine": "cycles", "angle": "top"})],
    )
    assert run.proc.returncode == 0, run.proc.stderr
    look = run.report("look.set_view")
    assert look["engine"] == "CYCLES" and look["angle"] == "top"
    assert look["camera"]["name"] == "LookCam"
    assert look["camera"]["rotation"] == pytest.approx([0, 0, 0])
    assert look["camera"]["location"][:2] == pytest.approx([0, 0])
    assert run.state["camera"] == "LookCam"


# --- guard hands the assets root over in the manifest --------------------------------


def test_guard_puts_assets_dir_in_manifest(
    keypair: tuple[str, str], sign: Any, tmp_path: Path
) -> None:
    guard = ExecutionGuard(
        HostRegistration(host_id="legion", control_plane_public_key=keypair[1]),
        RenderProfile(blender_version="4.5.3", gpu_model="NVIDIA GeForce RTX 4070 Ti"),
        runtime=RUNTIME,
    )
    seen: list[dict[str, Any]] = []

    def launcher(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append({"manifest": json.loads(Path(cmd[-1]).read_text()), "env": kwargs["env"]})
        return subprocess.CompletedProcess(cmd, 0, "", "")

    envelope, frames = guard.check(sign())
    assets = tmp_path / "assets"
    env = {"PATH": "/usr/bin", "SPECTRACUT_ASSETS_DIR": str(assets)}
    guard.launch(envelope, frames, tmp_path / "a", env=env, launcher=launcher)
    guard.launch(envelope, frames, tmp_path / "b", env={"PATH": "/usr/bin"}, launcher=launcher)
    assert seen[0]["manifest"]["assets_dir"] == str(assets.resolve())
    assert seen[1]["manifest"]["assets_dir"] is None
    assert set(seen[0]["env"]) == {"PATH", "SPECTRACUT_EXECUTION_TOKEN"}
