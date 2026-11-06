# SPDX-License-Identifier: AGPL-3.0-or-later
"""``studio new`` + ``studio run`` on the standard profile with the real departments."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import JsonValue
from typer.testing import CliRunner

from spectracut.cli import studio as studio_mod
from spectracut.cli.app import app
from spectracut.edit.timeline import Edit
from spectracut.providers.base import VisionScore
from spectracut.providers.mock_text import MockTextProvider, instance_of
from spectracut.queue.flamenco import WorkerTag
from spectracut.queue.signing import generate_keypair, load_private_key
from spectracut.spec.brief import AudienceProfile, Brief, ClaimSource, Persona, ProofPoint
from spectracut.storyboard.animatic import TempAudio
from spectracut.storyboard.models import Board, Storyboard, StyleFrames
from spectracut.studio import device_shot as ds
from spectracut.studio.departments import all_departments
from spectracut.studio.departments import art as art_depts
from spectracut.studio.departments import finishing as finishing_depts
from spectracut.studio.departments import shots as shot_depts
from spectracut.studio.gates import GateMode, GatePolicy
from spectracut.studio.graph import ProductionGraph, RunContext, RunResult
from spectracut.studio.node import NodeSpec, Tier
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
    unregistered = result.output.partition("no department registered yet for:")[2]
    # Finishing (#252) registers edit, render and delivery.
    for stage in ("render", "edit", "delivery", "voiceover"):
        assert stage not in unregistered.split()


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


def _respond(system: str, prompt: str, schema: dict[str, Any]) -> JsonValue:
    """The mock's schema instance, with the palette swatch the mock cannot infer (minProperties)."""
    output = instance_of(schema)
    if isinstance(output, dict) and isinstance(output.get("palette"), dict):
        output["palette"] = {"swatches": {"primary": "#1a2b3c"}}
    return output


def test_approving_each_gate_builds_concept_then_script(
    home: Path, brief_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(studio_mod, "text_provider_factory", lambda: MockTextProvider(_respond))
    # Style frames build beside the concept and render on the (fake) farm.
    monkeypatch.setattr(studio_mod, "device_shot_factory", _fake_farm(tmp_path, FakeFlamenco(), []))
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
    # Art direction builds beside the concept (it reads only the brief); style frames
    # read the brief and the art direction, never the script.
    # Voiceover (#251) reads only the script, so it builds here too; edit (#252) is
    # registered and waits on the animatic, so it is not reached.
    result = runner.invoke(app, ["studio", "run", "film"])
    assert "executed: script" in result.output
    assert "undeclared kind" not in result.output
    assert "no department registered" not in result.output


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


def _fake_farm(
    tmp_path: Path, fake: FakeFlamenco, built: list[str]
) -> Callable[[studio_mod.Production], studio_mod.DeviceShotDeps]:
    """A ``device_shot_factory`` on ``fake``; records each production it builds for."""
    private, _public = generate_keypair()

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

    return factory


SCRIPT_OUT: dict[str, Any] = {
    "concept_id": "concept-1",
    "lines": [{"beat": "hook", "vo": "Send a URL."}, {"beat": "cta"}],
}
ART_OUT: dict[str, Any] = {
    "mood_board": [{"ref": "clean studio product shot", "take": "soft key light"}],
    "palette": {"swatches": {"ink": "#101820", "signal": "#00c2a8"}},
    "materials": ["matte anodised aluminium"],
    "look": {"id": "clean"},
}
FRAMES_OUT: dict[str, Any] = {
    "frames": [
        {"id": "hero", "description": "Dashboard hero", "prompt": "clean dashboard on a plinth"},
        {"id": "cta", "description": "End card", "prompt": "logo on dark ink"},
    ]
}


def _fake_animatic_av(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """A fake mp4 preview and local voice for the animatic; returns the previews written."""
    written: list[Path] = []

    def preview(edit: Edit, out: Path) -> Path:
        out.write_bytes(b"mp4")
        written.append(out)
        return out

    def voice(text: str, path: Path) -> TempAudio:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"wav")
        return TempAudio(media_ref=str(path), duration_s=1.5)

    monkeypatch.setattr(studio_mod, "animatic_preview_factory", lambda: preview)
    monkeypatch.setattr(art_depts, "default_voice", lambda runtime=None: voice)
    return written


class _FakeDepartments:
    """Stands in for DepartmentRunner: canned script, art, frames, storyboard and shot plan."""

    kinds: list[str] = []

    def __init__(self, *_a: object, **_kw: object) -> None:
        pass

    def __call__(self, ctx: RunContext) -> RunResult:
        _FakeDepartments.kinds.append(ctx.spec.kind)
        if ctx.spec.kind == "script":
            return RunResult(output=SCRIPT_OUT)
        if ctx.spec.kind == art_depts.ART_DIRECTION:
            return RunResult(output=ART_OUT)
        if ctx.spec.kind == art_depts.STYLE_FRAMES:
            return RunResult(output=FRAMES_OUT)
        if ctx.spec.kind == shot_depts.SHOT_PLAN:
            plan = {"shots": [{"shot_id": "open", "template": "end-card", "slots": {"cta": "Go"}}]}
            return RunResult(output=plan)
        if ctx.spec.kind == "storyboard":
            board = Board(shot_id="open", beat="hook", action="the card lands", duration_s=2.0)
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
    built: list[str] = []
    monkeypatch.setattr(studio_mod, "device_shot_factory", _fake_farm(tmp_path, fake, built))
    monkeypatch.setattr(studio_mod, "DepartmentRunner", _FakeDepartments)
    _fake_animatic_av(monkeypatch)
    # The same storyboard -> animatic -> template_shots -> shots edges the breakdown wires;
    # no voiceover edge, so the animatic falls back to the local voice.
    graph = ProductionGraph("film", profile=load_profile("standard"), gates=GatePolicy())
    graph.add_nodes(
        [
            NodeSpec(id="script", kind="script", department="script-writer"),
            NodeSpec(id="storyboard", kind="storyboard", department="storyboard"),
            NodeSpec(id="art_direction", kind="art_direction", department="art-director"),
            NodeSpec(
                id="animatic",
                kind="animatic",
                department="animatic-editor",
                inputs=("script", "storyboard", "art_direction"),
            ),
            *shot_depts.shot_nodes(params=params),
        ]
    )
    run = studio_mod.default_runner(studio_mod.Production("film", store), graph)
    report = graph.run(run, critic=shot_depts.shot_critic(0.6))
    assert report.executed == [
        "script",
        "storyboard",
        "art_direction",
        "animatic",
        "template_shots",
        "shots",
    ], report
    assert built == ["film"]
    # The animatic's board renders on the farm too: one board plus two shot nodes.
    assert len(fake.submitted) == 3
    assert all(job["worker_tag"] == "tag-1" for job in fake.submitted)
    again = graph.run(run, critic=shot_depts.shot_critic(0.6))
    assert not again.executed and len(fake.submitted) == 3


def test_standard_style_frames_render_on_the_farm_through_default_runner(
    home: Path, brief_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = runner.invoke(app, ["studio", "new", str(brief_file), "--id", "film"])
    assert created.exit_code == 0, created.output
    store = ProductionStore(home)
    fake = FakeFlamenco()
    built: list[str] = []
    monkeypatch.setattr(studio_mod, "device_shot_factory", _fake_farm(tmp_path, fake, built))
    monkeypatch.setattr(studio_mod, "DepartmentRunner", _FakeDepartments)
    monkeypatch.setattr(_FakeDepartments, "kinds", [])
    previews = _fake_animatic_av(monkeypatch)
    auto = {k: GateMode.AUTO for k in art_depts.PREPRODUCTION_GATES}
    graph = ProductionGraph(
        "film", profile=load_profile("standard"), gates=GatePolicy(defaults=auto)
    )
    graph.add_nodes(
        [
            NodeSpec(id="brief", kind="brief", department="strategist"),
            NodeSpec(id="script", kind="script", department="script-writer", inputs=("brief",)),
            *art_depts.preproduction_nodes(),
        ]
    )
    run = studio_mod.default_runner(studio_mod.Production("film", store), graph)
    report = graph.run(run)
    assert report.failed == [], {n: graph.state(n).error for n in report.failed}
    assert art_depts.STYLE_FRAMES in report.executed and art_depts.ANIMATIC in report.executed

    frames = StyleFrames.model_validate(graph.output(art_depts.STYLE_FRAMES))
    assert len(frames.frames) == 2
    for frame in frames.frames:
        assert frame.render is not None and frame.tier == "final"
        assert Path(frame.render.path).is_file()
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", frame.render.sha256)
        assert frame.render.provider == "blender-farm"
    assert built == ["film"]
    # Two style frames plus the animatic's one board, all on the farm.
    assert len(fake.submitted) == len(frames.frames) + 1
    assert all(job["worker_tag"] == "tag-1" for job in fake.submitted)
    assert graph.ledger.totals(art_depts.STYLE_FRAMES).gpu_minutes > 0
    assert graph.ledger.totals(art_depts.ANIMATIC).gpu_minutes > 0

    edit = Edit.model_validate(graph.output(art_depts.ANIMATIC))
    assert art_depts.ANIMATIC not in _FakeDepartments.kinds
    board = next(t for t in edit.tracks if t.id == "picture").clips()[0]
    assert Path(board.media_ref).is_file() and board.media_ref.endswith("boards/open.png")
    assert [p.name for p in previews] == ["animatic.mp4"]


def test_default_runner_routes_finishing_nodes_to_the_finishing_runner(
    home: Path, brief_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """edit/render/delivery are built in code, never by the LLM text runner."""
    created = runner.invoke(app, ["studio", "new", str(brief_file), "--id", "fin"])
    assert created.exit_code == 0, created.output
    store = ProductionStore(home)
    text_kinds: list[str] = []

    class Text:
        def __init__(self, *_a: object, **_kw: object) -> None:
            pass

        def __call__(self, ctx: RunContext) -> RunResult:
            text_kinds.append(ctx.spec.kind)
            return RunResult(output={"node": ctx.spec.id})

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
            client=FakeFlamenco(),
            key=load_private_key(generate_keypair()[0]),
            judge=MockJudge(),
            options=ds.ExecutorOptions(clock=Ticks(), sleep=lambda _s: None, poll_interval=0.0),
        )

    routed: list[str] = []

    def edit(self: finishing_depts.FinishingRunner, ctx: RunContext) -> RunResult:
        routed.append(ctx.spec.kind)
        return RunResult(output={"node": ctx.spec.id})

    monkeypatch.setattr(studio_mod, "device_shot_factory", factory)
    monkeypatch.setattr(studio_mod, "DepartmentRunner", Text)
    monkeypatch.setattr(finishing_depts.FinishingRunner, "_edit", edit)
    graph = ProductionGraph("fin", profile=load_profile("standard"), gates=GatePolicy())
    graph.add_nodes([NodeSpec(id="script", kind="script", department="script-writer")])
    run = studio_mod.default_runner(studio_mod.Production("fin", store), graph)
    spec = finishing_depts.finishing_nodes()[0]
    ctx = RunContext(spec=spec, tier=Tier.FINAL, key="k", inputs={}, attempt=1)
    assert run(ctx).output == {"node": "edit"}
    assert routed == ["edit"] and text_kinds == [] and built == ["fin"]
    other = RunContext(spec=graph.spec("script"), tier=Tier.FINAL, key="k", inputs={}, attempt=1)
    run(other)
    assert text_kinds == ["script"] and built == ["fin"]
