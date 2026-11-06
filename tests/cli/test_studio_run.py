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
from spectracut.studio.departments import sound as sound_depts
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

    def no_sound(_production: studio_mod.Production) -> studio_mod.SoundDeps:
        raise AssertionError("text-only run built the sound deps")

    monkeypatch.setattr(studio_mod, "device_shot_factory", no_farm)
    monkeypatch.setattr(studio_mod, "sound_factory", no_sound)
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


def test_default_runner_routes_sound_nodes_to_the_sound_runner(
    home: Path, brief_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """voiceover/sound_design/music/mix are built in code on sound_factory's deps (#253)."""
    created = runner.invoke(app, ["studio", "new", str(brief_file), "--id", "snd"])
    assert created.exit_code == 0, created.output
    store = ProductionStore(home)
    text_kinds: list[str] = []

    class Text:
        def __init__(self, *_a: object, **_kw: object) -> None:
            pass

        def __call__(self, ctx: RunContext) -> RunResult:
            text_kinds.append(ctx.spec.kind)
            return RunResult(output={"node": ctx.spec.id})

    ardour = object()
    built: list[str] = []

    def factory(production: studio_mod.Production) -> studio_mod.SoundDeps:
        built.append(production.id)
        return studio_mod.SoundDeps(ardour=ardour, elevenlabs_configured=lambda: False)  # type: ignore[arg-type]

    routed: list[str] = []

    def build(self: sound_depts.SoundRunner, ctx: RunContext) -> RunResult:
        assert self.ardour is ardour and self.elevenlabs_configured() is False
        assert self.workdir == store.path("snd") / "sound"
        routed.append(ctx.spec.kind)
        return RunResult(output={"node": ctx.spec.id})

    monkeypatch.setattr(studio_mod, "DepartmentRunner", Text)
    monkeypatch.setattr(studio_mod, "sound_factory", factory)
    for method in ("_voiceover", "_sound_design", "_music", "_mix"):
        monkeypatch.setattr(sound_depts.SoundRunner, method, build)
    graph = ProductionGraph("snd", profile=load_profile("standard"), gates=GatePolicy())
    graph.add_nodes([NodeSpec(id="script", kind="script", department="script-writer")])
    run = studio_mod.default_runner(studio_mod.Production("snd", store), graph)

    script = RunContext(spec=graph.spec("script"), tier=Tier.FINAL, key="k", inputs={}, attempt=1)
    run(script)
    assert text_kinds == ["script"] and built == []
    for spec in sound_depts.sound_nodes():
        ctx = RunContext(spec=spec, tier=Tier.PREVIEW, key="k", inputs={}, attempt=1)
        assert run(ctx).output == {"node": spec.id}
    assert routed == [
        sound_depts.VOICEOVER,
        sound_depts.SOUND_DESIGN,
        sound_depts.MUSIC,
        sound_depts.MIX,
    ]
    assert text_kinds == ["script"] and built == ["snd"]


# ---- #289: the model runtime on the model host -------------------------------------


def _register(home: Path, *hosts: tuple[str, bool]) -> None:
    from spectracut.cli.render import HostEntry, HostRegistry, hosts_path
    from spectracut.queue.executors.base import GpuCard
    from spectracut.queue.profile import RenderProfile

    card = GpuCard(name="RTX 3070 Laptop", vram_gb=8)
    registry = HostRegistry(
        hosts=tuple(
            HostEntry(
                name=name,
                cpus=8,
                mem_gb=28,
                gpus=(card,) if gpu else (),
                profile=RenderProfile(blender_version="4.2.0", os="windows"),
            )
            for name, gpu in hosts
        )
    )
    path = hosts_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(registry.model_dump_json(), encoding="utf-8")


@pytest.fixture
def control_plane(home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Outside mock mode, on a control plane that is not the model host; never spawns ssh."""
    import subprocess

    monkeypatch.delenv("SPECTRACUT_PROVIDERS", raising=False)
    monkeypatch.delenv("SPECTRACUT_MODEL_HOST", raising=False)
    monkeypatch.setattr(studio_mod.socket, "gethostname", lambda: "control")

    def no_ssh(*_a: object, **_kw: object) -> None:
        raise AssertionError("resolving the model runtime must not spawn ssh")

    monkeypatch.setattr(subprocess, "Popen", no_ssh)
    return home


def _production(home: Path, pid: str = "snd") -> studio_mod.Production:
    return studio_mod.Production(pid, ProductionStore(home))


def test_default_sound_builds_runtime_on_registered_model_host(control_plane: Path) -> None:
    from spectracut.providers.local.remote import RemoteLoader
    from spectracut.providers.local.runtime import ModelRuntime

    _register(control_plane, ("mrdubey-1", False), ("legion", True))
    deps = studio_mod.default_sound(_production(control_plane))
    assert isinstance(deps.runtime, ModelRuntime)
    assert deps.runtime_problem is None
    loader = deps.runtime._loader
    assert isinstance(loader, RemoteLoader)
    assert (loader.host, loader.os) == ("legion", "windows")


def test_the_model_host_can_be_named_in_the_env_or_config(
    control_plane: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from spectracut.providers.local.remote import RemoteLoader

    _register(control_plane, ("legion", True), ("mrdubey-1", True))
    runtime, problem = studio_mod.model_runtime(control_plane)
    assert runtime is None and problem is not None and "several GPU hosts" in problem
    (tmp_path / "config.toml").write_text('[models]\nhost = "mrdubey-1"\n', encoding="utf-8")
    runtime, _ = studio_mod.model_runtime(control_plane)
    assert runtime is not None and isinstance(runtime._loader, RemoteLoader)
    assert runtime._loader.host == "mrdubey-1"
    monkeypatch.setenv("SPECTRACUT_MODEL_HOST", "legion")
    runtime, _ = studio_mod.model_runtime(control_plane)
    assert runtime is not None and isinstance(runtime._loader, RemoteLoader)
    assert runtime._loader.host == "legion"


def test_on_the_model_host_models_load_in_process(
    control_plane: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from spectracut_host.models import load_model

    _register(control_plane, ("legion", True))
    monkeypatch.setattr(studio_mod.socket, "gethostname", lambda: "legion")
    runtime, problem = studio_mod.model_runtime(control_plane)
    assert problem is None and runtime is not None and runtime._loader is load_model


def _sound_edit() -> dict[str, JsonValue]:
    from spectracut.edit.timeline import Clip, Track

    picture = [
        Clip(id="pic-a", shot_id="a", media_ref="board:a", source_out=48),
        Clip(id="pic-b", shot_id="b", media_ref="board:b", source_out=48),
    ]
    edit = Edit(id="animatic", tracks=[Track(id="picture", kind="video", items=picture)])
    return edit.model_dump(mode="json")


def _sound_design_ctx() -> RunContext:
    spec = next(n for n in sound_depts.sound_nodes() if n.kind == sound_depts.SOUND_DESIGN)
    inputs: dict[str, JsonValue] = {spec.inputs[0]: _sound_edit()}
    return RunContext(spec=spec, tier=Tier.PREVIEW, key="k", inputs=inputs, attempt=1)


@pytest.mark.parametrize(
    ("config", "named"),
    [
        ('[models]\nhost = "legion"\n', "model host 'legion' is not registered"),
        ("", "no model host"),
    ],
)
def test_a_missing_model_host_is_an_actionable_error_naming_it(
    control_plane: Path, tmp_path: Path, config: str, named: str
) -> None:
    from spectracut.providers.base import ProviderError

    (tmp_path / "config.toml").write_text(config, encoding="utf-8")
    _register(control_plane, ("mrdubey-1", False))
    production = _production(control_plane)
    deps = studio_mod.default_sound(production)
    assert deps.runtime is None and deps.runtime_problem is not None
    sound = sound_depts.SoundRunner(
        lambda ctx: RunResult(output={}),
        studio_mod.DEPARTMENTS,
        workdir=tmp_path / "sound",
        runtime=deps.runtime,
        runtime_problem=deps.runtime_problem,
    )
    with pytest.raises(ProviderError) as err:
        sound(_sound_design_ctx())
    message = str(err.value)
    assert "sound_design" in message and named in message
    assert "legion" in message and "spectracut hosts add" in message


class _ServeChannel:
    """``ssh legion spectracut-host models-serve`` in-process: the real host loop on pipes."""

    def __init__(self, loads: list[str]) -> None:
        import os
        import threading

        from spectracut.providers.local.registry import MockLocalProvider
        from spectracut_host.models import serve

        r_in, w_in = os.pipe()
        r_out, w_out = os.pipe()
        self.stdin = open(w_in, "w", encoding="utf-8")  # noqa: SIM115
        self.stdout = open(r_out, encoding="utf-8")  # noqa: SIM115
        host_in = open(r_in, encoding="utf-8")  # noqa: SIM115
        host_out = open(w_out, "w", encoding="utf-8")  # noqa: SIM115

        class Model:
            def __init__(self, kind: str) -> None:
                self.kind = kind

            def generate(self, prompt: str, **options: Any) -> Path:
                out = Path(options.pop("out_path"))
                return MockLocalProvider(self.kind).generate(prompt, out, **options)

            def close(self) -> None:
                loads.append("close")

        def loader(spec: Any) -> Model:
            loads.append(spec.id)
            return Model(spec.kind)

        def run() -> None:
            with host_in, host_out:
                serve(host_in, host_out, loader)

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def wait(self, timeout: float | None = None) -> int:
        self.thread.join(timeout)
        return 0

    def kill(self) -> None:
        pass


def test_sound_design_runs_on_the_model_host_and_records_gpu_minutes(
    control_plane: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """studio run's sound_design reaches the models on legion; usage lands in the ledger."""
    from spectracut.providers.local.remote import RemoteLoader
    from spectracut.providers.local.runtime import ModelRuntime
    from spectracut.queue.executors.base import GpuCard, LeasePool

    loads: list[str] = []
    argv: list[list[str]] = []

    def spawn(cmd: Any) -> _ServeChannel:
        argv.append(list(cmd))
        return _ServeChannel(loads)

    runtime = ModelRuntime(
        RemoteLoader("legion", "windows", spawn=spawn),  # type: ignore[arg-type]
        LeasePool(8, 28, (GpuCard(name="RTX 3070 Laptop", vram_gb=8),)),
        host="legion",
    )
    resolved: list[Path] = []

    def factory(home: Path) -> studio_mod.ModelRuntimeResult:
        resolved.append(home)
        return runtime, None

    monkeypatch.setattr(studio_mod, "model_runtime_factory", factory)
    spec = next(n for n in sound_depts.sound_nodes() if n.kind == sound_depts.SOUND_DESIGN)
    graph = ProductionGraph(
        "snd", gates=GatePolicy(defaults={"animatic": GateMode.AUTO, spec.kind: GateMode.AUTO})
    )
    graph.add_nodes([NodeSpec(id="animatic", kind="animatic", department="stub"), spec])
    inner = studio_mod.default_runner(_production(control_plane), graph)

    def run(ctx: RunContext) -> RunResult:
        if ctx.spec.kind == "animatic":
            return RunResult(output=_sound_edit())
        return inner(ctx)

    report = graph.run(run)
    assert report.failed == [], {n: graph.state(n).error for n in report.failed}
    entry = next(e for e in graph.ledger.entries() if e.node_id == spec.id)
    assert entry.gpu_minutes > 0
    assert len(argv) == 1 and argv[0][3] == "legion"
    assert loads == ["ezaudio-xl", "close"]
    assert resolved == [control_plane]


def test_sound_and_preproduction_share_one_model_runtime(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = object()
    calls: list[Path] = []

    def factory(h: Path) -> studio_mod.ModelRuntimeResult:
        calls.append(h)
        return runtime, None  # type: ignore[return-value]

    monkeypatch.setattr(studio_mod, "model_runtime_factory", factory)
    seen: list[object] = []

    def explorer(config: object = None, *, runtime: object = None) -> None:
        seen.append(runtime)

    class Pre:
        def __init__(self, *_a: object, **_kw: object) -> None:
            pass

        def __call__(self, ctx: RunContext) -> RunResult:
            return RunResult(output={"node": ctx.spec.id})

    monkeypatch.setattr(art_depts, "default_explorer", explorer)
    monkeypatch.setattr(art_depts, "PreproductionRunner", Pre)
    monkeypatch.setattr(studio_mod, "animatic_preview_factory", lambda: None)
    production = _production(home)
    graph = ProductionGraph("snd", profile=load_profile("standard"), gates=GatePolicy())
    graph.add_nodes([NodeSpec(id="style", kind=art_depts.STYLE_FRAMES, department="art")])
    run = studio_mod.default_runner(production, graph)
    run(RunContext(spec=graph.spec("style"), tier=Tier.FINAL, key="k", inputs={}, attempt=1))
    deps = studio_mod.default_sound(production)
    assert seen == [runtime] and deps.runtime is runtime
    assert calls == [home]
