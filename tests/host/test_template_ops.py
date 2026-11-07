# SPDX-License-Identifier: AGPL-3.0-or-later
"""Template runtime ops (#198): device rig, object animation, text, HDRI world, passes.

Pure checks run in a separate ``python -I`` interpreter with only ``host/`` on the
path (the control plane refuses ``bpy_runtime`` imports in this process). The
ops run through a fake ``blender`` whose fake ``bpy`` keeps named objects and mesh
faces on top of a recording ``MagicMock``, the pattern of ``test_bpy_ops.py``.
"""

from __future__ import annotations

import hashlib
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
HOST = ROOT / "host"
RUNTIME = HOST / "bpy_runtime"
RUNNER = RUNTIME / "runner.py"
TEMPLATE_TOOLS = {"device.add", "object.animate", "text.add", "world.set_hdri"}

FAKE_BPY = r"""
import atexit, json, os
from types import SimpleNamespace
from unittest.mock import MagicMock

_bpy = MagicMock(name="bpy")
_bpy.app.version = (4, 5, 3)
_objects = []
_events = []
_CUBE_FACES = [(1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)]


def _new_object(name, data=None):
    o = MagicMock(name="obj_" + name)
    o.name = name
    o.type = "MESH"
    o.data = data if data is not None else MagicMock(name="meshdata_" + name)
    o.parent = None
    o.location, o.rotation_euler, o.scale = (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (1.0, 1.0, 1.0)
    o.keyframe_insert.side_effect = lambda **kw: _events.append(
        ["key", o.name, kw["data_path"], kw["frame"],
         list(getattr(o, kw["data_path"]))]
    )
    o.modifiers.new.side_effect = lambda **kw: (
        _events.append(["modifier", o.name, kw["name"], kw["type"]]) or MagicMock(name="mod")
    )
    _objects.append(o)
    return o


def _get(name):
    return next((o for o in _objects if o.name == name), None)


def _remove(obj, do_unlink=True):
    _objects.remove(obj)


def _primitive(location=(0, 0, 0)):
    data = MagicMock(name="cubedata")
    data.polygons = [SimpleNamespace(center=c, select=False) for c in _CUBE_FACES]
    o = _new_object("Cube", data)
    o.location = tuple(location)
    _bpy.context.active_object = o


def _separate(type="SELECTED"):
    active = _bpy.context.view_layer.objects.active
    picked = [p for p in active.data.polygons if p.select]
    data = MagicMock(name="piecedata")
    data.polygons = [SimpleNamespace(center=p.center, select=False) for p in picked]
    _events.append(["separate", active.name, len(picked)])
    _new_object(active.name + ".001", data)


def _material(name):
    m = MagicMock(name="mat_" + name)
    m.name = name
    return m


_bpy.data.objects.new.side_effect = _new_object
_bpy.data.objects.get.side_effect = _get
_bpy.data.objects.remove.side_effect = _remove
_bpy.data.objects.__iter__.side_effect = lambda: iter(list(_objects))
_bpy.data.materials.new.side_effect = _material
_bpy.data.node_groups.get.return_value = None
_bpy.ops.mesh.primitive_cube_add.side_effect = _primitive
_bpy.ops.mesh.separate.side_effect = _separate
_bpy.context.scene.camera = None


def _dump():
    record = os.environ.get("FAKE_BPY_RECORD")
    if record:
        state = {
            "calls": [str(c) for c in _bpy.mock_calls],
            "events": _events,
            "objects": sorted(o.name for o in _objects),
            "texts": {
                o.name: {k: getattr(o.data, k) for k in ("body", "align_x", "align_y")}
                for o in _objects
                if isinstance(getattr(o.data, "body", None), str)
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

    def report(self, tool: str) -> dict[str, Any]:
        steps = json.loads((self.out / "report.json").read_text())["steps"]
        found: dict[str, Any] = next(s["report"] for s in steps if s["tool"] == tool)
        return found

    @property
    def calls(self) -> list[str]:
        calls: list[str] = self.state.get("calls", [])
        return calls


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


def pure(code: str, *argv: str) -> Any:
    """Run ``code`` with only ``host/`` importable and return its printed JSON."""
    prelude = "import json, sys; sys.path.insert(0, sys.argv[1])\n"
    result = subprocess.run(
        [sys.executable, "-I", "-c", prelude + code, str(HOST), *argv],
        capture_output=True,
        text=True,
        check=False,
        env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"},
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


FRAME = b"\x89PNG fake ui frame "
CUBE = ("object.add_primitive", {"kind": "cube", "name": "Box"})


def _assets(tmp_path: Path) -> Path:
    assets = tmp_path / "assets"
    (assets / "ui").mkdir(parents=True)
    for i in range(1, 4):
        (assets / "ui" / f"home_{i:04d}.png").write_bytes(FRAME + bytes([i]))
    (assets / "ui" / "demo.mp4").write_bytes(b"movie")
    (assets / "fonts").mkdir()
    (assets / "fonts" / "Brand.ttf").write_bytes(b"font")
    (assets / "hdri").mkdir()
    (assets / "hdri" / "studio.hdr").write_bytes(b"hdr")
    return assets


def _frames(n: int = 3) -> list[dict[str, str]]:
    return [
        {"path": f"ui/home_{i:04d}.png", "sha256": sha(FRAME + bytes([i]))} for i in range(1, n + 1)
    ]


# --- R14: registry and guard ------------------------------------------------------------


def test_registry_and_guard_list_the_template_ops() -> None:
    names, same = pure(
        "from bpy_runtime.tools import TOOLS, TOOL_NAMES\n"
        "print(json.dumps([sorted(TOOLS), TOOL_NAMES == frozenset(TOOLS)]))\n"
    )
    assert same is True
    assert TEMPLATE_TOOLS <= set(names)
    assert TEMPLATE_TOOLS <= runtime_tool_names(RUNTIME / "tools.py")
    assert set(names) == runtime_tool_names(RUNTIME / "tools.py")


# --- R11: validation before bpy -----------------------------------------------------------

_BOOM = (
    "from bpy_runtime.ops import ToolError\n"
    "from bpy_runtime.ops.scene import set_context\n"
    "from bpy_runtime.tools import TOOLS\n"
    "class Boom:\n"
    "    def __getattr__(self, name):\n"
    "        raise AssertionError('bpy touched: ' + name)\n"
    "set_context(sys.argv[2])\n"
    "out = []\n"
    "for tool, args in json.loads(sys.argv[3]):\n"
    "    try:\n"
    "        TOOLS[tool](Boom(), args)\n"
    "        out.append([tool, 'ran'])\n"
    "    except ToolError as exc:\n"
    "        out.append([tool, exc.code])\n"
    "print(json.dumps(out))\n"
)

_SCREEN = {"frames": [{"path": "ui/home_0001.png", "sha256": sha(FRAME + b"\x01")}]}
_HDRI = {"path": "hdri/studio.hdr", "sha256": sha(b"hdr")}
_BAD_ARGS: list[tuple[str, dict[str, Any]]] = [
    ("device.add", {"kind": "phone", "screen": _SCREEN, "script": "x"}),
    ("device.add", {"kind": "tablet", "screen": _SCREEN}),
    ("device.add", {"kind": "phone"}),
    ("device.add", {"kind": "phone", "screen": _SCREEN, "emission_strength": -1}),
    ("device.add", {"kind": "phone", "screen": {**_SCREEN, "movie": _HDRI}}),
    ("device.add", {"kind": "phone", "screen": {"frames": [{"path": "ui/a.png"}]}}),
    ("device.add", {"kind": "phone", "screen": _SCREEN, "grid": {"resolution": [1, 2]}}),
    ("device.add", {"kind": "phone", "screen": _SCREEN, "bevel": 0.5}),
    ("device.add", {"kind": "phone", "screen": _SCREEN, "finish": "chrome-logo"}),
    (
        "object.animate",
        {"object": "Box", "preset": "turntable", "frame_start": 1, "frame_end": 24, "eval": 1},
    ),
    ("object.animate", {"object": "Box", "preset": "spin", "frame_start": 1, "frame_end": 24}),
    ("object.animate", {"object": "Box", "preset": "turntable", "frame_start": 24, "frame_end": 1}),
    (
        "object.animate",
        {"object": "Box", "preset": "turntable", "frame_start": 1, "frame_end": 24, "pieces": 4},
    ),
    (
        "object.animate",
        {"object": "Box", "preset": "shatter", "frame_start": 1, "frame_end": 24, "pieces": 1},
    ),
    (
        "object.animate",
        {
            "object": "Box",
            "preset": "reveal",
            "frame_start": 1,
            "frame_end": 24,
            "from": "position",
        },
    ),
    ("text.add", {"body": "Hi", "python": "x"}),
    ("text.add", {"body": ""}),
    ("text.add", {"body": "Hi", "mode": "cutout"}),
    ("text.add", {"body": "Hi", "plate": "Box"}),
    ("text.add", {"body": "Hi", "size": 0}),
    ("text.add", {"body": "Hi", "font": {"path": "fonts/Brand.ttf", "sha256": "XYZ"}}),
    ("world.set_hdri", {**_HDRI, "url": "http://x"}),
    ("world.set_hdri", {**_HDRI, "strength": -1}),
    ("world.set_hdri", {**_HDRI, "rotation": 720}),
    ("world.set_hdri", {"path": "hdri/studio.hdr", "sha256": "0"}),
]


def test_each_new_op_refuses_bad_args_before_touching_bpy(tmp_path: Path) -> None:
    assets = _assets(tmp_path)
    got = pure(_BOOM, str(assets), json.dumps(_BAD_ARGS))
    assert got == [[tool, "args.invalid"] for tool, _ in _BAD_ARGS]


def test_asset_refusals_happen_before_bpy(tmp_path: Path) -> None:
    assets = _assets(tmp_path)
    steps = [
        (
            "device.add",
            {"kind": "phone", "screen": {"frames": [_frames()[0] | {"sha256": "0" * 64}]}},
        ),
        ("text.add", {"body": "Hi", "font": {"path": "fonts/Brand.ttf", "sha256": "0" * 64}}),
        ("world.set_hdri", {**_HDRI, "sha256": "0" * 64}),
        ("world.set_hdri", {**_HDRI, "path": "../studio.hdr"}),
    ]
    got = pure(_BOOM, str(assets), json.dumps(steps))
    assert [code for _, code in got] == [
        "asset.hash_mismatch",
        "asset.hash_mismatch",
        "asset.hash_mismatch",
        "asset.path_refused",
    ]


# --- R3: moire fade and R5: deterministic shatter (pure maths) ---------------------------


def test_grid_fade_goes_flat_before_half_a_cell() -> None:
    got = pure(
        "from bpy_runtime.ops.device import grid_fade\n"
        "xs = [i / 100 for i in range(0, 101)]\n"
        "print(json.dumps([grid_fade(x) for x in xs]))\n"
    )
    assert got[0] == 1.0 and got[25] == 1.0
    assert got[50] == 0.0 and all(v == 0.0 for v in got[50:])
    assert all(a >= b for a, b in zip(got, got[1:], strict=False))
    assert 0.0 < got[37] < 1.0


def test_shatter_plan_is_deterministic_per_seed() -> None:
    a, b, c = pure(
        "from bpy_runtime.ops.animate import shatter_plan\n"
        "centers = [(x * 0.1, y * 0.1, (x * y) % 3 * 0.1) for x in range(6) for y in range(6)]\n"
        "print(json.dumps([shatter_plan(centers, 5, 7, 2.0), shatter_plan(centers, 5, 7, 2.0),"
        " shatter_plan(centers, 5, 8, 2.0)]))\n"
    )
    assert a == b and a != c
    assert len(a["seeds"]) == len(a["offsets"]) == len(a["spins"]) == 5
    for offset in a["offsets"]:
        length = sum(v * v for v in offset) ** 0.5
        assert 1.2 - 1e-6 <= length <= 2.0 + 1e-6


def test_sequence_frames_must_be_consecutive() -> None:
    got = pure(
        "from bpy_runtime.ops import ToolError\n"
        "from bpy_runtime.ops.device import check_sequence\n"
        "out = []\n"
        "for paths in (['a/f_0001.png', 'a/f_0002.png'], ['a/f_0001.png', 'a/f_0003.png'],"
        " ['a/f_0001.png', 'b/f_0002.png'], ['a/f_1.png', 'a/f_02.png'], ['a/x.png', 'a/y.png']):\n"
        "    try:\n"
        "        check_sequence(paths); out.append('ok')\n"
        "    except ToolError as exc:\n"
        "        out.append(exc.code)\n"
        "print(json.dumps(out))\n"
    )
    assert got == ["ok", "args.invalid", "args.invalid", "args.invalid", "args.invalid"]


# --- R1-R4, R12: device.add through the fake blender --------------------------------------


@pytest.mark.parametrize("kind", ["phone", "laptop"])
def test_device_add_builds_rig_with_sequence_screen(
    blender: Path, tmp_path: Path, kind: str
) -> None:
    assets = _assets(tmp_path)
    step = {
        "kind": kind,
        "name": "Dev",
        "screen": {"frames": _frames(), "frame_offset": 4},
        "emission_strength": 3,
    }
    run = run_steps(blender, tmp_path, [("device.add", step)], assets_dir=assets)
    assert run.proc.returncode == 0, run.proc.stderr
    report = run.report("device.add")
    parts = ["Dev.body", "Dev.bezel", "Dev.screen", "Dev.glass"]
    assert report == {
        "device": "Dev.body",
        "kind": kind,
        "objects": parts
        + (["Dev.base", "Dev.keyboard", "Dev.trackpad"] if kind == "laptop" else []),
        "screen_object": "Dev.screen",
        "screen_material": "screen.Dev",
        "node_group": "SC_Screen",
        "emission_strength": 3.0,
        "frames": 3,
        "frame_offset": 4,
        "bevel": {"phone": 0.004, "laptop": 0.003}[kind],
        "finish": "aluminium",
    }
    calls = run.calls
    # Higher fidelity: a bevelled shell (and deck), no text anywhere.
    assert ["modifier", "Dev.body", "bevel", "BEVEL"] in run.state["events"]
    first = str((assets / "ui" / "home_0001.png").resolve())
    assert any(f"images.load({first!r}, check_existing=True)" in c for c in calls), calls
    assert sum("images.load" in c for c in calls) == 1
    assert any("node_groups.new('SC_Screen', 'ShaderNodeTree')" in c for c in calls)
    for node in ("ShaderNodeCameraData", "ShaderNodeMapRange", "ShaderNodeEmission"):
        assert any(f"nodes.new('{node}')" in c for c in calls), node
    # No text, logo or image other than the screen UI anywhere on the rig.
    assert not any("curves.new" in c or "fonts.load" in c for c in calls)


def test_device_add_movie_screen(blender: Path, tmp_path: Path) -> None:
    assets = _assets(tmp_path)
    step = {"kind": "phone", "screen": {"movie": {"path": "ui/demo.mp4", "sha256": sha(b"movie")}}}
    run = run_steps(blender, tmp_path, [("device.add", step)], assets_dir=assets)
    assert run.proc.returncode == 0, run.proc.stderr
    report = run.report("device.add")
    assert report["movie"] == "ui/demo.mp4" and report["screen_object"] == "Phone.screen"


@pytest.mark.parametrize(
    ("frames", "code"),
    [
        ([{"path": "ui/home_0001.png", "sha256": "0" * 64}], "asset.hash_mismatch"),
        (
            _frames(2)[:1] + [{"path": "ui/home_0002.png", "sha256": "1" * 64}],
            "asset.hash_mismatch",
        ),
        ([{"path": "../home_0001.png", "sha256": "0" * 64}], "asset.path_refused"),
        ([{"path": "/etc/home_0001.png", "sha256": "0" * 64}], "asset.path_refused"),
        ([{"path": "ui/missing_0001.png", "sha256": "0" * 64}], "asset.path_refused"),
        ([{"path": "ui/demo.mp4", "sha256": sha(b"movie")}], "asset.path_refused"),
    ],
)
def test_device_add_refuses_bad_screen_frames(
    blender: Path, tmp_path: Path, frames: list[dict[str, str]], code: str
) -> None:
    assets = _assets(tmp_path)
    (tmp_path / "home_0001.png").write_bytes(FRAME + b"\x01")
    step = ("device.add", {"kind": "phone", "screen": {"frames": frames}})
    run = run_steps(blender, tmp_path, [step], assets_dir=assets)
    assert run.proc.returncode == 1
    assert f"[{code}]" in run.proc.stderr, run.proc.stderr
    assert not any("images.load" in c or "objects.new" in c for c in run.calls)


def test_device_add_refuses_without_assets_dir(blender: Path, tmp_path: Path) -> None:
    step = ("device.add", {"kind": "phone", "screen": {"frames": _frames(1)}})
    run = run_steps(blender, tmp_path, [step])
    assert run.proc.returncode == 1 and "[asset.path_refused]" in run.proc.stderr


# --- R5: object.animate -------------------------------------------------------------------


def _keys(run: Run, path: str) -> list[list[Any]]:
    return [e for e in run.state["events"] if e[0] == "key" and e[2] == path]


def test_turntable_and_reveal_keys(blender: Path, tmp_path: Path) -> None:
    turn = {
        "object": "Box",
        "preset": "turntable",
        "frame_start": 1,
        "frame_end": 48,
        "degrees": 180,
        "axis": "z",
    }
    reveal = {
        "object": "Box",
        "preset": "reveal",
        "frame_start": 10,
        "frame_end": 20,
        "from": "position",
        "offset": [0, 0, -2],
    }
    grow = {"object": "Box", "preset": "reveal", "frame_start": 1, "frame_end": 5}
    steps: list[tuple[str, dict[str, Any]]] = [
        CUBE,
        ("object.animate", turn),
        ("object.animate", reveal),
        ("object.animate", grow),
    ]
    run = run_steps(blender, tmp_path, steps)
    assert run.proc.returncode == 0, run.proc.stderr
    rot = _keys(run, "rotation_euler")
    assert [(k[3], round(k[4][2], 5)) for k in rot] == [(1, 0.0), (48, 3.14159)]
    loc = _keys(run, "location")
    assert [(k[3], k[4]) for k in loc] == [(10, [0.0, 0.0, -2.0]), (20, [0.0, 0.0, 0.0])]
    scale = _keys(run, "scale")
    assert [k[3] for k in scale] == [1, 5] and scale[0][4][0] < 0.001 and scale[1][4][0] == 1.0


def test_shatter_and_assemble_split_seeded(blender: Path, tmp_path: Path) -> None:
    args = {"object": "Box", "frame_start": 1, "frame_end": 30, "pieces": 3, "seed": 11}
    run = run_steps(blender, tmp_path, [CUBE, ("object.animate", {**args, "preset": "shatter"})])
    assert run.proc.returncode == 0, run.proc.stderr
    report = run.report("object.animate")
    assert report["pieces"] == ["Box", "Box.piece.001", "Box.piece.002"]
    assert report["seed"] == 11
    assert not any("cell_fracture" in c for c in run.calls)
    shatter = [(k[1], k[3], k[4]) for k in _keys(run, "location")]
    assert len(shatter) == 6 and all(frame in (1, 30) for _, frame, _ in shatter)
    whole = {n: loc for n, f, loc in shatter if f == 1}
    assert all(loc == [0.0, 0.0, 0.0] for loc in whole.values())

    again = tmp_path / "again"
    again.mkdir()
    rerun = run_steps(blender, again, [CUBE, ("object.animate", {**args, "preset": "shatter"})])
    assert _keys(rerun, "location") == _keys(run, "location")

    back = tmp_path / "back"
    back.mkdir()
    rev = run_steps(blender, back, [CUBE, ("object.animate", {**args, "preset": "assemble"})])
    assert rev.proc.returncode == 0, rev.proc.stderr
    assembled = {
        (n, loc[0], loc[1], loc[2])
        for n, f, loc in [(k[1], k[3], k[4]) for k in _keys(rev, "location")]
        if f == 30
    }
    assert all(x == y == z == 0.0 for _, x, y, z in assembled)


def test_animate_unknown_object(blender: Path, tmp_path: Path) -> None:
    step = (
        "object.animate",
        {"object": "Nope", "preset": "turntable", "frame_start": 1, "frame_end": 2},
    )
    run = run_steps(blender, tmp_path, [step])
    assert run.proc.returncode == 1 and "[scene.object_not_found]" in run.proc.stderr


# --- R6: text.add -------------------------------------------------------------------------


def test_text_depth_with_pinned_font(blender: Path, tmp_path: Path) -> None:
    assets = _assets(tmp_path)
    step = {
        "body": "Ship it",
        "name": "Title",
        "size": 0.5,
        "extrude": 0.05,
        "font": {"path": "fonts/Brand.ttf", "sha256": sha(b"font")},
    }
    run = run_steps(blender, tmp_path, [("text.add", step)], assets_dir=assets)
    assert run.proc.returncode == 0, run.proc.stderr
    assert run.report("text.add") == {"mode": "depth", "font": "fonts/Brand.ttf", "object": "Title"}
    font = str((assets / "fonts" / "Brand.ttf").resolve())
    assert any(f"fonts.load({font!r}, check_existing=True)" in c for c in run.calls)
    assert any("curves.new('Title', type='FONT')" in c for c in run.calls)


def test_text_cutout_brand_font_cuts_the_plate(blender: Path, tmp_path: Path) -> None:
    step = {"body": "GO", "name": "Cut", "mode": "cutout", "plate": "Box", "extrude": 0.5}
    run = run_steps(blender, tmp_path, [CUBE, ("text.add", step)])
    assert run.proc.returncode == 0, run.proc.stderr
    assert run.report("text.add") == {
        "mode": "cutout",
        "font": "Inter-Regular.ttf",
        "plate": "Box",
        "modifier": "cutout.Cut",
        "object": "Cut",
    }
    assert ["modifier", "Box", "cutout.Cut", "BOOLEAN"] in run.state["events"]
    assert any("new_from_object" in c for c in run.calls)


# #332: text.add loads the host-shipped brand font, sets the body verbatim and aligns.
API_BODY = 'POST /api/v1/scrape {"max_tier": 3}'
BRAND_FONT = (RUNTIME / "fonts" / "Inter-Regular.ttf").resolve()


def test_text_left_align_sets_the_body_verbatim(blender: Path, tmp_path: Path) -> None:
    step = {"body": API_BODY, "name": "Req", "align": "left", "bevel": 0.01}
    run = run_steps(blender, tmp_path, [("text.add", step)])
    assert run.proc.returncode == 0, run.proc.stderr
    assert run.state["texts"]["Req"] == {"body": API_BODY, "align_x": "LEFT", "align_y": "CENTER"}
    assert run.report("text.add") == {
        "mode": "depth",
        "font": "Inter-Regular.ttf",
        "align": "left",
        "object": "Req",
    }


def test_text_without_align_stays_centred(blender: Path, tmp_path: Path) -> None:
    run = run_steps(blender, tmp_path, [("text.add", {"body": "ONE BEAM", "name": "T"})])
    assert run.proc.returncode == 0, run.proc.stderr
    assert run.state["texts"]["T"]["align_x"] == "CENTER"


@pytest.mark.parametrize(
    ("weight", "file"), [(None, "Inter-Regular.ttf"), (600, "Inter-SemiBold.ttf")]
)
def test_text_without_font_loads_the_shipped_brand_font(
    blender: Path, tmp_path: Path, weight: int | None, file: str
) -> None:
    step: dict[str, Any] = {"body": "Hi", "name": "T"}
    if weight is not None:
        step["weight"] = weight
    run = run_steps(blender, tmp_path, [("text.add", step)])
    assert run.proc.returncode == 0, run.proc.stderr
    font = str((RUNTIME / "fonts" / file).resolve())
    assert any(f"fonts.load({font!r}, check_existing=True)" in c for c in run.calls)
    assert run.report("text.add")["font"] == file


def test_brand_font_missing_or_altered_fails_with_a_clear_code(tmp_path: Path) -> None:
    (tmp_path / "altered").mkdir()
    (tmp_path / "altered" / "Inter-Regular.ttf").write_bytes(b"not inter")
    code = (
        "from bpy_runtime.ops import ToolError\n"
        "from bpy_runtime.ops.text import brand_font\n"
        "out = {}\n"
        "for d in sys.argv[2:]:\n"
        "    try:\n"
        "        brand_font(400, d)\n"
        "    except ToolError as exc:\n"
        "        out[d] = [exc.code, str(exc)]\n"
        "print(json.dumps(out))\n"
    )
    empty, altered = str(tmp_path / "empty"), str(tmp_path / "altered")
    got = pure(code, empty, altered)
    assert got[empty][0] == "font.missing" and "Inter-Regular.ttf" in got[empty][1]
    assert got[altered][0] == "asset.hash_mismatch"


def test_shipped_brand_fonts_match_their_pins() -> None:
    assert pure(
        "from bpy_runtime.ops.text import brand_font\nprint(json.dumps(str(brand_font(400))))"
    ) == str(BRAND_FONT)


def test_brand_font_weight_is_refused_with_a_pinned_font() -> None:
    args = {"body": "Hi", "font": {"path": "fonts/Brand.ttf", "sha256": "0" * 64}, "weight": 600}
    code = (
        "from bpy_runtime.ops import ToolError\n"
        "from bpy_runtime.ops.text import _parse\n"
        "try:\n"
        "    _parse(json.loads(sys.argv[2]))\n"
        "except ToolError as exc:\n"
        "    print(json.dumps(str(exc)))\n"
    )
    assert "not allowed with font" in pure(code, json.dumps(args))


def test_text_cutout_needs_an_existing_plate(blender: Path, tmp_path: Path) -> None:
    step = ("text.add", {"body": "GO", "mode": "cutout", "plate": "Missing"})
    run = run_steps(blender, tmp_path, [step])
    assert run.proc.returncode == 1 and "[scene.object_not_found]" in run.proc.stderr
    assert not any("curves.new" in c for c in run.calls)


# --- R7: world.set_hdri -------------------------------------------------------------------


def test_world_set_hdri(blender: Path, tmp_path: Path) -> None:
    assets = _assets(tmp_path)
    step = {**_HDRI, "strength": 2, "rotation": 90}
    run = run_steps(blender, tmp_path, [("world.set_hdri", step)], assets_dir=assets)
    assert run.proc.returncode == 0, run.proc.stderr
    assert run.report("world.set_hdri") == {**_HDRI, "strength": 2.0, "rotation": 90.0}
    hdri = str((assets / "hdri" / "studio.hdr").resolve())
    assert any(f"images.load({hdri!r}, check_existing=True)" in c for c in run.calls)
    for node in ("ShaderNodeTexEnvironment", "ShaderNodeMapping", "ShaderNodeBackground"):
        assert any(f"nodes.new('{node}')" in c for c in run.calls), node


@pytest.mark.parametrize(
    ("args", "code"),
    [
        ({**_HDRI, "sha256": "0" * 64}, "asset.hash_mismatch"),
        ({**_HDRI, "path": "../studio.hdr"}, "asset.path_refused"),
        ({**_HDRI, "path": "hdri/studio.png"}, "asset.path_refused"),
    ],
)
def test_world_set_hdri_refusals(
    blender: Path, tmp_path: Path, args: dict[str, Any], code: str
) -> None:
    assets = _assets(tmp_path)
    run = run_steps(blender, tmp_path, [("world.set_hdri", args)], assets_dir=assets)
    assert run.proc.returncode == 1 and f"[{code}]" in run.proc.stderr, run.proc.stderr
    assert not any("images.load" in c for c in run.calls)


# --- R13: render.set_passes with the issue's allowlist ------------------------------------


def test_set_passes_enables_exactly_the_issue_allowlist() -> None:
    wanted = [
        "combined",
        "z",
        "mist",
        "normal",
        "diffuse",
        "glossy",
        "emission",
        "cryptomatte_object",
        "cryptomatte_material",
    ]
    report, layer = pure(
        "from types import SimpleNamespace\n"
        "from bpy_runtime.ops.props import PASSES, render_set_passes\n"
        "layer = SimpleNamespace()\n"
        "bpy = SimpleNamespace(context=SimpleNamespace(view_layer=layer))\n"
        "for name in PASSES: PASSES[name](layer, True)\n"
        "report = render_set_passes(bpy, {'passes': json.loads(sys.argv[2])})\n"
        "print(json.dumps([report, vars(layer)]))\n",
        json.dumps(wanted),
    )
    assert report == {"passes": wanted}
    assert {k for k, v in layer.items() if not v} == {"use_pass_vector"}


# --- #362: device.add with a pinned kit model ----------------------------------------------

KIT_IMPORT = r"""
_actions = [SimpleNamespace(name="Anim")]
_bpy.data.actions.__iter__.side_effect = lambda: iter(list(_actions))
_bpy.data.actions.remove.side_effect = lambda a: (
    _actions.remove(a), _events.append(["action.remove", a.name])
)
_bpy.data.images.load.return_value.size = (3024, 1964)
_kit = {}
IDENTITY = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]


def _socket():
    return SimpleNamespace(default_value=None)


def _import(filepath):
    _events.append(["import", filepath])
    root = _new_object("Macbook")
    root.type = "EMPTY"
    root.matrix_world = IDENTITY
    root.material_slots = []
    # A 2 m wide model standing on z = 0.5 with a 1.6 x 0.8 m screen facing -Y.
    verts = [(-0.8, -0.5, 0.6), (0.8, -0.5, 0.6), (0.8, -0.5, 1.4), (-0.8, -0.5, 1.4),
             (0.0, 0.5, 0.5)]
    uv = {}

    def new_uv(name):
        layer = SimpleNamespace(data=[SimpleNamespace(uv=None) for _ in verts])
        uv[name] = layer
        return layer

    mesh = SimpleNamespace(
        vertices=[SimpleNamespace(co=v) for v in verts],
        loops=[SimpleNamespace(vertex_index=i) for i in range(len(verts))],
        polygons=[
            SimpleNamespace(material_index=1, loop_indices=[0, 1, 2, 3], normal=(0, -1, 0)),
            SimpleNamespace(material_index=0, loop_indices=[4], normal=(0, 0, 1)),
        ],
        uv_layers=SimpleNamespace(new=new_uv),
    )
    bsdf = SimpleNamespace(
        type="BSDF_PRINCIPLED",
        inputs={k: _socket() for k in ("Base Color", "Metallic", "Roughness")},
    )
    body = SimpleNamespace(
        name="Frame.001", use_nodes=True, node_tree=SimpleNamespace(nodes=[bsdf], links=[])
    )
    glass = SimpleNamespace(name="DisplayGlass", use_nodes=True)
    top = _new_object("Top", mesh)
    top.parent = root
    top.matrix_world = IDENTITY
    top.bound_box = [(x, y, z) for x in (-1, 1) for y in (-0.5, 0.5) for z in (0.5, 1.5)]
    top.material_slots = [SimpleNamespace(material=body), SimpleNamespace(material=glass)]
    for o in (root, top):
        o.animation_data_clear.side_effect = (lambda n: lambda: _events.append(["clear", n]))(
            o.name
        )
    _kit.update(root=root, top=top, uv=uv, bsdf=bsdf)


def _kit_dump():
    record = os.environ.get("FAKE_BPY_RECORD")
    if record and _kit:
        uv = {k: [d.uv for d in v.data[:4]] for k, v in _kit["uv"].items()}
        state = {
            "uv": uv,
            "root": [list(_kit["root"].scale), list(_kit["root"].location)],
            "slots": [str(s.material.name) for s in _kit["top"].material_slots],
            "body": {k: s.default_value for k, s in _kit["bsdf"].inputs.items()},
        }
        with open(record + ".kit", "w") as fh:
            json.dump(state, fh, default=str)


_bpy.ops.import_scene.gltf.side_effect = _import
atexit.register(_kit_dump)
"""


@pytest.fixture
def kit_blender(tmp_path: Path) -> Path:
    bpy_dir = tmp_path / "fake_bpy"
    bpy_dir.mkdir()
    fake = FAKE_BPY.replace("import sys\nsys.modules[__name__] = _bpy", KIT_IMPORT)
    (bpy_dir / "bpy.py").write_text(fake + "\nimport sys\nsys.modules[__name__] = _bpy\n")
    exe = tmp_path / "blender"
    exe.write_text(FAKE_BLENDER.format(python=sys.executable, fake_bpy_dir=str(bpy_dir)))
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return exe


GLB = b"glTF fake kit model"


def _kit_step(**over: Any) -> dict[str, Any]:
    step: dict[str, Any] = {
        "kind": "laptop",
        "name": "device",
        "screen": {"frames": _frames(1)},
        "emission_strength": 1.7,
        "finish": "space-black",
        "model": {
            "path": f"devices/{sha(GLB)}.glb",
            "sha256": sha(GLB),
            "screen": {"node": "Top", "material": "DisplayGlass"},
            "real_width": 0.3,
            "body_material": "Frame.001",
        },
    }
    step.update(over)
    return step


def _kit_assets(tmp_path: Path) -> Path:
    assets = _assets(tmp_path)
    (assets / "devices").mkdir()
    (assets / "devices" / f"{sha(GLB)}.glb").write_bytes(GLB)
    return assets


def test_device_add_model_normalises_and_maps_the_screen(
    kit_blender: Path, tmp_path: Path
) -> None:
    assets = _kit_assets(tmp_path)
    run = run_steps(kit_blender, tmp_path, [("device.add", _kit_step())], assets_dir=assets)
    assert run.proc.returncode == 0, run.proc.stderr
    report = run.report("device.add")
    assert report["device"] == "Macbook" and report["screen_object"] == "Top"
    assert report["model"] == f"devices/{sha(GLB)}.glb" and report["emission_strength"] == 1.7
    # 2 m wide -> 0.3 m: scale 0.15; lowest point z 0.5 * 0.15 lifted to the floor.
    assert report["scale"] == 0.15
    screen = report["screen"]
    assert screen["centre"] == [0.0, -0.075, 0.075]
    assert screen["normal"] == [0.0, -1.0, 0.0] and screen["up"] == [0.0, 0.0, 1.0]
    assert (screen["width"], screen["height"]) == (0.24, 0.12)
    assert screen["corners"][0] == [-0.12, -0.075, 0.135]  # top-left seen from the front
    kit = json.loads((tmp_path / "bpy.json.kit").read_text())
    assert kit["root"] == [[0.15, 0.15, 0.15], [0.0, 0.0, -0.075]]
    # Planar UVs: upright and unmirrored seen from the front (bottom-left -> 0,0).
    uvs = [[round(c, 9) + 0.0 for c in uv] for uv in kit["uv"]["SC_ScreenUV"]]
    assert uvs == [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]
    assert kit["slots"] == ["Frame.001", "screen.device"]
    assert kit["body"]["Roughness"] == 0.34 and kit["body"]["Metallic"] == 1.0
    events = run.state["events"]
    assert ["clear", "Macbook"] in events and ["clear", "Top"] in events
    assert ["action.remove", "Anim"] in events
    calls = run.calls
    for node in ("ShaderNodeEmission", "ShaderNodeBsdfGlossy", "ShaderNodeFresnel"):
        assert any(f"nodes.new('{node}')" in c for c in calls), node
    assert any("nodes.new('ShaderNodeMapping')" in c for c in calls)
    # No procedural rig parts.
    assert not any("Dev.body" in c or "meshes.new" in c for c in calls)


@pytest.mark.parametrize(
    ("over", "code"),
    [
        ({"model": {**_kit_step()["model"], "sha256": "0" * 64}}, "asset.hash_mismatch"),
        ({"model": {**_kit_step()["model"], "path": "../mac.glb"}}, "asset.path_refused"),
        ({"finish": "aluminium"}, "args.invalid"),
        ({"rotation": [0, 0, 1]}, "args.invalid"),
        ({"bevel": 0.003}, "args.invalid"),
    ],
)
def test_device_add_model_refusals_before_bpy(
    kit_blender: Path, tmp_path: Path, over: dict[str, Any], code: str
) -> None:
    assets = _kit_assets(tmp_path)
    run = run_steps(kit_blender, tmp_path, [("device.add", _kit_step(**over))], assets_dir=assets)
    assert run.proc.returncode == 1
    assert f"[{code}]" in run.proc.stderr, run.proc.stderr
    assert not any("import_scene" in c or "images.load" in c for c in run.calls)


def test_cover_crop_fills_the_screen_from_the_top() -> None:
    got = pure(
        "from bpy_runtime.ops.device import cover_crop\n"
        "print(json.dumps([cover_crop(2, 1, 3024, 1964), cover_crop(1, 2, 4, 2),"
        " cover_crop(2, 1, 4, 2)]))"
    )
    taller, wider, same = got
    # A taller capture keeps its top: v spans [1 - f, 1]; u is untouched.
    assert taller[0] == 1.0 and taller[2] == 0.0
    assert taller[1] == pytest.approx((3024 / 1964) / 2) and taller[1] + taller[3] == 1.0
    # A wider capture is cropped equally at both sides.
    assert wider[1] == 1.0 and wider[0] == pytest.approx(0.25) and wider[2] == pytest.approx(0.375)
    assert same == [1.0, 1.0, 0.0, 0.0]


def test_model_finishes_match_the_device_kit() -> None:
    from spectracut.templates.device_kit import MACBOOK_FINISHES

    host = pure(
        "from bpy_runtime.ops.device import MODEL_FINISHES\nprint(json.dumps(MODEL_FINISHES))"
    )
    assert host == json.loads(json.dumps(dict(MACBOOK_FINISHES)))
