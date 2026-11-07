# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fixtures for host tests: keys, signed jobs, a host registration, fake Blender.

The fake ``blender`` is a Python script. ``--version`` prints a Blender 4.5
banner. Otherwise it records its argv and environment, then runs the real
``bpy_runtime/runner.py`` as ``__main__`` with a fake ``bpy`` (a recording
``MagicMock``) on ``sys.path``, the way Blender runs ``--python`` scripts. The
fake ``bpy`` writes one file per rendered frame.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from spectracut.queue.signing import (
    JobEnvelope,
    SignedJob,
    generate_keypair,
    load_private_key,
    sign_job,
)
from spectracut.spec.recipe import Recipe, RecipeStep

ROOT = Path(__file__).resolve().parents[2]
HOST_DIR = ROOT / "host"
RUNTIME_DIR = HOST_DIR / "bpy_runtime"

FAKE_BPY = r"""
import atexit, json, os
from pathlib import Path
from unittest.mock import MagicMock

_bpy = MagicMock(name="bpy")
_bpy.app.version = (4, 5, 3)

def _chunk(kind, body):
    import struct, zlib
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

def _png():
    import struct, time, zlib
    head = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", head)
        + _chunk(b"tEXt", b"RenderTime\x00" + str(time.time()).encode())
        + _chunk(b"IDAT", zlib.compress(b"\x00\x01\x02\x03\x04"))
        + _chunk(b"IEND", b""))

def _render(animation=False):
    scene = _bpy.context.scene
    out = str(scene.render.filepath)
    for frame in range(int(scene.frame_start), int(scene.frame_end) + 1):
        body = _png() if os.environ.get("FAKE_BPY_PNG") else b"png"
        Path(f"{out}{frame:04d}.png").write_bytes(body)
    record = os.environ.get("FAKE_BPY_RECORD")
    if record:
        stamps = {k: getattr(scene.render, k) is True for k in (
            "use_stamp", "use_stamp_date", "use_stamp_render_time")}
        Path(record + ".stamps").write_text(json.dumps(stamps))

_bpy.ops.render.render.side_effect = _render

# FAKE_BPY_GPU=OPTIX (or CUDA, ...) gives Cycles one GPU of that type next to the CPU;
# unset, the device list is empty, as on a host without a GPU.
_gpu = os.environ.get("FAKE_BPY_GPU")
if _gpu:
    from types import SimpleNamespace
    _bpy.context.preferences.addons["cycles"].preferences.devices = [
        SimpleNamespace(type="CPU", name="CPU", use=True),
        SimpleNamespace(type=_gpu, name="NVIDIA GeForce RTX 3070 Laptop GPU", use=False),
    ]

def _device_state():
    record = os.environ.get("FAKE_BPY_RECORD")
    if not record:
        return
    scene = _bpy.context.scene
    prefs = _bpy.context.preferences.addons["cycles"].preferences
    try:
        uses = {d.type: d.use for d in prefs.devices}
    except TypeError:
        uses = {}
    Path(record + ".device").write_text(json.dumps({
        "cycles_device": str(scene.cycles.device),
        "denoising_use_gpu": scene.cycles.denoising_use_gpu is True,
        "compute_device_type": str(prefs.compute_device_type),
        "uses": uses,
    }))

_render_frames = _render

def _render_and_record(animation=False):
    _device_state()
    _render_frames(animation)

_bpy.ops.render.render.side_effect = _render_and_record

def _dump():
    record = os.environ.get("FAKE_BPY_RECORD")
    if record:
        with open(record, "a") as fh:
            fh.write(json.dumps([str(c) for c in _bpy.mock_calls]) + "\n")

atexit.register(_dump)
import sys
sys.modules[__name__] = _bpy
"""

FAKE_BLENDER = r"""#!{python}
import json, os, runpy, sys
argv = sys.argv[1:]
if argv == ["--version"]:
    print("Blender {version} LTS\n\tbuild date: 2026-01-01")
    sys.exit(0)
record = os.environ.get("FAKE_BLENDER_RECORD")
if record:
    with open(record, "a") as fh:
        fh.write(json.dumps({{"argv": argv, "env": sorted(os.environ)}}) + "\n")
script = argv[argv.index("--python") + 1]
sys.path.insert(0, {fake_bpy_dir!r})
sys.argv = ["blender"] + argv
try:
    runpy.run_path(script, run_name="__main__")
except SystemExit as exc:
    sys.exit(exc.code if isinstance(exc.code, int) else 1)
"""


@pytest.fixture
def keypair() -> tuple[str, str]:
    return generate_keypair()


def _make_recipe(tools: list[tuple[str, dict[str, Any]]] | None = None) -> Recipe:
    tools = tools or [
        ("scene.clear", {}),
        ("camera.add", {"name": "Cam", "location": [0, -6, 1.5], "rotation": [1.4, 0, 0]}),
        ("camera.set_lens", {"mm": 35}),
        ("object.add_primitive", {"kind": "cube", "location": [0, 0, 0]}),
        ("light.add", {"kind": "area", "energy": 500, "location": [2, -2, 3]}),
        ("render.set_engine", {"engine": "eevee"}),
        ("render.set_resolution", {"width": 64, "height": 36}),
    ]
    steps = [RecipeStep(index=i, tool=t, args=a) for i, (t, a) in enumerate(tools)]
    return Recipe(id="r1", shot_id="shot-1", blender_version="4.5", steps=steps)


def _make_envelope(
    recipe: Recipe | None = None,
    *,
    job_id: str = "job-1",
    frames: tuple[int, int] = (1, 3),
    recipe_hash: str | None = None,
    expires_in: timedelta = timedelta(hours=1),
    issued_at: datetime | None = None,
    required_tag: str | None = None,
) -> JobEnvelope:
    recipe = recipe or _make_recipe()
    issued = issued_at or datetime.now(UTC)
    return JobEnvelope(
        job_id=job_id,
        recipe=recipe,
        recipe_hash=recipe_hash or recipe.content_hash,
        frame_start=frames[0],
        frame_end=frames[1],
        output="shot-1",
        required_tag=required_tag,
        issued_at=issued,
        expires_at=issued + expires_in,
    )


SignJob = Callable[..., SignedJob]
MakeEnvelope = Callable[..., JobEnvelope]
MakeRecipe = Callable[..., Recipe]


@pytest.fixture
def make_recipe() -> MakeRecipe:
    return _make_recipe


@pytest.fixture
def make_envelope() -> MakeEnvelope:
    return _make_envelope


@pytest.fixture
def sign(keypair: tuple[str, str]) -> SignJob:
    def _sign(envelope: JobEnvelope | None = None, **kwargs: Any) -> SignedJob:
        return sign_job(envelope or _make_envelope(**kwargs), load_private_key(keypair[0]))

    return _sign


@pytest.fixture
def registration(tmp_path: Path, keypair: tuple[str, str]) -> Path:
    path = tmp_path / "host.json"
    path.write_text(json.dumps({"host_id": "legion", "control_plane_public_key": keypair[1]}))
    return path


def _write_exe(path: Path, text: str) -> Path:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture
def fake_bin(tmp_path: Path) -> Path:
    """A bin dir with fake ``blender`` (4.5.3) and ``nvidia-smi`` (RTX 4070 Ti)."""
    bpy_dir = tmp_path / "fake_bpy"
    bpy_dir.mkdir()
    (bpy_dir / "bpy.py").write_text(FAKE_BPY)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_exe(
        bin_dir / "blender",
        FAKE_BLENDER.format(python=sys.executable, version="4.5.3", fake_bpy_dir=str(bpy_dir)),
    )
    _write_exe(
        bin_dir / "nvidia-smi",
        "#!/bin/sh\necho 'NVIDIA GeForce RTX 4070 Ti, 560.94, 12282'\n",
    )
    return bin_dir


@pytest.fixture
def host_env(tmp_path: Path, fake_bin: Path, registration: Path) -> dict[str, str]:
    """Environment for running ``spectracut-host`` as a registered host with fake Blender."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("SPECTRACUT_")}
    env.update(
        PATH=f"{fake_bin}{os.pathsep}{env.get('PATH', '')}",
        SPECTRACUT_HOST_CONFIG=str(registration),
        SPECTRACUT_BLENDER=str(fake_bin / "blender"),
        FAKE_BLENDER_RECORD=str(tmp_path / "blender-record.jsonl"),
        FAKE_BPY_RECORD=str(tmp_path / "bpy-record.jsonl"),
        HOME=str(tmp_path / "home"),
    )
    return env


def read_records(path: Path) -> list[Any]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
