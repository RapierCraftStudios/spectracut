# SPDX-License-Identifier: AGPL-3.0-or-later
"""``studio new`` + ``studio run`` on the standard profile with the real departments."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from spectracut.cli import studio as studio_mod
from spectracut.cli.app import app
from spectracut.spec.brief import AudienceProfile, Brief, ClaimSource, Persona, ProofPoint
from spectracut.studio.departments import all_departments

runner = CliRunner()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "home"
    monkeypatch.setenv("SPECTRACUT_HOME", str(root))
    monkeypatch.setenv("SPECTRACUT_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("SPECTRACUT_PROVIDERS", "mock")
    return root


@pytest.fixture
def brief_file(tmp_path: Path) -> Path:
    brief = Brief(
        id="alterlab",
        product="AlterLab",
        audience=AudienceProfile(personas=[Persona(name="Dev", description="ships often")]),
        sources=[ClaimSource(id="docs", title="Docs", ref="https://example.test/docs")],
        proof_points=[ProofPoint(claim="Fast", source_id="docs")],
        cta="Try it",
    )
    path = tmp_path / "brief.json"
    path.write_text(brief.model_dump_json(), encoding="utf-8")
    return path


def test_departments_are_registered_by_default() -> None:
    assert studio_mod.DEPARTMENTS == all_departments()
    assert studio_mod.text_provider_factory is not None


def test_new_lists_the_stages_with_no_department(home: Path, brief_file: Path) -> None:
    result = runner.invoke(app, ["studio", "new", str(brief_file), "--profile", "standard"])
    assert result.exit_code == 0, result.output
    assert "  brief: department=strategist" in result.output
    assert "no department registered yet for:" in result.output
    unregistered = result.output.split("no department registered yet for:")[1]
    assert "render" in unregistered
    assert "voiceover" not in unregistered


def test_run_builds_brief_and_stops_at_the_brief_human_gate(home: Path, brief_file: Path) -> None:
    created = runner.invoke(
        app, ["studio", "new", str(brief_file), "--profile", "standard", "--id", "alterlab-launch"]
    )
    assert created.exit_code == 0, created.output
    result = runner.invoke(app, ["studio", "run", "alterlab-launch"])
    assert "LookupError" not in result.output
    assert "no department registered" not in result.output
    assert result.exit_code == 0, result.output
    assert "awaiting approval: brief" in result.output


def test_approving_each_gate_builds_concept_then_script(home: Path, brief_file: Path) -> None:
    created = runner.invoke(
        app, ["studio", "new", str(brief_file), "--id", "film", "--profile", "standard"]
    )
    assert created.exit_code == 0, created.output
    for built in ("brief", "concept"):
        result = runner.invoke(app, ["studio", "run", "film"])
        assert result.exit_code == 0, result.output
        assert f"awaiting approval: {built}" in result.output
        approved = runner.invoke(app, ["studio", "approve", "film", built])
        assert approved.exit_code == 0, approved.output
    # style_frames needs an art_direction node the standard profile does not emit yet.
    assert "executed: script" in runner.invoke(app, ["studio", "run", "film"]).output
