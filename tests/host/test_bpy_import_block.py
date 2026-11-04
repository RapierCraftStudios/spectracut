# SPDX-License-Identifier: AGPL-3.0-or-later
"""The control plane can't import bpy or the bpy runtime (guardrail regression test)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from spectracut import _bpy_guard

ROOT = Path(__file__).resolve().parents[2]


def _run(code: str, extra_path: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if extra_path is not None:
        env["PYTHONPATH"] = f"{extra_path}{os.pathsep}{env.get('PYTHONPATH', '')}"
    return subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, check=False
    )


@pytest.mark.parametrize("module", ["bpy", "bpy.ops", "bpy_runtime", "bpy_runtime.tools"])
def test_control_plane_cannot_import_blender(tmp_path: Path, module: str) -> None:
    # A real-looking bpy is importable on this path; the control plane must still refuse it.
    (tmp_path / "bpy").mkdir()
    (tmp_path / "bpy" / "__init__.py").write_text("")
    (tmp_path / "bpy" / "ops.py").write_text("")
    code = (
        "import spectracut, importlib\n"
        "try:\n"
        f"    importlib.import_module({module!r})\n"
        "except ImportError as exc:\n"
        "    assert 'registered render hosts' in str(exc), exc\n"
        "    print('blocked')\n"
        "else:\n"
        "    raise SystemExit('imported')\n"
    )
    result = _run(code, tmp_path)
    assert result.returncode == 0, result.stderr + result.stdout
    assert result.stdout.strip() == "blocked"


def test_bpy_runtime_is_on_path_but_blocked() -> None:
    # The editable host install exposes host/, so only the blocker keeps bpy_runtime out.
    result = _run(
        "import importlib.util, spectracut\n"
        "from spectracut import _bpy_guard\n"
        "import sys\n"
        "sys.meta_path = [f for f in sys.meta_path"
        " if not isinstance(f, _bpy_guard.BpyImportBlocker)]\n"
        "print(importlib.util.find_spec('bpy_runtime') is not None)\n"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True"


def test_host_agent_process_cannot_import_bpy() -> None:
    result = _run("import spectracut_host.agent\nimport bpy_runtime.tools\n")
    assert result.returncode != 0
    assert "registered render hosts" in result.stderr


def test_other_imports_unaffected() -> None:
    assert not _bpy_guard.is_blocked("bpyx")
    assert not _bpy_guard.is_blocked("json")
    assert _bpy_guard.is_blocked("bpy")
    assert _bpy_guard.is_blocked("bpy_runtime.runner")


def test_install_is_idempotent() -> None:
    _bpy_guard.install()
    _bpy_guard.install()
    blockers = [f for f in sys.meta_path if isinstance(f, _bpy_guard.BpyImportBlocker)]
    assert len(blockers) == 1
