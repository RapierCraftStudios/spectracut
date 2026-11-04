# SPDX-License-Identifier: AGPL-3.0-or-later
"""The package layout matches spike § Repo layout."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

CORE_MODULES = [
    "cli",
    "spec",
    "harness",
    "recipes",
    "studio",
    "canon",
    "edit",
    "finishing",
    "queue",
    "templates",
    "skills",
    "storyboard",
    "critique",
    "writer",
    "audio",
    "formats",
    "qa",
    "distribution",
    "providers",
    "providers/local",
    "api",
    "cloud",
]

LAYOUT_PATHS = [
    "pyproject.toml",
    "src/spectracut/__init__.py",
    "host/pyproject.toml",
    "host/spectracut_host/__init__.py",
    "host/bpy_runtime/__init__.py",
    "packages/capture/package.json",
    "integrations/forgedock/README.md",
    "apps/review/package.json",
    "apps/web/package.json",
    "docs/engineering/technical-spike.md",
    "tests",
    ".github/workflows/ci.yml",
]


@pytest.mark.parametrize("module", CORE_MODULES)
def test_core_module_exists(module: str) -> None:
    assert (ROOT / "src" / "spectracut" / module / "__init__.py").is_file()


@pytest.mark.parametrize("path", LAYOUT_PATHS)
def test_layout_path_exists(path: str) -> None:
    assert (ROOT / path).exists()


def test_spike_lists_every_core_module() -> None:
    spike = (ROOT / "docs" / "engineering" / "technical-spike.md").read_text()
    for module in CORE_MODULES:
        top = module.split("/")[0]
        assert f"    {top}/" in spike, f"{top}/ not in spike § Repo layout"


def test_control_plane_does_not_import_blender() -> None:
    code = (
        "import importlib, pkgutil, sys, spectracut\n"
        "for m in pkgutil.walk_packages(spectracut.__path__, 'spectracut.'):\n"
        "    importlib.import_module(m.name)\n"
        "bad = [n for n in sys.modules if n == 'bpy' or n.startswith(('bpy.', 'bpy_runtime'))]\n"
        "assert not bad, bad\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
