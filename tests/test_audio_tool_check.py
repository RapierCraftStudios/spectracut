# SPDX-License-Identifier: AGPL-3.0-or-later
"""Static checks for the audio tool check (issue #48).

The e2e run needs the Docker image (see docs/engineering/audio-tool-check.md);
these tests keep the script, image, report and workflow consistent without it.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "audio_tool_check.sh"
DOCKERFILE = ROOT / "host" / "docker" / "audio.Dockerfile"
REPORT = ROOT / "docs" / "engineering" / "audio-tool-check.md"
WORKFLOW = ROOT / ".github" / "workflows" / "audio-tool-check.yml"
SPDX = "SPDX-License-Identifier: AGPL-3.0-or-later"

TOOLS = [
    "Ardour",
    "SuperCollider",
    "Pedalboard",
    "LSP Plugins",
    "x42",
    "Airwindows",
    "Dragonfly Reverb",
    "Surge XT",
    "IEM Plugin Suite",
    "EBU ADM Renderer (ear)",
]
COLUMNS = ["Tool", "Version", "Licence", "Install path", "Result", "Time", "Fallback"]


def head(path: Path, n: int = 5) -> str:
    return "\n".join(path.read_text(encoding="utf-8").splitlines()[:n])


def results_table() -> dict[str, dict[str, str]]:
    """Parse the report's results table (the one whose header starts with COLUMNS)."""
    lines = REPORT.read_text(encoding="utf-8").splitlines()
    rows: dict[str, dict[str, str]] = {}
    header: list[str] | None = None
    for line in lines:
        if not line.startswith("|"):
            if header is not None and rows:
                break
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if header is None:
            if cells[: len(COLUMNS)] == COLUMNS:
                header = cells
            continue
        if set("".join(cells)) <= set("-: "):
            continue
        rows[cells[0]] = dict(zip(header, cells, strict=True))
    assert header is not None, f"no results table with columns {COLUMNS} in {REPORT}"
    return rows


def test_script_parses_and_has_header() -> None:
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)
    assert SPDX in head(SCRIPT)
    text = SCRIPT.read_text(encoding="utf-8")
    assert "set -euo pipefail" in text
    assert "results.json" in text


@pytest.mark.parametrize("tool", TOOLS)
def test_script_records_every_tool(tool: str) -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert re.search(rf"(record|inventory_\w+) \"?{re.escape(tool)}\"? ", text), tool


def test_script_checks_determinism_twice() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "for run in 1 2" in text
    assert 'hashes[0]}" == "${hashes[1]}' in text


def test_dockerfile_is_pinned() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert SPDX in head(DOCKERFILE)
    assert re.search(r"^FROM \S+@sha256:[0-9a-f]{64}$", text, re.M), "FROM is not digest-pinned"
    assert "pedalboard==" in text
    assert "ear==" in text
    for url_arg in ("SURGE", "AIRWINDOWS"):
        assert re.search(rf"^ARG {url_arg}_SHA256=[0-9a-f]{{64}}$", text, re.M), url_arg
    assert "COPY" not in text and "ADD " not in text, "image must not bake in repo files"


def test_report_lists_every_tool_with_fallback() -> None:
    rows = results_table()
    assert sorted(rows) == sorted(TOOLS)
    for tool, row in rows.items():
        for col in ("Version", "Licence", "Install path", "Result"):
            assert row[col] not in ("", "-", "?"), f"{tool}: empty {col}"
        assert row["Result"].split()[0] in {"PASS", "FAIL"}, f"{tool}: result {row['Result']}"
        if not row["Result"].startswith("PASS"):
            assert row["Fallback"] not in ("", "-"), f"{tool} fails without a fallback"


def test_workflow_runs_image_and_script() -> None:
    data: dict[Any, Any] = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    triggers = data.get(True) or data.get("on")
    assert isinstance(triggers, dict)
    assert "workflow_dispatch" in triggers
    paths = triggers["pull_request"]["paths"]
    assert "host/docker/audio.Dockerfile" in paths
    assert "scripts/audio_tool_check.sh" in paths
    runs = "\n".join(
        step.get("run", "") for job in data["jobs"].values() for step in job["steps"]
    )
    assert "host/docker/audio.Dockerfile" in runs
    assert "audio_tool_check.sh" in runs
