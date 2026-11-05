# SPDX-License-Identifier: AGPL-3.0-or-later
"""The installed spectracut-host wheel can find its bpy runtime (#161).

Builds the real wheels, installs them into a throwaway venv outside the source
tree (``--no-deps``, no network) and checks, from that venv, that the guard
resolves ``bpy_runtime`` inside it: ``runtime_tool_names`` and ``runner.py``.
Third-party dependencies come from the dev venv through a ``.pth`` appended
after the temp venv's own site-packages, so the installed ``spectracut`` and
``spectracut_host`` win and the editable source paths are never on ``sys.path``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import sysconfig
import zipfile
from pathlib import Path

import pytest
from spectracut_host.guard import runtime_tool_names

from spectracut.setup.wheels import LocalWheels, build_wheels

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "host" / "bpy_runtime"

pytestmark = pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv")

PROBE = r"""
import json, sys
from pathlib import Path
from spectracut_host.guard import ExecutionGuard, runtime_dir, runtime_tool_names
from spectracut.queue.profile import RenderProfile

runtime = runtime_dir({})
guard = ExecutionGuard(None, RenderProfile(blender_version="4.5.3", gpu_model="x"))
try:
    import bpy_runtime.tools  # noqa: F401
    blocked = False
except ImportError as exc:
    blocked = "registered render hosts" in str(exc)
print(json.dumps({
    "runtime": str(runtime),
    "guard_runtime": str(guard.runtime),
    "tools": sorted(runtime_tool_names(runtime / "tools.py")),
    "runner": (runtime / "runner.py").is_file(),
    "blocked": blocked,
}))
"""


@pytest.fixture(scope="module")
def wheels(tmp_path_factory: pytest.TempPathFactory) -> LocalWheels:
    return build_wheels(tmp_path_factory.mktemp("wheels"))


def test_host_wheel_ships_every_runtime_module_and_no_bytecode(wheels: LocalWheels) -> None:
    names = set(zipfile.ZipFile(wheels.host).namelist())
    expected = {f"bpy_runtime/{p.relative_to(RUNTIME).as_posix()}" for p in RUNTIME.rglob("*.py")}
    assert {"bpy_runtime/runner.py", "bpy_runtime/tools.py"} <= expected
    assert {"bpy_runtime/ops/__init__.py", "bpy_runtime/ops/scene.py"} <= expected
    assert expected <= names
    assert not [n for n in names if n.endswith(".pyc") or "__pycache__" in n]
    core = set(zipfile.ZipFile(wheels.spectracut).namelist())
    assert not [n for n in core if n.startswith("bpy_runtime/")]


def test_installed_wheel_resolves_runtime_tools_and_runner(
    wheels: LocalWheels, tmp_path: Path
) -> None:
    uv = shutil.which("uv")
    assert uv is not None
    venv = tmp_path / "host-venv"

    def run(cmd: list[str]) -> str:
        return subprocess.run(cmd, capture_output=True, text=True, check=True, cwd=tmp_path).stdout

    run([uv, "venv", "-q", "--python", sys.executable, str(venv)])
    py = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    run(
        [uv, "pip", "install", "-q", "--offline", "--no-deps", "--python", str(py)]
        + [str(w) for w in wheels.files]
    )
    site = run([str(py), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"])
    site = site.strip()
    (Path(site) / "zz_dev_deps.pth").write_text(sysconfig.get_paths()["purelib"] + "\n")

    got = json.loads(run([str(py), "-c", PROBE]))
    runtime = Path(got["runtime"])
    assert runtime == Path(site) / "bpy_runtime"
    assert runtime.is_relative_to(venv.resolve()) or runtime.is_relative_to(venv)
    assert not runtime.resolve().is_relative_to(ROOT)
    assert got["guard_runtime"] == got["runtime"]
    assert set(got["tools"]) == set(runtime_tool_names(RUNTIME / "tools.py"))
    assert got["tools"]
    assert got["runner"] is True
    assert got["blocked"] is True  # shipping the files does not let the host import them
