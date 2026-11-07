# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shots (#250): storyboard boards rendered from the template library on the farm.

A fake Flamenco Manager "renders" by writing frames and ``report.json``; a fake text
runner plays the storyboard, animatic and the shot planner; a mock vision judge scores.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import JsonValue, ValidationError

from spectracut.providers.base import VisionScore
from spectracut.providers.mock_image import solid_png
from spectracut.providers.schema import claude_output_schema
from spectracut.qa.common import encode_png
from spectracut.queue.flamenco import WorkerTag
from spectracut.queue.signing import (
    generate_keypair,
    load_private_key,
    load_public_key,
    parse_signed_job,
    verify_job,
)
from spectracut.spec.brand import load_brand_kit
from spectracut.spec.recipe import Recipe
from spectracut.storyboard.models import Board, Storyboard
from spectracut.studio import device_shot as ds
from spectracut.studio.departments import STAGE_DEPARTMENTS, all_departments
from spectracut.studio.departments import shots as sd
from spectracut.studio.departments.capture import UI_CAPTURE, UICaptures
from spectracut.studio.gates import GateMode
from spectracut.studio.graph import ProductionGraph, RunContext, RunResult
from spectracut.studio.ledger import Usage
from spectracut.studio.node import NodeSpec, NodeStatus, Tier
from spectracut.studio.profiles import load_profile
from spectracut.studio.scene_builder import SceneBrief, SceneBuild, compile_calls
from spectracut.templates import TEMPLATES

ROOT = Path(__file__).resolve().parents[2]
KIT = ROOT / "brands" / "alterlab" / "brand-kit.yaml"
TAG = "3070laptopgpu"


@pytest.fixture(autouse=True)
def _qa_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """QA reports stay in tmp; no host OCR reads the fake (non-PNG) frames."""
    monkeypatch.setenv("SPECTRACUT_HOME", str(tmp_path / "qa-home"))
    monkeypatch.setattr("spectracut.qa.copy_ocr.available_reader", lambda: None)


class FakeFlamenco:
    """A Manager whose jobs complete at once, writing frames and report.json."""

    def __init__(self) -> None:
        self.submitted: list[dict[str, Any]] = []

    def list_tags(self) -> list[WorkerTag]:
        return [WorkerTag(id="tag-1", name=TAG)]

    def submit(self, job: dict[str, Any]) -> dict[str, Any]:
        self.submitted.append(job)
        settings = job["settings"]
        signed = json.loads(Path(settings["job_file"]).read_text(encoding="utf-8"))
        recipe_hash = signed["envelope"]["recipe_hash"]
        out = Path(settings["output_dir"])
        out.mkdir(parents=True, exist_ok=True)
        start, end = (int(x) for x in settings["frames"].split("-"))
        for frame in (start, end):
            (out / f"frame_{frame:04d}.png").write_bytes(f"{recipe_hash}:{frame}".encode())
        report = {"reports": [], "scene_graph_hash": "sha256:" + _sha(recipe_hash)}
        (out / "report.json").write_text(json.dumps(report), encoding="utf-8")
        return {"id": f"fl-{len(self.submitted)}"}

    def job_status(self, job_id: str) -> str:
        return "completed"


class Judge:
    name = "mock"

    def __init__(self, score: float = 0.9) -> None:
        self.score_value = score
        self.frames: list[Path] = []
        self.last_usage: dict[str, int] = {}

    def score(self, frame_path: Path, rubric: str) -> VisionScore:
        self.frames.append(frame_path)
        self.last_usage = {"input_tokens": 100, "output_tokens": 40}
        return VisionScore(score=self.score_value, notes="legible")


class Ticks:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        self.t += 90.0
        return self.t


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _plan(**over: dict[str, JsonValue]) -> dict[str, JsonValue]:
    shots: list[dict[str, JsonValue]] = [
        {"shot_id": "open", "template": "typography", "slots": {"body": "One view of every run"}},
        {"shot_id": "close", "template": "end-card", "variant": "tilted", "slots": {"cta": "Try"}},
    ]
    for shot in shots:
        shot.update(over.get(str(shot["shot_id"]), {}))
    return {"shots": list(shots)}


class Text:
    """The text runner: storyboard from its params, a stand-in animatic, the shot plan."""

    def __init__(self, plan: dict[str, JsonValue]) -> None:
        self.plan = plan
        self.kinds: list[str] = []

    def __call__(self, ctx: RunContext) -> RunResult:
        self.kinds.append(ctx.spec.kind)
        if ctx.spec.kind == sd.SHOT_PLAN:
            assert ctx.spec.department == sd.ShotPlanner.name
            return RunResult(output=self.plan, usage=Usage(ai_usage={"claude_requests": 1}))
        if ctx.spec.kind == "storyboard":
            boards = [
                Board(shot_id=b, action=str(ctx.spec.params.get(b, "show it")), duration_s=2.0)
                for b in ("open", "close")
            ]
            return RunResult(output=Storyboard(boards=boards).model_dump(mode="json"))
        return RunResult(output={"animatic": "locked"})


class Rig:
    def __init__(self, tmp_path: Path, plan: dict[str, JsonValue], score: float = 0.9) -> None:
        private, self.public = generate_keypair()
        self.fake, self.judge, self.text = FakeFlamenco(), Judge(score), Text(plan)
        self.shared = tmp_path / "shared"
        self.config = ds.DeviceShotConfig(
            flamenco_url="http://fake",
            shared=self.shared,
            worker_tag=TAG,
            assets_dir=str(tmp_path / "a"),
        )
        self.runner = sd.ShotRunner(
            self.text,
            all_departments(),
            config=self.config,
            client=self.fake,
            key=load_private_key(private),
            judge=self.judge,
            production_id="film",
            options=ds.ExecutorOptions(clock=Ticks(), sleep=lambda _s: None, poll_interval=0.0),
        )
        profile = load_profile("standard")
        auto = {"storyboard": GateMode.AUTO, "animatic": GateMode.AUTO}
        self.graph = ProductionGraph("film", profile=profile, gates=profile.gate_policy(nodes=auto))
        kit = load_brand_kit(KIT).model_dump(mode="json")
        self.graph.add_nodes(
            [
                NodeSpec(id="storyboard", kind="storyboard", department="storyboard"),
                NodeSpec(
                    id="animatic",
                    kind="animatic",
                    department="animatic-editor",
                    inputs=("storyboard",),
                ),
                *sd.shot_nodes(params={ds.BRAND_KIT_PARAM: kit}),
            ]
        )
        self.critic = sd.shot_critic(profile.critique.pass_score)

    def run(self) -> Any:
        return self.graph.run(self.runner, critic=self.critic)


def test_departments_are_registered_with_their_stage() -> None:
    depts = all_departments()
    for stage in (sd.TEMPLATE_SHOTS, sd.SHOTS):
        dept = depts[STAGE_DEPARTMENTS[stage]]
        assert stage in dept.produces and dept.uses_gpu
        assert dept.skill_pack is not None and dept.skill_pack.name == "motion-designer"
    assert depts["template-shots"] is sd.TemplateShotsDepartment
    assert depts["shots"] is sd.ShotsDepartment
    assert set(sd.TemplateShotsDepartment.inputs) == {"storyboard", "animatic", "ui_capture"}
    assert set(sd.ShotsDepartment.inputs) == {sd.TEMPLATE_SHOTS, "ui_capture"}


def test_shot_nodes_edges_and_params() -> None:
    template, shots = sd.shot_nodes("sb", "anim", params={ds.ASSETS_DIR_PARAM: "/a"})
    assert (template.id, template.kind, template.inputs) == (
        "template_shots",
        "template_shots",
        ("sb", "anim"),
    )
    assert (shots.id, shots.kind, shots.inputs) == ("shots", "shots", ("template_shots",))
    assert template.params == shots.params == {ds.ASSETS_DIR_PARAM: "/a"}


def test_one_signed_job_per_board_from_templates_pinned_to_the_worker_tag(tmp_path: Path) -> None:
    rig = Rig(tmp_path, _plan())
    report = rig.run()
    assert report.executed == ["storyboard", "animatic", "template_shots", "shots"], report
    # template_shots: 2 boards at preview; shots: 2 re-renders at the node tier.
    assert len(rig.fake.submitted) == 4
    for job in rig.fake.submitted:
        assert job["worker_tag"] == "tag-1"
        signed = parse_signed_job(Path(job["settings"]["job_file"]).read_text(encoding="utf-8"))
        envelope = verify_job(signed, load_public_key(rig.public))
        assert envelope.required_tag == TAG
    out = sd.RenderedShots.model_validate(rig.graph.output("template_shots"))
    assert [s.shot_id for s in out.shots] == ["open", "close"]
    kit = load_brand_kit(KIT)
    expected = TEMPLATES["typography"].preview(kit, {"body": "One view of every run"})
    assert out.shots[0].recipe_hash == expected.content_hash
    assert out.shots[0].worker_tag == TAG
    assert out.shots[1].template == "end-card" and out.shots[1].variant == "tilted"
    assert all(s.frames and s.vision_qa.score == 0.9 and s.qa.passed for s in out.shots)
    final = sd.RenderedShots.model_validate(rig.graph.output("shots"))
    assert [s.recipe for s in final.shots] == [s.recipe for s in out.shots]
    assert len(rig.judge.frames) == 4


def test_gpu_minutes_and_vision_qa_usage_land_in_the_ledger(tmp_path: Path) -> None:
    rig = Rig(tmp_path, _plan())
    rig.run()
    by_node = {e.node_id: e for e in rig.graph.ledger.entries()}
    for node in ("template_shots", "shots"):
        entry = by_node[node]
        assert entry.gpu_minutes > 0
        assert entry.ai_usage[ds.VISION_QA_METRIC] == 2
        assert entry.ai_usage[ds.VISION_QA_INPUT_TOKENS] == 200
    assert by_node["template_shots"].ai_usage["claude_requests"] == 1


def test_rerun_with_no_change_hits_the_cache_and_a_changed_board_rebuilds(tmp_path: Path) -> None:
    rig = Rig(tmp_path, _plan())
    rig.run()
    jobs = len(rig.fake.submitted)
    again = rig.run()
    assert not again.executed
    assert len(rig.fake.submitted) == jobs
    rig.graph.set_params("storyboard", {"open": "zoom on the dashboard"})
    changed = rig.run()
    assert "template_shots" in changed.executed
    assert len(rig.fake.submitted) > jobs


@pytest.mark.parametrize(
    ("plan", "match"),
    [
        (_plan(open={"template": "no-such"}), "unknown template"),
        (_plan(open={"variant": "sideways"}), "sideways"),
        (_plan(open={"slots": {"body": "A revolutionary dashboard"}}), "forbidden word"),
        (_plan(close={"slots": {}}), "close"),
        ({"shots": _plan()["shots"][:1]}, "unplanned: close"),  # type: ignore[index]
        (
            {
                "shots": [
                    *_plan()["shots"],  # type: ignore[misc]
                    {"shot_id": "extra", "template": "typography", "slots": {"body": "Hi"}},
                ]
            },
            "not on the storyboard: extra",
        ),
    ],
)
def test_bad_plans_fail_clearly_and_submit_nothing(
    tmp_path: Path, plan: dict[str, JsonValue], match: str
) -> None:
    rig = Rig(tmp_path, plan)
    report = rig.run()
    assert "template_shots" in report.failed
    assert rig.fake.submitted == []
    state = rig.graph.state("template_shots")
    assert match in (state.error or "")


def test_missing_storyboard_input_fails_clearly(tmp_path: Path) -> None:
    rig = Rig(tmp_path, _plan())
    spec = sd.shot_nodes()[0]
    ctx = RunContext(spec=spec, tier=Tier.PREVIEW, key="k", inputs={}, attempt=1)
    with pytest.raises(ValueError, match="needs a storyboard input"):
        rig.runner(ctx)


def test_low_vision_score_fails_the_critic_and_the_graph_revises_then_rejects(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path, _plan(), score=0.2)
    report = rig.run()
    assert "template_shots" in report.rejected
    assert rig.graph.status("template_shots") is NodeStatus.REJECTED
    outcomes = [e.outcome for e in rig.graph.ledger.entries() if e.node_id == "template_shots"]
    assert outcomes == ["revise", "rejected"]
    verdict = rig.graph.decision("template_shots")
    assert verdict is not None and verdict.critic is not None
    assert "vision QA 0.20 < 0.60" in verdict.critic.comment


def test_shot_critic_fails_on_render_qa_and_delegates_other_kinds() -> None:
    shot: dict[str, JsonValue] = {
        "shot_id": "open",
        "template": "typography",
        "recipe": {},
        "recipe_hash": "sha256:" + "0" * 64,
        "tier": "preview",
        "scene_graph_hash": "sha256:" + "1" * 64,
        "frames": {"frame_0001.png": "a"},
        "output_dir": "/o",
        "job_id": "j",
        "worker_tag": TAG,
        "qa": {"passed": False, "failures": ["black frame"]},
        "vision_qa": {"score": 0.95},
    }
    critic = sd.shot_critic(0.6)
    spec = sd.shot_nodes()[1]
    verdict = critic(spec, {"shots": [shot]})
    assert not verdict.passed and "render QA failed: black frame" in verdict.comment
    other = NodeSpec(id="script", kind="script", department="writer")
    assert critic(other, {"x": 1}).passed
    seen: list[str] = []

    def inner(s: NodeSpec, _o: JsonValue) -> Any:
        seen.append(s.id)
        return critic(spec, {"shots": [shot]})

    assert not sd.shot_critic(0.6, inner)(other, {}).passed
    assert seen == ["script"]
    # A shot node built by a custom runner (no rendered-shots record) is not crashed on.
    assert critic(spec, {"node": "shots"}).passed
    assert not sd.shot_critic(0.6, inner)(spec, {"node": "shots"}).passed
    assert seen == ["script", "shots"]


def _resolver(schema: dict[str, Any]) -> Any:
    defs = schema.get("$defs", {})

    def resolve(node: dict[str, Any]) -> dict[str, Any]:
        ref = node.get("$ref")
        return resolve(defs[ref.rsplit("/", 1)[-1]]) if isinstance(ref, str) else node

    return resolve


def _branches(schema: dict[str, Any]) -> list[dict[str, Any]]:
    """Each branch of one planned shot in the schema sent to Claude, ``$ref``s resolved."""
    resolve = _resolver(schema)
    shot = resolve(resolve(schema["properties"]["shots"])["items"])
    return [resolve(b) for b in shot["oneOf"]]


def test_shot_planner_schema_enumerates_every_registered_template() -> None:
    sent, _wrap = claude_output_schema(sd.ShotPlanner.output_schema())
    branches = _branches(sent)
    consts = [b["properties"]["template"].get("const") for b in branches]
    assert sorted(c for c in consts if c is not None) == sorted(TEMPLATES)
    # A board that fits no template leaves it unset (the scene-builder branch).
    scene = [b for b in branches if b["title"] == "SceneBuilderPlan"]
    assert [b["properties"]["template"]["type"] for b in scene] == ["null"]
    assert scene[0]["required"] == ["shot_id", "scene_builder"]


def test_shot_planner_schema_carries_each_template_slot_schema() -> None:
    sent, _wrap = claude_output_schema(sd.ShotPlanner.output_schema())
    resolve = _resolver(sent)
    by_id = {
        b["properties"]["template"]["const"]: b
        for b in _branches(sent)
        if "const" in b["properties"]["template"]
    }
    for template_id, template in TEMPLATES.items():
        branch = by_id[template_id]
        assert set(branch["required"]) >= {"shot_id", "template", "slots"}, template_id
        slots = resolve(branch["properties"]["slots"])
        model = template.slots.model_json_schema()
        assert slots["title"] == model["title"], template_id
        assert set(slots["properties"]) == set(model["properties"]), template_id
        assert sorted(slots.get("required", [])) == sorted(model.get("required", [])), template_id
        assert slots["additionalProperties"] is False, template_id
        described = [k for k, v in slots["properties"].items() if "description" in v]
        assert described, template_id
        variants = branch["properties"]["variant"]["anyOf"][0]["enum"]
        assert variants == [v.id for v in template.variants], template_id
    typography = resolve(by_id["typography"]["properties"]["slots"])
    assert typography["required"] == ["body"]
    assert "description" in typography["properties"]["body"]
    # The live failure (#293): brief fields and notes in slots, the copy line missing.
    bad = {"source_id": "brief-1", "notes": "insert the request"}
    with pytest.raises(ValidationError, match="body"):
        sd.ShotPlan.model_validate(
            {"shots": [{"shot_id": "s02b", "template": "typography", "slots": bad}]}
        )
    with pytest.raises(ValidationError, match="source_id"):
        sd.ShotPlan.model_validate(
            {
                "shots": [
                    {"shot_id": "s02b", "template": "typography", "slots": {"body": "Hi", **bad}}
                ]
            }
        )


_SHA = "0" * 64
SAMPLE_SLOTS: dict[str, dict[str, JsonValue]] = {
    "device-laptop": {
        "screen_frames": [{"path": "ui/frame_0001.png", "sha256": _SHA}],
        "hdri": {"path": "hdri/studio.exr", "sha256": _SHA},
    },
    "device-phone": {
        "screen_movie": {"path": "ui/app.mp4", "sha256": _SHA},
        "hdri": {"path": "hdri/studio.exr", "sha256": _SHA},
    },
    "end-card": {"cta": "Start free"},
    "hero-object": {"primitive": "uv_sphere", "animation": "reveal"},
    "typography": {"body": "One API, any page", "mode": "cutout"},
}


def test_every_registered_template_has_a_sample_reply() -> None:
    assert set(SAMPLE_SLOTS) == set(TEMPLATES)


@pytest.mark.parametrize("template_id", sorted(SAMPLE_SLOTS))
def test_sample_reply_per_template_validates_and_normalises(template_id: str) -> None:
    variant = TEMPLATES[template_id].variants[-1].id
    shot: dict[str, JsonValue] = {
        "shot_id": "open",
        "template": template_id,
        "variant": variant,
        "slots": SAMPLE_SLOTS[template_id],
    }
    plan = sd.ShotPlan.model_validate({"shots": [shot]})
    normalised = sd._planned_shot(plan.shots[0])
    assert normalised.template == template_id and normalised.variant == variant
    assert normalised.slots == SAMPLE_SLOTS[template_id]
    TEMPLATES[template_id].slots.model_validate(normalised.slots)
    # The reply round-trips through the runner's dump (department.validate_output).
    dumped = sd.ShotPlanner.validate_output("template_shots", {"shots": [shot]})
    sd.ShotPlan.model_validate(dumped)


def test_recorded_launch_film_shot_plan_still_validates() -> None:
    recorded = Path(__file__).parents[1] / "acceptance" / "recorded" / "ShotPlan.json"
    plan = sd.ShotPlan.model_validate(json.loads(recorded.read_text()))
    assert {sd._planned_shot(s).template for s in plan.shots} == {"typography"}


def test_board_that_fits_no_template_goes_to_the_scene_builder(tmp_path: Path) -> None:
    plan = _plan(close={"template": None, "variant": None, "slots": {}})
    plan["shots"][1]["scene_builder"] = "slow orbit around the glowing product"  # type: ignore[index, call-overload]
    rig = Rig(tmp_path, plan)
    report = rig.run()
    assert "template_shots" in report.executed and not report.failed, report
    out = sd.RenderedShots.model_validate(rig.graph.output("template_shots"))
    assert [s.shot_id for s in out.shots] == ["open"]
    assert [(s.shot_id, s.description) for s in out.scene_builder] == [
        ("close", "slow orbit around the glowing product")
    ]
    final = sd.RenderedShots.model_validate(rig.graph.output("shots"))
    assert final.scene_builder == out.scene_builder
    # One preview job and one tier re-render, both for the template board only.
    assert len(rig.fake.submitted) == 2


@pytest.mark.parametrize(
    "shot",
    [
        {"shot_id": "open"},
        {"shot_id": "open", "template": "typography", "scene_builder": "a wide city shot"},
        {"shot_id": "open", "template": "hero_object_move"},
    ],
)
def test_plan_shot_needs_exactly_one_known_template_or_scene_builder(
    shot: dict[str, JsonValue],
) -> None:
    with pytest.raises(ValidationError):
        sd.TemplateShot.model_validate(shot)


def test_motion_designer_prompt_lists_every_template() -> None:
    prompt = sd.ShotPlanner.pack().prompt
    for template_id in TEMPLATES:
        assert f"`{template_id}`" in prompt, template_id
    assert "scene_builder" in prompt


def test_shot_critic_fails_a_scene_builder_only_record() -> None:
    """Boards only pending for the scene builder leave nothing to review: not a pass."""
    record: JsonValue = {
        "shots": [],
        "scene_builder": [{"shot_id": "open", "description": "a city"}],
    }
    verdict = sd.shot_critic(0.6)(sd.shot_nodes()[0], record)
    assert not verdict.passed and "pending for the scene builder" in verdict.comment
    with pytest.raises(ValidationError):
        sd.RenderedShots.model_validate({"shots": []})


# ---- scene-built shots (#281) ----------------------------------------------------------


def _shot_record(output_dir: Path, score: float = 0.95) -> dict[str, JsonValue]:
    return {
        "shot_id": "open",
        "template": sd.SCENE_BUILDER,
        "recipe": {},
        "recipe_hash": "sha256:" + "0" * 64,
        "tier": "preview",
        "scene_graph_hash": "sha256:" + "1" * 64,
        "frames": {"frame_0001.png": "a", "frame_0048.png": "b"},
        "output_dir": str(output_dir),
        "job_id": "j",
        "worker_tag": TAG,
        "qa": {"passed": True, "failures": []},
        "vision_qa": {"score": score},
        "frame_end": 48,
        "description": "a glowing product on a plinth",
    }


def test_shot_critic_fails_a_blank_first_frame_whatever_its_score(tmp_path: Path) -> None:
    """Regression: a flat frame does not depict its description, even if the judge says so."""
    blank, busy = tmp_path / "blank", tmp_path / "busy"
    blank.mkdir()
    busy.mkdir()
    (blank / "frame_0001.png").write_bytes(solid_png(16, 16, (40, 40, 40)))
    (busy / "frame_0001.png").write_bytes(_checker_png())
    critic = sd.shot_critic(0.6)
    spec = sd.shot_nodes()[0]
    verdict = critic(spec, {"shots": [_shot_record(blank)]})
    assert not verdict.passed and "first frame is blank" in verdict.comment
    assert critic(spec, {"shots": [_shot_record(busy)]}).passed


def _checker_png(size: int = 16) -> bytes:
    pixels = bytes(255 if (x // 4 + y // 4) % 2 else 0 for y in range(size) for x in range(size))
    return encode_png(size, size, 1, pixels)


SCENE_CALLS: list[JsonValue] = [
    {"tool": "scene.clear", "args": {}},
    {"tool": "object.add_primitive", "args": {"kind": "cube", "name": "slab"}},
    {"tool": "light.add", "args": {"kind": "area", "name": "key", "energy": 500}},
    {"tool": "camera.add", "args": {"name": "cam", "location": [0, -6, 1]}},
    {"tool": "camera.use", "args": {"name": "cam"}},
]


class FakeSceneBuilder:
    """The scene builder: records each brief, returns a fixed recipe and usage."""

    def __init__(self, recipe: bool = True) -> None:
        self.briefs: list[SceneBrief] = []
        self.recipe = recipe

    def __call__(self, brief: SceneBrief) -> SceneBuild:
        self.briefs.append(brief)
        recipe = compile_calls(SCENE_CALLS, recipe_id=f"{brief.id}-r", shot_id=brief.id)
        return SceneBuild(
            id=brief.id,
            status="accepted" if self.recipe else "escalated",
            attempts=1,
            recipe=recipe if self.recipe else None,
            usage=Usage(gpu_minutes=1.0, ai_usage={"scene_builder_claude_requests": 2}),
        )


def _scene_rig(tmp_path: Path, builder: FakeSceneBuilder) -> Rig:
    plan = _plan(close={"template": None, "variant": None, "slots": {}})
    plan["shots"][1]["scene_builder"] = "slow orbit around the glowing product"  # type: ignore[index, call-overload]
    rig = Rig(tmp_path, plan)
    rig.runner.scene_builder = builder
    return rig


def test_scene_builder_shot_renders_as_an_animated_sequence_at_the_board_timing(
    tmp_path: Path,
) -> None:
    builder = FakeSceneBuilder()
    rig = _scene_rig(tmp_path, builder)
    report = rig.run()
    assert not report.failed, {n: rig.graph.state(n).error for n in report.failed}
    out = sd.RenderedShots.model_validate(rig.graph.output("template_shots"))
    assert [s.shot_id for s in out.shots] == ["open", "close"] and out.scene_builder == []
    scene = out.shots[1]
    assert scene.template == sd.SCENE_BUILDER
    # The storyboard's close board runs 2.0 s: 48 frames at 24 fps.
    assert scene.frame_end == 48 == sd.shot_frames(Board(shot_id="x", action="a", duration_s=2.0))
    assert scene.description is not None and "slow orbit" in scene.description
    tools = [step.tool for step in Recipe.model_validate(scene.recipe).steps]
    # The storyboard's board is static: lens and frame range, no camera move.
    assert tools[-2:] == ["camera.set_lens", "render.set_frame_range"]
    assert "camera.move" not in tools
    # The brief carries the board: lens, move and duration.
    [brief] = builder.briefs
    assert brief.id == "close" and "50 mm lens" in brief.description and "2 s" in brief.description
    jobs = [j for j in rig.fake.submitted if "close" in j["name"]]
    assert [j["settings"]["frames"] for j in jobs] == ["1-48", "1-48"]
    final = sd.RenderedShots.model_validate(rig.graph.output("shots"))
    assert [(s.template, s.frame_end) for s in final.shots][1] == (sd.SCENE_BUILDER, 48)
    entry = next(e for e in rig.graph.ledger.entries() if e.node_id == "template_shots")
    assert entry.ai_usage["scene_builder_claude_requests"] == 2


def test_scene_builder_with_no_recipe_fails_the_shot_node(tmp_path: Path) -> None:
    rig = _scene_rig(tmp_path, FakeSceneBuilder(recipe=False))
    report = rig.run()
    assert "template_shots" in report.failed
    assert "produced no recipe" in (rig.graph.state("template_shots").error or "")


@pytest.mark.parametrize(
    ("move", "keyed"),
    [("static", False), ("push", True), ("orbit", True), ("handheld", True)],
)
def test_motion_steps_key_the_board_move_lens_and_frame_range(move: str, keyed: bool) -> None:
    board = Board.model_validate(
        {"shot_id": "s", "action": "a", "duration_s": 1.5, "move": move, "lens_mm": 50}
    )
    recipe = compile_calls(SCENE_CALLS, recipe_id="r", shot_id="s")
    calls = dict(sd.motion_steps(board, recipe))
    assert calls["camera.set_lens"] == {"mm": 50}
    assert calls["render.set_frame_range"] == {"frame_start": 1, "frame_end": 36}
    assert ("camera.move" in calls) is keyed
    if keyed:
        move_args: Any = calls["camera.move"]
        keys = move_args["keys"]
        assert keys[0]["frame"] == 1 and keys[0] != keys[-1]
    # A recipe that already keys its camera keeps its own move.
    moved = sd.with_motion(recipe, board)
    assert [s.tool for s in moved.steps].count("camera.move") == int(keyed)
    again = dict(sd.motion_steps(board, moved))
    assert "camera.move" not in again


# ---- device shots from UI captures (#339) ----------------------------------------


def _capture(tmp_path: Path, frames: int = 3, fps: int = 12) -> UICaptures:
    """A laptop capture of beat 'dashboard': ``frames`` distinct PNGs at ``fps``."""
    root = tmp_path / "captures"
    root.mkdir(exist_ok=True)
    clip = []
    for n in range(frames):
        path = root / f"dash_{n:04d}.png"
        path.write_bytes(solid_png(8, 8, (10 * n, 20, 30)))
        clip.append({"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    frames_sha = hashlib.sha256("\n".join(f["sha256"] for f in clip).encode()).hexdigest()
    return UICaptures.model_validate(
        {
            "product_id": "alterlab",
            "devices": ["laptop"],
            "captures": [
                {
                    "key": "beat:dashboard",
                    "beat": "dashboard",
                    "route": "/runs",
                    "url": "http://127.0.0.1:9/runs",
                    "device": "laptop",
                    "fps": fps,
                    "clip": clip,
                    "still": clip[-1],
                    "manifest": str(root / "manifest.json"),
                    "events": str(root / "events.json"),
                    "text": str(root / "text.json"),
                    "provenance": {
                        "product_id": "alterlab",
                        "url": "http://127.0.0.1:9/runs",
                        "steps": [],
                        "device": "laptop",
                        "spec_sha256": _sha("spec"),
                        "frames_sha256": frames_sha,
                        "rig": "@spectracut/capture@0",
                        "demo_check": "passed",
                        "deterministic": True,
                    },
                }
            ],
        }
    )


class DeviceText(Text):
    """Storyboard with a 3 s device push on 'dashboard' and a 1 s abstract close."""

    def __init__(self, plan: dict[str, JsonValue], captures: UICaptures, close_s: float) -> None:
        super().__init__(plan)
        self.captures, self.close_s = captures, close_s

    def __call__(self, ctx: RunContext) -> RunResult:
        if ctx.spec.kind == "storyboard":
            boards = [
                Board.model_validate(
                    {
                        "shot_id": "open",
                        "beat": "dashboard",
                        "action": "the runs dashboard",
                        "duration_s": 3.0,
                        "move": "push",
                        "device": "laptop",
                    }
                ),
                Board(shot_id="close", action="end card", duration_s=self.close_s),
            ]
            return RunResult(output=Storyboard(boards=boards).model_dump(mode="json"))
        if ctx.spec.kind == UI_CAPTURE:
            return RunResult(output=self.captures.model_dump(mode="json"))
        return super().__call__(ctx)


def _device_rig(tmp_path: Path, close_s: float = 1.0) -> tuple[Rig, UICaptures]:
    captures = _capture(tmp_path)
    rig = Rig(tmp_path, _plan())
    rig.text = DeviceText(rig.text.plan, captures, close_s)
    rig.runner.inner = rig.text
    graph = ProductionGraph("film", profile=rig.graph.profile, gates=rig.graph.gates)
    specs = []
    for node_id in rig.graph.order():
        spec = rig.graph.spec(node_id)
        if spec.kind in (sd.TEMPLATE_SHOTS, sd.SHOTS):
            spec = spec.model_copy(update={"inputs": (*spec.inputs, UI_CAPTURE)})
        specs.append(spec)
    kit_assets = tmp_path / "kit-assets"
    kit_assets.mkdir()
    for i, spec in enumerate(specs):
        if spec.kind in (sd.TEMPLATE_SHOTS, sd.SHOTS):
            params = {**spec.params, ds.ASSETS_DIR_PARAM: str(kit_assets)}
            specs[i] = spec.model_copy(update={"params": params})
    specs.append(NodeSpec(id=UI_CAPTURE, kind=UI_CAPTURE, department="ui-capture"))
    graph.add_nodes(specs)
    rig.graph = graph
    return rig, captures


def test_device_board_renders_on_the_device_template_with_the_capture_on_screen(
    tmp_path: Path,
) -> None:
    rig, captures = _device_rig(tmp_path)
    report = rig.run()
    assert not report.failed, {n: rig.graph.state(n).error for n in report.failed}
    out = sd.RenderedShots.model_validate(rig.graph.output("template_shots"))
    device = out.shots[0]
    # The planner said 'typography'; the device board renders on device-laptop anyway.
    assert device.template == "device-laptop" and device.variant == "three-quarter"
    assert device.capture == "beat:dashboard" and device.frame_end == 72
    recipe = Recipe.model_validate(device.recipe)
    steps = {s.tool: s.args for s in recipe.steps}
    # The capture is the screen: 3 frames at 12 fps resampled to 72 frames at 24 fps.
    screen: Any = steps["device.add"]["screen"]
    shas = [f["sha256"] for f in screen["frames"]]
    clip = [f.sha256 for f in captures.captures[0].clip]
    assert set(shas) == set(clip) and len(shas) == 72
    assert all(f["path"].startswith("captures/") for f in screen["frames"])
    # Focus on the device body, then a focus pull keyed on the camera.
    dofs = [s.args for s in recipe.steps if s.tool == "camera.set_dof"]
    assert dofs[0] == {"focus_object": "device.body", "fstop": sd.DEVICE_FSTOP}
    # The Look's render rules follow (the AlterLab Look turns motion blur and DoF off).
    assert dofs[1:] == [{"enabled": False}]
    assert steps["render.set_property"] == {"property": "render.use_motion_blur", "value": False}
    pulls = [
        s.args for s in recipe.steps if s.tool == "camera.move" and "focus_distance" in str(s.args)
    ]
    keys: Any = pulls[0]["keys"]
    assert keys[0]["focus_distance"] < keys[-1]["focus_distance"]
    # The board's push is keyed and the frame range is the board's.
    assert any(s.tool == "camera.move" and s.args is not pulls[0] for s in recipe.steps)
    assert steps["render.set_frame_range"] == {"frame_start": 1, "frame_end": 72}
    # The capture PNGs were staged beside the kit assets, verified by sha256, even
    # with an assets-dir override (it cannot hold the production's captures).
    assert rig.config.assets_dir
    staged = next((rig.shared / "assets").iterdir())
    assert (staged / screen["frames"][0]["path"]).read_bytes() == Path(
        captures.captures[0].clip[0].path
    ).read_bytes()
    # The final shots node re-renders it at tier with the same recipe and frame range.
    final = sd.RenderedShots.model_validate(rig.graph.output("shots"))
    assert final.shots[0].recipe == device.recipe
    assert (final.shots[0].capture, final.shots[0].frame_end) == ("beat:dashboard", 72)
    jobs = [j for j in rig.fake.submitted if "open" in j["name"]]
    assert [j["settings"]["frames"] for j in jobs] == ["1-72", "1-72"]
    # The abstract board is unchanged: no capture recorded.
    assert out.shots[1].template == "end-card" and out.shots[1].capture is None
    assert "capture" not in out.shots[1].model_dump()


def test_device_shots_under_the_runtime_share_fail_the_shot_plan(tmp_path: Path) -> None:
    rig, _captures = _device_rig(tmp_path, close_s=3.0)
    report = rig.run()
    assert "template_shots" in report.failed
    error = rig.graph.state("template_shots").error or ""
    assert "50%" in error and "60%" in error and "close" in error
    assert not rig.fake.submitted


def test_device_board_without_a_matching_capture_fails_naming_board_and_key(
    tmp_path: Path,
) -> None:
    captures = _capture(tmp_path)
    board = Board.model_validate(
        {"shot_id": "s", "action": "a", "duration_s": 1.0, "device": "phone", "beat": "dashboard"}
    )
    with pytest.raises(ValueError, match=r"'s'.*phone.*beat:dashboard"):
        sd.board_capture(captures, board)


def test_capture_frames_resample_to_the_shot_and_hold_the_last_frame(tmp_path: Path) -> None:
    capture = _capture(tmp_path, frames=2, fps=12).captures[0]
    pinned, extra = sd.capture_frames(capture, 6)
    clip = [f.sha256 for f in capture.clip]
    assert [p.sha256 for p in pinned] == [clip[0]] * 2 + [clip[1]] * 4
    assert set(extra) == {p.path for p in pinned}
    one = sd.capture_frames(_capture(tmp_path, frames=1).captures[0], 10)[0]
    assert len(one) == 1


def test_stage_assets_extra_files_join_the_manifest_and_default_is_unchanged(
    tmp_path: Path,
) -> None:
    src = tmp_path / "kit"
    (src / "ui").mkdir(parents=True)
    (src / "ui" / "a.png").write_bytes(b"a")
    plain = ds.stage_assets(src, tmp_path / "s1", {})
    assert ds.stage_assets(src, tmp_path / "s2", {}, None) == plain
    capture = tmp_path / "cap.png"
    capture.write_bytes(b"cap")
    sha = hashlib.sha256(b"cap").hexdigest()
    staged = ds.stage_assets(
        src, tmp_path / "s3", {"captures/x/0001.png": sha}, {"captures/x/0001.png": capture}
    )
    assert staged != plain
    assert (tmp_path / "s3" / "assets" / staged / "captures/x/0001.png").read_bytes() == b"cap"
    with pytest.raises(ValueError, match="relative"):
        ds.stage_assets(src, tmp_path / "s4", {}, {"../x.png": capture})
