# SPDX-License-Identifier: AGPL-3.0-or-later
"""CI runs ruff, mypy, pytest, tsc, ESLint, Vitest and the SPDX check on every PR."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def load() -> dict[Any, Any]:
    data = yaml.safe_load(WORKFLOW.read_text())
    assert isinstance(data, dict)
    return data


def run_lines() -> list[str]:
    lines: list[str] = []
    for job in load()["jobs"].values():
        for step in job["steps"]:
            if "run" in step:
                lines.extend(step["run"].splitlines())
    return lines


def test_runs_on_every_pull_request() -> None:
    # PyYAML parses the bare key `on` as boolean True.
    triggers = load().get(True) or load().get("on")
    assert isinstance(triggers, dict)
    assert "pull_request" in triggers
    assert triggers["pull_request"] in (None, {}) or "branches" not in triggers["pull_request"]


@pytest.mark.parametrize(
    "needle",
    [
        "uv sync --locked",
        "scripts/check_spdx.py",
        "scripts/check_schema_compat.py",
        "ruff check",
        "ruff format --check",
        "mypy",
        "pytest",
        "npm ci",
        "tsc",
        "eslint",
        "vitest run",
    ],
)
def test_ci_runs_check(needle: str) -> None:
    assert any(needle in line for line in run_lines()), f"ci.yml never runs {needle!r}"
