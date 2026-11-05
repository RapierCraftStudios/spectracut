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


def test_ci_builds_both_images_without_pushing() -> None:
    # The release images job only runs on tags, so CI must build the images on every PR.
    docker_jobs = [
        job
        for job in load()["jobs"].values()
        if any("docker/build-push-action" in step.get("uses", "") for step in job["steps"])
    ]
    assert docker_jobs, "ci.yml never builds the Docker images"
    files = {
        entry["dockerfile"]
        for job in docker_jobs
        for entry in job.get("strategy", {}).get("matrix", {}).get("include", [])
    }
    assert files == {"docker/Dockerfile.api", "docker/Dockerfile.host"}
    for job in docker_jobs:
        for step in job["steps"]:
            if "docker/build-push-action" in step.get("uses", ""):
                assert step.get("with", {}).get("push", False) is False
