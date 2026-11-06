# SPDX-License-Identifier: AGPL-3.0-or-later
"""Critic v1: vision rubric scoring, sequence review, pairwise choice and the revise loop (#30)."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import ClassVar

import pytest
from pydantic import BaseModel, Field, JsonValue

from spectracut.canon.schema import Canon
from spectracut.critique.critic import (
    OVERALL,
    Candidate,
    PairwiseJudge,
    PairwiseVerdict,
    StudioCritic,
    critic_for_runner,
    critique_prompt,
    extract_artifact,
    review_sequence,
)
from spectracut.critique.revise import (
    ReviseLoop,
    RevisionRequest,
    best_of_n_runner,
    recipe_diff,
)
from spectracut.providers.base import VisionScore
from spectracut.providers.mock_text import MockTextProvider
from spectracut.providers.mock_vision import MockVisionJudge
from spectracut.qa.common import Gray, encode_png, gray_png
from spectracut.spec._base import StrictModel
from spectracut.spec.brief import AudienceProfile, Brief, ClaimSource, Persona, ProofPoint
from spectracut.studio import (
    CriticVerdict,
    Department,
    DepartmentRunner,
    GateMode,
    NodeSpec,
    NodeStatus,
    ProductionGraph,
    QualityProfile,
    RunContext,
    RunResult,
    Usage,
    load_profile,
)
from spectracut.studio.producer import Producer

VISUAL = {"style_frames", "storyboard", "animatic", "template_shots", "shots", "render"}


def _brief() -> Brief:
    return Brief(
        id="spellwise",
        product="Spellwise",
        audience=AudienceProfile(personas=[Persona(name="Dev", description="ships often")]),
        sources=[ClaimSource(id="docs", title="Docs", ref="https://example.test/docs")],
        proof_points=[ProofPoint(claim="Fast spellcheck", source_id="docs")],
        cta="Try it",
    )


def _png(path: Path, frame: Gray) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gray_png(frame))
    return path


def _flat(value: float, w: int = 32, h: int = 16) -> Gray:
    return Gray(w, h, tuple([value] * (w * h)))


def _bar(x: int, w: int = 32, h: int = 16) -> Gray:
    """A dark frame with a bright 4 px bar at column ``x`` (constant mean luma)."""
    return Gray(w, h, tuple(0.9 if x <= c < x + 4 else 0.1 for _ in range(h) for c in range(w)))


class ScriptedVision:
    """Vision judge whose score is looked up by file stem; records every call."""

    name = "scripted-vision"

    def __init__(self, scores: dict[str, float] | None = None, default: float = 0.9) -> None:
        self.scores = scores or {}
        self.default = default
        self.calls: list[tuple[str, str]] = []

    def score(self, frame_path: Path, rubric: str) -> VisionScore:
        self.calls.append((Path(frame_path).stem, rubric))
        value = self.scores.get(Path(frame_path).stem, self.default)
        return VisionScore(score=value, notes=f"{Path(frame_path).stem}: key light too flat")


def _text_judge(spec: NodeSpec, prompt: str, output: JsonValue) -> CriticVerdict:
    return CriticVerdict(passed=True, scores={"clarity": 0.8}, comment=f"{spec.kind} reads clearly")


def _critic(
    vision: ScriptedVision, profile: QualityProfile | None = None, **kw: object
) -> StudioCritic:
    return StudioCritic(
        vision,
        rubric_for=lambda spec: f"rubric for {spec.kind}: legible UI, motivated light",
        text_judge=_text_judge,
        profile=profile if profile is not None else load_profile("standard"),
        **kw,  # type: ignore[arg-type]
    )


SHOT = NodeSpec(id="shot-01", kind="shots", department="lighter")


class UsageVision(ScriptedVision):
    """Vision judge that reports tokens for its last call, like the Claude CLI judge."""

    def score(self, frame_path: Path, rubric: str) -> VisionScore:
        self.last_usage = {"input_tokens": 10, "cache_read_input_tokens": 90, "output_tokens": 5}
        return super().score(frame_path, rubric)


def test_critic_verdict_carries_judge_usage_once_per_review(tmp_path: Path) -> None:
    frame = _png(tmp_path / "frame.png", _bar(4))
    out: JsonValue = {"frames": [str(frame)]}
    critic = _critic(UsageVision(), samples_profile())
    first = critic.review(SHOT, out)
    assert first.usage == {
        "critic_requests": 1,
        "critic_input_tokens": 100,
        "critic_output_tokens": 5,
    }
    assert critic.review(SHOT, out).usage == {}  # cached: tokens were already charged
    assert _critic(ScriptedVision()).review(SHOT, out).usage == {}  # no last_usage


def samples_profile() -> QualityProfile:
    return load_profile("standard")


# ---- R1: every gate in a standard production gets a score and notes -----------


def test_every_gate_in_a_standard_production_gets_a_critic_score_and_notes(tmp_path: Path) -> None:
    profile = load_profile("standard")
    producer = Producer()
    plan = producer.breakdown(_brief(), profile, duration_s=30)
    graph = producer.new_graph("spellwise", plan, profile)
    frame = _png(tmp_path / "frame.png", _bar(4))

    def runner(ctx: RunContext) -> RunResult:
        if ctx.spec.kind in VISUAL:
            return RunResult(output={"node": ctx.spec.id, "frames": [str(frame)]})
        return RunResult(output={"node": ctx.spec.id, "text": "Spellwise checks as you type"})

    vision = ScriptedVision()
    critic = _critic(vision, profile)
    for _ in range(40):
        report = producer.run(graph, runner, critic=critic)
        for nid in report.run.awaiting_approval:
            graph.approve(nid, "ok")
        if not report.run.executed and not report.run.awaiting_approval:
            break

    gated = [n for n in graph.order() if graph.gate_for(graph.spec(n)) is not GateMode.AUTO]
    assert gated == list(graph.order())  # standard gates every stage
    for nid in gated:
        assert graph.status(nid) is NodeStatus.CLEAN, nid
        decision = graph.decision(nid)
        assert decision is not None and decision.critic is not None, nid
        assert decision.critic.scores and OVERALL in decision.critic.scores, nid
        assert decision.critic.comment.strip(), nid
        if graph.spec(nid).kind in VISUAL:
            assert "frame-1" in decision.critic.scores
            assert "key light too flat" in decision.critic.comment
    assert vision.calls, "visual stages were scored by the vision judge"


# ---- R2 / R8: failing shot revised, re-scored, then escalated ------------------


def _shot_loop(
    tmp_path: Path, scores: dict[str, float], *, budget: int, n: int = 1, window: int = 3
) -> tuple[ReviseLoop, list[RevisionRequest], ScriptedVision]:
    requests: list[RevisionRequest] = []
    vision = ScriptedVision(scores, default=0.3)

    def generate(req: RevisionRequest) -> JsonValue:
        requests.append(req)
        return {"exposure": f"a{req.attempt}c{req.candidate}", "camera": "35mm", "look": "clean"}

    def render(recipe: JsonValue) -> JsonValue:
        assert isinstance(recipe, dict)
        name = str(recipe["exposure"])
        return {"frames": [str(_png(tmp_path / f"{name}.png", _flat(0.5)))]}

    loop = ReviseLoop(
        generate, render, _critic(vision), SHOT, revision_budget=budget, best_of_n=n, window=window
    )
    return loop, requests, vision


def test_failing_shot_is_revised_up_to_the_budget_then_escalated(tmp_path: Path) -> None:
    profile = load_profile("standard")
    budget = profile.revision_budget["shots"]
    requests: list[RevisionRequest] = []
    vision = ScriptedVision(default=0.2)

    def generate(req: RevisionRequest) -> JsonValue:
        requests.append(req)
        return {"exposure": f"a{req.attempt}c{req.candidate}"}

    def render(recipe: JsonValue) -> JsonValue:
        assert isinstance(recipe, dict)
        return {"frames": [str(_png(tmp_path / f"{recipe['exposure']}.png", _flat(0.5)))]}

    loop = ReviseLoop.from_profile(generate, render, _critic(vision, profile), SHOT, profile)
    out = loop.run()
    assert out.status == "escalated"
    assert out.attempts == 1 + budget
    n = profile.split_budget("shots").candidates
    assert len(requests) == (1 + budget) * n
    assert len({stem for stem, _ in vision.calls}) == (1 + budget) * n  # every candidate re-scored
    assert not out.verdict.passed and "below the pass mark" in out.notes
    # notes go back with the revision request
    revisions = [r for r in requests if r.attempt > 1]
    assert revisions and all("key light too flat" in r.notes for r in revisions)


def test_shot_that_passes_on_revision_is_accepted(tmp_path: Path) -> None:
    loop, requests, _ = _shot_loop(tmp_path, {"a2c0": 0.95}, budget=3)
    out = loop.run()
    assert out.status == "accepted" and out.attempts == 2
    assert out.verdict.passed and len(requests) == 2


def test_graph_sends_critic_notes_back_then_rejects_after_the_budget(tmp_path: Path) -> None:
    profile = load_profile("standard")
    graph = ProductionGraph("p", profile=profile)
    graph.add_node(SHOT)
    frame = _png(tmp_path / "dim.png", _flat(0.5))
    seen: list[str | None] = []

    def runner(ctx: RunContext) -> RunResult:
        seen.append(ctx.feedback.comment if ctx.feedback else None)
        return RunResult(output={"frames": [str(frame)], "attempt": ctx.attempt})

    graph.run(runner, critic=_critic(ScriptedVision(default=0.2), profile))
    assert graph.status("shot-01") is NodeStatus.REJECTED
    assert len(seen) == 1 + profile.revision_budget["shots"]
    assert seen[0] is None and seen[1] is not None and "key light too flat" in seen[1]


# ---- R3: frame-sequence review catches flicker and a static UI ----------------


def _sequence(tmp_path: Path, name: str, frames: Sequence[Gray]) -> list[str]:
    return [str(_png(tmp_path / name / f"{i:03d}.png", f)) for i, f in enumerate(frames)]


def test_sequence_review_catches_flicker(tmp_path: Path) -> None:
    seq = _sequence(tmp_path, "flicker", [_bar(2 * i) if i % 2 else _flat(0.8) for i in range(8)])
    review = review_sequence([Path(p) for p in seq])
    assert "flicker" in review.faults
    verdict = _critic(ScriptedVision())(SHOT, {"sequence": seq})
    assert not verdict.passed and "flickers" in verdict.comment
    assert verdict.scores["flicker"] > 0.02


def test_sequence_review_catches_a_static_ui(tmp_path: Path) -> None:
    seq = _sequence(tmp_path, "static", [_bar(8)] * 6)
    review = review_sequence([Path(p) for p in seq])
    assert review.faults == ("static_ui",)
    verdict = _critic(ScriptedVision())(SHOT, {"frames": seq[:1], "sequence": seq})
    assert not verdict.passed and "static" in verdict.comment


def test_smooth_motion_passes_sequence_review(tmp_path: Path) -> None:
    seq = _sequence(tmp_path, "smooth", [_bar(2 * i) for i in range(8)])
    assert review_sequence([Path(p) for p in seq]).ok
    verdict = _critic(ScriptedVision())(SHOT, {"sequence": seq})
    assert verdict.passed, verdict.comment


def test_unreadable_sequence_frame_is_a_fault(tmp_path: Path) -> None:
    review = review_sequence([tmp_path / "missing.png", tmp_path / "also.png"])
    assert review.faults == ("unreadable",)


# ---- R4: pairwise comparison chooses between revisions ------------------------


class PrefersGood:
    name = "prefers-good"

    def __init__(self) -> None:
        self.calls = 0

    def compare(self, a: Path, b: Path, rubric: str) -> PairwiseVerdict:
        self.calls += 1
        return PairwiseVerdict(winner="b" if "good" in b.stem else "a", notes="b is crisper")


def test_pairwise_judge_picks_between_candidates(tmp_path: Path) -> None:
    pair = PrefersGood()
    assert isinstance(pair, PairwiseJudge)
    critic = _critic(ScriptedVision({"good": 0.7, "plain": 0.8}), pairwise=pair)
    plain = critic.candidate(SHOT, {"frames": [str(_png(tmp_path / "plain.png", _flat(0.4)))]})
    good = critic.candidate(SHOT, {"frames": [str(_png(tmp_path / "good.png", _flat(0.6)))]})
    assert critic.compare(SHOT, plain, good).winner == "b"  # judge overrides the raw scores
    assert critic.best(SHOT, [plain, good]) is good
    assert pair.calls == 2


def test_pairwise_falls_back_to_scores_and_ties_keep_the_incumbent() -> None:
    critic = _critic(ScriptedVision())
    lo = Candidate({"x": 1}, CriticVerdict(passed=True, scores={OVERALL: 0.7}))
    hi = Candidate({"x": 2}, CriticVerdict(passed=True, scores={OVERALL: 0.9}))
    same = Candidate({"x": 3}, CriticVerdict(passed=True, scores={OVERALL: 0.7}))
    bad = Candidate({"x": 4}, CriticVerdict(passed=False, scores={OVERALL: 0.99}))
    assert critic.compare(SHOT, lo, hi).winner == "b"
    assert critic.compare(SHOT, lo, same).winner == "a"
    assert critic.compare(SHOT, lo, bad).winner == "a"  # a pass beats a higher-scoring fail


def test_losing_revision_is_undone_and_best_recipe_stays_the_state(tmp_path: Path) -> None:
    loop, requests, _ = _shot_loop(tmp_path, {"a1c0": 0.5, "a2c0": 0.4, "a3c0": 0.45}, budget=2)
    out = loop.run()
    assert out.status == "escalated"
    assert [it.kept for it in out.history] == [True, False, False]
    assert requests[2].state == requests[1].state == out.recipe
    assert isinstance(out.recipe, dict) and out.recipe["exposure"] == "a1c0"


# ---- R5: best-of-N and the generate/verify split come from the profile --------


def test_best_of_n_and_budget_split_are_set_by_the_quality_profile() -> None:
    draft, standard, cinema = (load_profile(n) for n in ("draft", "standard", "cinema"))
    assert draft.split_budget("render").candidates == 1
    assert standard.split_budget("shots").candidates == 2
    assert cinema.split_budget("shots").candidates > standard.split_budget("shots").candidates
    assert (
        cinema.split_budget("shots").verify_samples > standard.split_budget("shots").verify_samples
    )
    assert standard.critique.pass_score < cinema.critique.pass_score


def test_budget_rule_default_and_profiles_without_critique_still_load() -> None:
    device = load_profile("device-shot")
    assert device.critique.best_of_n == {}
    stage = device.stages[0]
    few = device.revision_budget[stage] <= 1
    split = device.split_budget(stage)
    assert (split.candidates, split.verify_samples) == ((2, 1) if few else (1, 2))
    data = load_profile("standard").model_dump(mode="json")
    data["revision_budget"]["script"] = 4
    data["critique"]["best_of_n"].pop("script", None)
    many = QualityProfile.model_validate(data).split_budget("script")
    assert (many.candidates, many.verify_samples) == (1, 2)


def test_critique_naming_an_unknown_stage_is_rejected() -> None:
    data = load_profile("standard").model_dump(mode="json")
    data["critique"]["best_of_n"]["nope"] = 2
    with pytest.raises(ValueError, match="critique.best_of_n"):
        QualityProfile.model_validate(data)


def test_dropping_a_stage_also_drops_its_critique_settings() -> None:
    producer = Producer()
    profile = load_profile("standard")
    graph = producer.new_graph(
        "spellwise", producer.breakdown(_brief(), profile, duration_s=30), profile
    )
    producer.drop_step(graph, "storyboard")
    assert graph.profile is not None and "storyboard" not in graph.profile.critique.best_of_n


def test_verify_samples_from_profile_set_how_often_each_frame_is_scored(tmp_path: Path) -> None:
    cinema = load_profile("cinema")
    vision = ScriptedVision()
    frame = _png(tmp_path / "f.png", _flat(0.5))
    _critic(vision, cinema)(SHOT, {"frames": [str(frame)]})
    assert len(vision.calls) == cinema.split_budget("shots").verify_samples


def test_best_of_n_runner_builds_n_candidates_and_keeps_the_winner(tmp_path: Path) -> None:
    profile = load_profile("standard")
    n = profile.split_budget("shots").candidates
    assert n == 2
    vision = ScriptedVision({"c0": 0.65, "c1": 0.95})
    calls: list[int] = []

    def inner(ctx: RunContext) -> RunResult:
        i = len(calls)
        calls.append(i)
        frame = _png(tmp_path / f"c{i}.png", _flat(0.5))
        return RunResult(
            output={"frames": [str(frame)]}, usage=Usage(ai_usage={"claude_requests": 1})
        )

    critic = _critic(vision, profile)
    graph = ProductionGraph("p", profile=profile)
    graph.add_node(SHOT)
    graph.run(best_of_n_runner(inner, critic, profile), critic=critic)
    assert len(calls) == n
    assert graph.output("shot-01") == {"frames": [str(tmp_path / "c1.png")]}
    assert graph.ledger.totals().ai_usage["claude_requests"] == n
    assert len(vision.calls) == n  # the gate reused the winner's cached review


# ---- R6: sliding window of recent iterations, recipe as full state ------------


def test_loop_keeps_a_sliding_window_with_the_full_recipe(tmp_path: Path) -> None:
    loop, requests, _ = _shot_loop(tmp_path, {}, budget=4, window=2)
    out = loop.run()
    assert out.attempts == 5 and len(out.history) == 5
    assert [len(r.window) for r in requests] == [0, 1, 2, 2, 2]
    assert [it.attempt for it in out.window] == [4, 5]
    for it in out.history:
        assert isinstance(it.recipe, dict) and set(it.recipe) == {"exposure", "camera", "look"}
        assert it.frames
    assert out.history[1].diff == {"exposure": {"from": "a1c0", "to": "a2c0"}}
    assert recipe_diff(None, {"a": 1}) == {}


# ---- R7: rubric, taste anchors, brief and canon in every judgement ------------


class Look(StrictModel):
    frames: list[str] = Field(min_length=1)


def test_critic_judges_against_rubric_anchors_brief_and_canon(tmp_path: Path) -> None:
    pack = tmp_path / "lighter"
    (pack / "anchors").mkdir(parents=True)
    (pack / "prompt.md").write_text("Light the shot.\n")
    (pack / "rubric.md").write_text("Screens are exposed for the UI.\n")
    (pack / "anchors" / "a.md").write_text("Anchor: soft key, crisp screen.\n")

    class Lighter(Department):
        name = "lighter"
        produces = frozenset({"shots"})
        inputs: ClassVar[dict[str, type[BaseModel]]] = {}
        output = Look
        skill_pack = pack

    canon = Canon(id="spellwise-canon", scope="production", music_themes=["bright-theme"])
    runner = DepartmentRunner([Lighter], MockTextProvider(), canon=canon, brief=_brief())
    vision = ScriptedVision()
    critic = critic_for_runner(runner, vision, profile=load_profile("standard"))
    frame = _png(tmp_path / "f.png", _flat(0.5))
    sheet = _png(tmp_path / "sheet.png", _flat(0.5))
    verdict = critic(SHOT, {"frames": [str(frame)], "contact_sheet": str(sheet)})
    assert {"frame-1", "contact-sheet", OVERALL} <= set(verdict.scores)
    prompt = vision.calls[0][1]
    for text in (
        "Screens are exposed for the UI.",
        "soft key, crisp screen",
        "Spellwise",
        "bright-theme",
    ):
        assert text in prompt
    assert critique_prompt("r").startswith("Score this artifact")
    assert extract_artifact("not a dict").visual is False


def test_text_outputs_use_the_runner_judge_with_the_rubric_prompt(tmp_path: Path) -> None:
    pack = tmp_path / "writer2"
    pack.mkdir()
    (pack / "prompt.md").write_text("Write.\n")
    (pack / "rubric.md").write_text("Every claim traces to a source.\n")

    class Writer2(Department):
        name = "writer2"
        produces = frozenset({"script"})
        inputs: ClassVar[dict[str, type[BaseModel]]] = {}
        output = Look
        skill_pack = pack

    seen: list[str] = []

    def judge(dept: type[Department], rubric: str, output: JsonValue) -> CriticVerdict:
        seen.append(rubric)
        return CriticVerdict(passed=False)

    runner = DepartmentRunner([Writer2], MockTextProvider())
    critic = critic_for_runner(runner, MockVisionJudge(), judge=judge)
    verdict = critic(NodeSpec(id="script", kind="script", department="writer2"), {"text": "hi"})
    assert "Every claim traces to a source." in seen[0]
    assert not verdict.passed and verdict.comment and verdict.scores[OVERALL] == 0.0


def test_encode_png_fixture_is_valid() -> None:
    assert encode_png(1, 1, 1, b"\x00").startswith(b"\x89PNG")
