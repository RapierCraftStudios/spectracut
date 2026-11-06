# SPDX-License-Identifier: AGPL-3.0-or-later
"""``studio new`` + ``studio run`` on the standard profile with the real departments."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from spectracut.cli import studio as studio_mod
from spectracut.cli.app import app
from spectracut.providers.base import VisionScore
from spectracut.queue.flamenco import WorkerTag
from spectracut.queue.signing import generate_keypair, load_private_key
from spectracut.spec.brief import AudienceProfile, Brief, ClaimSource, Persona, ProofPoint
from spectracut.storyboard.models import Board, Storyboard
from spectracut.studio import device_shot as ds
from spectracut.studio.departments import all_departments
from spectracut.studio.departments import shots as shot_depts
from spectracut.studio.gates import GatePolicy
from spectracut.studio.graph import ProductionGraph, RunContext, RunResult
from spectracut.studio.node import NodeSpec
from spectracut.studio.profiles import load_profile
from spectracut.studio.store import ProductionStore

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
    assert "voiceover" in result.output.split("no department registered yet for:")[1]


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


# ---- template_shots / shots through default_runner (#250) -----------------------

KIT = Path(__file__).resolve().parents[2] / "brands" / "alterlab" / "brand-kit.yaml"


TAG = "3070laptopgpu"


class FakeFlamenco:
    """A Manager whose jobs complete at once, writing one frame and report.json."""

    def __init__(self) -> None:
        self.submitted: list[dict[str, Any]] = []

    def list_tags(self) -> list[WorkerTag]:
        return [WorkerTag(id="tag-1", name=TAG)]

    def submit(self, job: dict[str, Any]) -> dict[str, Any]:
        self.submitted.append(job)
        settings = job["settings"]
        signed = json.loads(Path(settings["job_file"]).read_text(encoding="utf-8"))
        out = Path(settings["output_dir"])
        out.mkdir(parents=True, exist_ok=True)
        (out / "frame_0001.png").write_bytes(signed["envelope"]["recipe_hash"].encode())
        report = {"reports": [], "scene_graph_hash": "sha256:" + "a" * 64}
        (out / "report.json").write_text(json.dumps(report), encoding="utf-8")
        return {"id": f"fl-{len(self.submitted)}"}

    def job_status(self, job_id: str) -> str:
        return "completed"


class MockJudge:
    name = "mock"
    last_usage: dict[str, int] = {}

    def score(self, frame_path: Path, rubric: str) -> VisionScore:
        return VisionScore(score=0.9)


class Ticks:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        self.t += 90.0
        return self.t


class _FakeDepartments:
    """Stands in for DepartmentRunner: canned storyboard, animatic and shot plan."""

    def __init__(self, *_a: object, **_kw: object) -> None:
        pass

    def __call__(self, ctx: RunContext) -> RunResult:
        if ctx.spec.kind == shot_depts.SHOT_PLAN:
            plan = {"shots": [{"shot_id": "open", "template": "end-card", "slots": {"cta": "Go"}}]}
            return RunResult(output=plan)
        if ctx.spec.kind == "storyboard":
            board = Board(shot_id="open", action="the card lands", duration_s=2.0)
            return RunResult(output=Storyboard(boards=[board]).model_dump(mode="json"))
        return RunResult(output={"node": ctx.spec.id})


def test_text_only_runs_never_build_farm_deps(
    home: Path, brief_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_farm(_production: studio_mod.Production) -> studio_mod.DeviceShotDeps:
        raise AssertionError("text-only run built the farm deps")

    monkeypatch.setattr(studio_mod, "device_shot_factory", no_farm)
    created = runner.invoke(app, ["studio", "new", str(brief_file), "--id", "text"])
    assert created.exit_code == 0, created.output
    result = runner.invoke(app, ["studio", "run", "text"])
    assert result.exit_code == 0, result.output
    assert "awaiting approval: brief" in result.output


def test_standard_shot_nodes_render_on_the_farm_through_default_runner(
    home: Path, brief_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("spectracut.qa.copy_ocr.available_reader", lambda: None)
    created = runner.invoke(
        app,
        ["studio", "new", str(brief_file), "--id", "film", "--brand-kit", str(KIT)],
    )
    assert created.exit_code == 0, created.output
    store = ProductionStore(home)
    stored, _meta = store.load("film")
    params = stored.spec(shot_depts.TEMPLATE_SHOTS).params
    assert ds.BRAND_KIT_PARAM in params and ds.ASSETS_DIR_PARAM in params
    assert stored.spec(shot_depts.SHOTS).params == params

    fake = FakeFlamenco()
    private, _public = generate_keypair()
    built: list[str] = []

    def factory(production: studio_mod.Production) -> studio_mod.DeviceShotDeps:
        built.append(production.id)
        config = ds.DeviceShotConfig(
            flamenco_url="http://fake",
            shared=tmp_path / "shared",
            worker_tag=TAG,
            assets_dir=str(tmp_path / "a"),
        )
        return studio_mod.DeviceShotDeps(
            config=config,
            client=fake,
            key=load_private_key(private),
            judge=MockJudge(),
            options=ds.ExecutorOptions(clock=Ticks(), sleep=lambda _s: None, poll_interval=0.0),
        )

    monkeypatch.setattr(studio_mod, "device_shot_factory", factory)
    monkeypatch.setattr(studio_mod, "DepartmentRunner", _FakeDepartments)
    # Until #262 composes shot_nodes into the breakdown, wire them after storyboard/animatic.
    graph = ProductionGraph("film", profile=load_profile("standard"), gates=GatePolicy())
    graph.add_nodes(
        [
            NodeSpec(id="storyboard", kind="storyboard", department="storyboard"),
            NodeSpec(id="animatic", kind="animatic", department="animatic-editor"),
            *shot_depts.shot_nodes(params=params),
        ]
    )
    run = studio_mod.default_runner(studio_mod.Production("film", store), graph)
    report = graph.run(run, critic=shot_depts.shot_critic(0.6))
    assert report.executed == ["storyboard", "animatic", "template_shots", "shots"], report
    assert built == ["film"]
    assert len(fake.submitted) == 2
    assert all(job["worker_tag"] == "tag-1" for job in fake.submitted)
    again = graph.run(run, critic=shot_depts.shot_critic(0.6))
    assert not again.executed and len(fake.submitted) == 2
