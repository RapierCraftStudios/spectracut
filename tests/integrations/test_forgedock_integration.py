# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ForgeDock skill pack is opt-in: without ForgeDock, SpectraCut is unchanged.

Spike § ForgeDock: SpectraCut never imports ForgeDock or reads its state; the
integration is a skill pack under ``integrations/forgedock/`` (issue #62).
"""

from __future__ import annotations

import json
import re
import subprocess
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from spectracut.cli.app import app

ROOT = Path(__file__).resolve().parents[2]
PACK = ROOT / "integrations" / "forgedock"
SKILL = PACK / "skills" / "spectracut-demo"
CODE_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".toml", ".json"}
# The only SpectraCut code allowed to name ForgeDock: doctor's PATH probe.
ALLOWED = {"src/spectracut/cli/doctor.py"}


def _tracked(*roots: str) -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", *roots], cwd=ROOT, capture_output=True, check=True
    ).stdout.decode()
    return [ROOT / p for p in out.split("\0") if p and Path(p).suffix in CODE_SUFFIXES]


def test_no_spectracut_code_references_forgedock() -> None:
    offenders = []
    for path in _tracked("src", "host", "packages", "apps"):
        rel = path.relative_to(ROOT).as_posix()
        if rel in ALLOWED or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if re.search(r"forgedock|integrations/forgedock", text, re.IGNORECASE):
            offenders.append(rel)
    assert offenders == []


def test_pack_is_outside_every_install_path() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    wheel = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel["packages"] == ["src/spectracut"]
    assert not any("integrations" in k for k in wheel.get("force-include", {}))
    workspaces = json.loads((ROOT / "package.json").read_text())["workspaces"]
    assert not any(w.startswith("integrations") for w in workspaces)
    assert not (SKILL / "package.json").exists()


def test_skill_pack_ships_the_demo_skill() -> None:
    text = (SKILL / "SKILL.md").read_text()
    front = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert front is not None
    assert re.search(r"^name: spectracut-demo$", front.group(1), re.MULTILINE)
    assert re.search(r"^description: .*staging.*", front.group(1), re.MULTILINE)
    assert "no Blender" in text
    runner = (SKILL / "demo.mjs").read_text()
    assert "SPDX-License-Identifier: AGPL-3.0-or-later" in runner
    assert "bpy" not in runner and "blender" not in runner.lower().replace("no blender", "")
    assert "spectracut-demo" in (PACK / "README.md").read_text()


@pytest.fixture
def no_forgedock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPECTRACUT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SPECTRACUT_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))


@pytest.mark.usefixtures("no_forgedock")
def test_doctor_reports_forgedock_off_without_it() -> None:
    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "forgedock: off" in result.output.splitlines()
