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
from spectracut.providers.schema import claude_output_schema
from spectracut.queue.flamenco import WorkerTag
from spectracut.queue.signing import (
    generate_keypair,
    load_private_key,
    load_public_key,
    parse_signed_job,
    verify_job,
)
from spectracut.spec.brand import load_brand_kit
from spectracut.storyboard.models import Board, Storyboard
from spectracut.studio import device_shot as ds
from spectracut.studio.departments import STAGE_DEPARTMENTS, all_departments
from spectracut.studio.departments import shots as sd
from spectracut.studio.gates import GateMode
from spectracut.studio.graph import ProductionGraph, RunContext, RunResult
from spectracut.studio.ledger import Usage
from spectracut.studio.node import NodeSpec, NodeStatus, Tier
from spectracut.studio.profiles import load_profile
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
    assert set(sd.TemplateShotsDepartment.inputs) == {"storyboard", "animatic"}
    assert set(sd.ShotsDepartment.inputs) == {sd.TEMPLATE_SHOTS}


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
            {"shots": [*_plan()["shots"], {"shot_id": "extra", "template": "typography"}]},  # type: ignore[misc]
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


def _template_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """The ``template`` property of one planned shot, with ``$ref``s resolved."""
    defs = schema.get("$defs", {})

    def resolve(node: dict[str, Any]) -> dict[str, Any]:
        ref = node.get("$ref")
        return resolve(defs[ref.rsplit("/", 1)[-1]]) if isinstance(ref, str) else node

    shot = resolve(resolve(schema["properties"]["shots"])["items"])
    return resolve(shot["properties"]["template"])


def test_shot_planner_schema_enumerates_every_registered_template() -> None:
    sent, _wrap = claude_output_schema(sd.ShotPlanner.output_schema())
    template = _template_schema(sent)
    options = [template, *template.get("anyOf", [])]
    enums = [o["enum"] for o in options if "enum" in o]
    assert enums == [sorted(TEMPLATES)], template
    assert {"type": "null"} in options  # a board that fits no template leaves it unset


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


def test_shot_critic_passes_a_scene_builder_only_record() -> None:
    record: JsonValue = {
        "shots": [],
        "scene_builder": [{"shot_id": "open", "description": "a city"}],
    }
    assert sd.shot_critic(0.6)(sd.shot_nodes()[0], record).passed
    with pytest.raises(ValidationError):
        sd.RenderedShots.model_validate({"shots": []})
