# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fixtures for harness tests: keys, a registered host, a fake Blender, a dispatcher.

The fake ``blender`` answers ``--version`` with a 4.5 banner and otherwise runs the
real ``host/bpy_runtime/runner.py`` (launched by the real host agent and guard) with
a fake ``bpy`` on ``sys.path``. The fake ``bpy`` keeps real objects (names,
transforms, bounding boxes, light energies, materials, the active camera) so the
runtime's ``inspect.framing`` and ``look.set_view`` produce real numbers. Its render
writes a real PNG whose pixels are a deterministic function of that scene state:
each mesh is a rectangle at its projected bounds, coloured by its material and lit
by the lights' total energy, over a background set by the engine and shading.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from spectracut.harness.dispatch import HarnessConfig, PreviewDispatcher
from spectracut.harness.recorder import Recorder
from spectracut.harness.tools import Session
from spectracut.queue.signing import SIGNING_KEY_ENV, generate_keypair, load_private_key

ROOT = Path(__file__).resolve().parents[2]
HOST_CMD = (sys.executable, "-m", "spectracut_host.agent")

FAKE_BPY = r"""
import math, struct, zlib
from pathlib import Path
from unittest.mock import MagicMock

_bpy = MagicMock(name="bpy")
_bpy.app.version = (4, 5, 3)
_objects = []
LONG_EDGE = 48


def _mat(loc, rot):
    rx, ry, rz = rot
    cx, sx, cy, sy, cz, sz = (math.cos(rx), math.sin(rx), math.cos(ry), math.sin(ry),
                              math.cos(rz), math.sin(rz))
    r = [[cz * cy, cz * sy * sx - sz * cx, cz * sy * cx + sz * sx],
         [sz * cy, sz * sy * sx + cz * cx, sz * sy * cx - cz * sx],
         [-sy, cy * sx, cy * cx]]
    return [r[0] + [loc[0]], r[1] + [loc[1]], r[2] + [loc[2]], [0, 0, 0, 1]]


class _Mesh:
    def __init__(self, extent):
        self.extent = extent
        self.materials = []


class _Light:
    def __init__(self, name, type):
        self.name, self.type, self.energy = name, type, 10.0


class _Obj:
    def __init__(self, name, data):
        self.name = name
        self.data = data
        if isinstance(data, _Light):
            self.type = "LIGHT"
        elif getattr(data, "fake_type", None) == "CAMERA":
            self.type = "CAMERA"
        else:
            self.type = "MESH"
        self.parent = None
        self.location, self.rotation_euler = (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)
        self.scale = (1.0, 1.0, 1.0)
        self._props = {}
        self._extra = {}

    def __getattr__(self, name):
        # Anything the runtime touches that this fake does not model.
        extra = self.__dict__.setdefault("_extra", {})
        return extra.setdefault(name, MagicMock(name=f"{self.__dict__.get('name')}.{name}"))

    def __getitem__(self, key):
        return self._props[key]

    def __setitem__(self, key, value):
        self._props[key] = value

    def get(self, key, default=None):
        return self._props.get(key, default)

    def keyframe_insert(self, **kw):
        pass

    @property
    def matrix_world(self):
        return _mat(self.location, self.rotation_euler)

    @property
    def bound_box(self):
        ex = getattr(self.data, "extent", (1.0, 1.0, 1.0))
        return [
            (x * ex[0] * self.scale[0], y * ex[1] * self.scale[1], z * ex[2] * self.scale[2])
            for x in (-1, 1) for y in (-1, 1) for z in (-1, 1)
        ]


def _unique(name):
    names = {o.name for o in _objects}
    if name not in names:
        return name
    i = 1
    while f"{name}.{i:03d}" in names:
        i += 1
    return f"{name}.{i:03d}"


class _Objects:
    def new(self, name, data=None):
        obj = _Obj(_unique(name), data)
        _objects.append(obj)
        return obj

    def get(self, name):
        return next((o for o in _objects if o.name == name), None)

    def remove(self, obj, do_unlink=True):
        _objects.remove(obj)

    def __iter__(self):
        return iter(list(_objects))


def _cam_data(name):
    d = MagicMock(name="camdata_" + name)
    d.fake_type = "CAMERA"
    d.lens, d.sensor_width, d.sensor_height, d.sensor_fit = 50.0, 36.0, 24.0, "AUTO"
    d.copy.side_effect = lambda: _cam_data(name + ".copy")
    return d


def _material(name):
    m = MagicMock(name="mat_" + name)
    m.name = name
    m.diffuse_color = (0.8, 0.8, 0.8, 1.0)
    m.node_tree.nodes.get.return_value.inputs.__contains__.return_value = True
    return m


_PRIMS = {
    "cube": ("Cube", (1.0, 1.0, 1.0)),
    "plane": ("Plane", (1.0, 1.0, 0.0)),
    "uv_sphere": ("Sphere", (1.0, 1.0, 1.0)),
    "cylinder": ("Cylinder", (1.0, 1.0, 1.0)),
}


def _primitive_op(kind):
    def op(location=(0, 0, 0), **kw):
        name, extent = _PRIMS[kind]
        obj = _bpy.data.objects.new(name, _Mesh(extent))
        obj.location = tuple(location)
        _bpy.context.active_object = obj
    return op


_bpy.data.objects = _Objects()
_bpy.data.cameras.new.side_effect = _cam_data
_bpy.data.lights.new.side_effect = lambda name, type: _Light(name, type)
_bpy.data.materials.new.side_effect = _material
for _kind in _PRIMS:
    getattr(_bpy.ops.mesh, f"primitive_{_kind}_add").side_effect = _primitive_op(_kind)

_scene = _bpy.context.scene
_scene.camera = None
_scene.render.resolution_x = 1920
_scene.render.resolution_y = 1080
_scene.render.resolution_percentage = 100
_scene.render.engine = "BLENDER_EEVEE_NEXT"
_scene.display.shading.color_type = "MATERIAL"


def _png(width, height, pixels):
    def chunk(kind, body):
        crc = struct.pack(">I", zlib.crc32(kind + body))
        return struct.pack(">I", len(body)) + kind + body + crc
    stride = width * 3
    raw = b"".join(b"\x00" + bytes(pixels[y * stride:(y + 1) * stride]) for y in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def _frame():
    from bpy_runtime.ops.inspect import framing_coverage, world_corners

    scene = _bpy.context.scene
    rx, ry = int(scene.render.resolution_x), int(scene.render.resolution_y)
    long_edge = max(rx, ry)
    w, h = max(1, round(LONG_EDGE * rx / long_edge)), max(1, round(LONG_EDGE * ry / long_edge))
    engine = str(scene.render.engine)
    workbench = engine == "BLENDER_WORKBENCH"
    single = workbench and str(scene.display.shading.color_type) == "SINGLE"
    energy = sum(o.data.energy for o in _objects if o.type == "LIGHT")
    light = 0.9 if workbench else min(1.0, 0.1 + energy / 2000.0)
    bg = {"BLENDER_WORKBENCH": (40, 40, 48), "CYCLES": (12, 14, 30)}.get(engine, (24, 24, 24))
    if single:
        bg = (60, 60, 60)
    pixels = bytearray(bytes(bg) * (w * h))
    cam = scene.camera
    if cam is None:
        return _png(w, h, pixels)
    d = cam.data
    matrix = [list(r) for r in cam.matrix_world]
    meshes = [o for o in _objects if o.type == "MESH"]
    cl = cam.location
    meshes.sort(key=lambda o: -sum((o.location[i] - cl[i]) ** 2 for i in range(3)))
    for obj in meshes:
        got = framing_coverage(matrix, float(d.lens), float(d.sensor_width), float(d.sensor_height),
                               str(d.sensor_fit), rx, ry, world_corners(obj))
        if got["bbox_ndc"] is None:
            continue
        x0, y0, x1, y1 = got["bbox_ndc"]
        mats = obj.data.materials
        colour = (0.8, 0.8, 0.8) if single or not mats else tuple(mats[-1].diffuse_color)[:3]
        rgb = bytes(min(255, round(255 * c * light)) for c in colour)
        for y in range(int((1 - y1) * h), min(h, math.ceil((1 - y0) * h))):
            for x in range(int(x0 * w), min(w, math.ceil(x1 * w))):
                pixels[(y * w + x) * 3:(y * w + x) * 3 + 3] = rgb
    return _png(w, h, pixels)


def _render(animation=False):
    scene = _bpy.context.scene
    out = str(scene.render.filepath)
    data = _frame()
    for frame in range(int(scene.frame_start), int(scene.frame_end) + 1):
        Path(f"{out}{frame:04d}.png").write_bytes(data)


_bpy.ops.render.render.side_effect = _render
import sys
sys.modules[__name__] = _bpy
"""

FAKE_BLENDER = r"""#!{python}
import runpy, sys
argv = sys.argv[1:]
if argv == ["--version"]:
    print("Blender 4.5.3 LTS\n\tbuild date: 2026-01-01")
    sys.exit(0)
script = argv[argv.index("--python") + 1]
sys.path.insert(0, {fake_bpy_dir!r})
sys.argv = ["blender"] + argv
try:
    runpy.run_path(script, run_name="__main__")
except SystemExit as exc:
    sys.exit(exc.code if isinstance(exc.code, int) else 1)
"""


def write_exe(path: Path, text: str) -> Path:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture
def keypair() -> tuple[str, str]:
    return generate_keypair()


@pytest.fixture
def registration(tmp_path: Path, keypair: tuple[str, str]) -> Path:
    path = tmp_path / "host.json"
    path.write_text(json.dumps({"host_id": "harness", "control_plane_public_key": keypair[1]}))
    return path


@pytest.fixture
def fake_blender(tmp_path: Path) -> Path:
    bpy_dir = tmp_path / "fake_bpy"
    bpy_dir.mkdir()
    (bpy_dir / "bpy.py").write_text(FAKE_BPY)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    write_exe(
        bin_dir / "nvidia-smi", "#!/bin/sh\necho 'NVIDIA GeForce RTX 4070 Ti, 560.94, 12282'\n"
    )
    return write_exe(
        bin_dir / "blender",
        FAKE_BLENDER.format(python=sys.executable, fake_bpy_dir=str(bpy_dir)),
    )


@pytest.fixture
def host_env(tmp_path: Path, fake_blender: Path, registration: Path) -> dict[str, str]:
    """Env for the host agent: a registered host whose Blender is the fake."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("SPECTRACUT_")}
    env.update(
        PATH=f"{fake_blender.parent}{os.pathsep}{env.get('PATH', '')}",
        SPECTRACUT_HOST_CONFIG=str(registration),
        SPECTRACUT_BLENDER=str(fake_blender),
        HOME=str(tmp_path / "home"),
    )
    return env


@pytest.fixture
def server_env(
    tmp_path: Path, host_env: dict[str, str], keypair: tuple[str, str]
) -> dict[str, str]:
    """Env for ``spectracut mcp``: the host env plus the control plane's signing key."""
    env = dict(host_env)
    env.update(
        {
            SIGNING_KEY_ENV: keypair[0],
            "SPECTRACUT_HARNESS_DIR": str(tmp_path / "harness"),
            "SPECTRACUT_HOST_CMD": " ".join(HOST_CMD),
            "SPECTRACUT_BLENDER_VERSION": "4.5",
        }
    )
    return env


MakeDispatcher = Callable[..., PreviewDispatcher]


@pytest.fixture
def make_dispatcher(
    tmp_path: Path, fake_blender: Path, host_env: dict[str, str], keypair: tuple[str, str]
) -> MakeDispatcher:
    """Build a dispatcher on the fake host; keyword args override :class:`HarnessConfig`."""

    def make(key: str | None = None, **config: Any) -> PreviewDispatcher:
        settings: dict[str, Any] = {
            "work_dir": tmp_path / "harness",
            "blender": str(fake_blender),
            "host_cmd": HOST_CMD,
        }
        settings.update(config)
        return PreviewDispatcher(
            HarnessConfig(**settings),
            signing_key=load_private_key(key or keypair[0]),
            env=host_env,
        )

    return make


@pytest.fixture
def dispatcher(make_dispatcher: MakeDispatcher) -> PreviewDispatcher:
    return make_dispatcher()


@pytest.fixture
def session(dispatcher: PreviewDispatcher, tmp_path: Path) -> Session:
    return Session(Recorder(), dispatcher, recipe_path=tmp_path / "harness" / "recipe.json")
