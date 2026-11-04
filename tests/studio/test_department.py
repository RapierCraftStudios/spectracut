# SPDX-License-Identifier: AGPL-3.0-or-later
"""Department contract and runner: skill packs, typed I/O, parallelism, context, Critic hook."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, ClassVar, cast

import pytest
from pydantic import BaseModel, Field, JsonValue

from spectracut.canon.schema import Canon
from spectracut.providers.base import Completion, TextProvider
from spectracut.providers.mock_text import MockTextProvider, instance_of
from spectracut.spec._base import StrictModel
from spectracut.spec.brief import AudienceProfile, Brief, ClaimSource, Persona, ProofPoint
from spectracut.studio import (
    CapacityLimits,
    CostEstimate,
    CriticVerdict,
    Department,
    DepartmentOutputError,
    DepartmentRunner,
    GateMode,
    GatePolicy,
    NodeSpec,
    NodeStatus,
    ProductionGraph,
    QualityProfile,
    SkillPack,
    Tier,
)
from spectracut.studio.runner import CONTEXT_METRIC

SKILLS = Path(__file__).resolve().parent / "skills"


# ---- test departments ------------------------------------------------------


class Line(StrictModel):
    text: str = Field(min_length=1)
    words: int = Field(ge=1)


class Frames(StrictModel):
    count: int = Field(ge=1, le=240)
    note: str


class Scribe(Department):
    name = "scribe"
    produces = frozenset({"line", "headline"})
    inputs: ClassVar[dict[str, type[BaseModel]]] = {"line": Line}
    output = Line
    skill_pack = SKILLS / "scribe"
    cost = CostEstimate(ai_requests=1, ai_input_tokens=800)


class Renderer(Department):
    name = "renderer"
    produces = frozenset({"frames"})
    inputs: ClassVar[dict[str, type[BaseModel]]] = {"line": Line, "headline": Line}
    output = Frames
    skill_pack = SKILLS / "renderer"
    cost = CostEstimate(ai_requests=1, gpu_minutes=2.0)
    uses_gpu = True


def brief() -> Brief:
    return Brief(
        id="spellwise",
        product="Spellwise",
        audience=AudienceProfile(personas=[Persona(name="Dev", description="ships often")]),
        sources=[ClaimSource(id="docs", title="Docs", ref="https://example.test/docs")],
        proof_points=[ProofPoint(claim="Fast spellcheck", source_id="docs")],
        cta="Try it",
    )


def canon() -> Canon:
    return Canon(id="spellwise-canon", scope="production", music_themes=["bright"])


class Recording:
    """Wraps a provider; records prompts and the peak number of concurrent calls."""

    name = "recording"

    def __init__(
        self,
        inner: TextProvider | None = None,
        *,
        barrier: threading.Barrier | None = None,
        hold: float = 0.0,
    ) -> None:
        self.inner = inner or MockTextProvider()
        self.barrier = barrier
        self.hold = hold
        self.prompts: list[str] = []
        self.systems: list[str] = []
        self.in_flight = 0
        self.peak = 0
        self._lock = threading.Lock()

    def complete(self, system: str, prompt: str, *, schema: dict[str, Any]) -> Completion:
        with self._lock:
            self.prompts.append(prompt)
            self.systems.append(system)
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
        try:
            if self.barrier is not None:
                self.barrier.wait(timeout=10)
            if self.hold:
                time.sleep(self.hold)
            return self.inner.complete(system, prompt, schema=schema)
        finally:
            with self._lock:
                self.in_flight -= 1


def context_of(prompt: str) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(prompt.split("\n\n", 1)[1])
    return payload


# ---- R1/R4: declaration and skill packs -------------------------------------


def test_department_is_a_typed_class_with_a_skill_pack() -> None:
    pack = Scribe.pack()
    assert isinstance(pack, SkillPack)
    assert pack.prompt.startswith("You are the test scribe")
    assert Scribe.rubric() == "Score clarity of the line from 0 to 1."
    assert [p.name for p in pack.failures] == ["verbatim.md"]
    assert pack.anchors == ()
    assert Scribe.output_schema()["required"] == ["text", "words"]
    assert Scribe.cost.ai_input_tokens == 800
    assert Renderer.uses_gpu and not Scribe.uses_gpu


@pytest.mark.parametrize(
    ("attrs", "match"),
    [
        ({"name": "Bad Name", "produces": frozenset({"x"}), "output": Line}, "slug"),
        ({"name": "ok", "produces": frozenset(), "output": Line}, "produces"),
        ({"name": "ok", "produces": frozenset({"x"}), "output": dict}, "output"),
        (
            {"name": "ok", "produces": frozenset({"x"}), "output": Line, "inputs": {"x": int}},
            "inputs",
        ),
    ],
)
def test_bad_declarations_are_rejected(attrs: dict[str, Any], match: str) -> None:
    with pytest.raises(TypeError, match=match):
        type("Bad", (Department,), attrs)


def test_abstract_bases_are_not_validated() -> None:
    base = type("Shared", (Department,), {"abstract": True})
    attrs = {"name": "child", "produces": frozenset({"x"}), "output": Line}
    child = cast(type[Department], type("Child", (base,), attrs))
    assert child.name == "child"


def test_missing_or_incomplete_skill_pack_is_rejected(tmp_path: Path) -> None:
    attrs = {"name": "ghost", "produces": frozenset({"x"}), "output": Line}
    ghost = type("Ghost", (Department,), {**attrs, "skill_pack": tmp_path / "nope"})
    with pytest.raises(ValueError, match="folder not found"):
        DepartmentRunner([ghost], MockTextProvider())
    (tmp_path / "half").mkdir()
    (tmp_path / "half" / "prompt.md").write_text("prompt\n")
    half = cast(
        type[Department], type("Half", (Department,), {**attrs, "skill_pack": tmp_path / "half"})
    )
    with pytest.raises(ValueError, match="missing rubric.md"):
        half.pack()


def test_runner_rejects_conflicting_departments() -> None:
    dup = type(
        "Dup",
        (Department,),
        {
            "name": "dup",
            "produces": frozenset({"line"}),
            "output": Line,
            "skill_pack": SKILLS / "scribe",
        },
    )
    with pytest.raises(ValueError, match="produced by both"):
        DepartmentRunner([Scribe, dup], MockTextProvider())
    with pytest.raises(ValueError, match="duplicate department"):
        DepartmentRunner([Scribe, Scribe], MockTextProvider())
    with pytest.raises(ValueError, match="gpu_leases is 0"):
        DepartmentRunner([Renderer], MockTextProvider(), limits=CapacityLimits(gpu_leases=0))


def test_runner_rejects_output_that_fails_its_schema() -> None:
    bad = MockTextProvider(respond=lambda s, p, schema: {"text": "", "words": 0})
    g = ProductionGraph("film")
    g.add_nodes([NodeSpec(id="l1", kind="line", department="scribe"), _frames("f1", "l1")])
    runner = DepartmentRunner([Scribe, Renderer], bad, limits=CapacityLimits(gpu_leases=1), graph=g)
    report = g.run(runner)
    assert report.failed == ["l1"] and report.blocked == ["f1"]
    state = g.state("l1")
    assert state.status is NodeStatus.FAILED
    assert state.error is not None
    assert state.error.startswith("DepartmentOutputError:")
    assert "output fails its schema" in state.error and "text" in state.error
    with pytest.raises(ValueError, match="no accepted output"):
        g.output("l1")
    assert [e.outcome for e in g.ledger.entries()] == ["failed"]


def test_inputs_and_kinds_are_checked() -> None:
    g = ProductionGraph("film")
    g.add_nodes(
        [
            NodeSpec(id="l1", kind="line", department="scribe"),
            NodeSpec(id="wrong", kind="frames", department="scribe"),
            NodeSpec(id="who", kind="line", department="nobody"),
        ]
    )
    runner = DepartmentRunner([Scribe], MockTextProvider(), graph=g)
    report = g.run(runner)
    assert report.failed == ["wrong", "who"]
    assert "does not produce node kind 'frames'" in (g.state("wrong").error or "")
    assert "unknown department 'nobody'" in (g.state("who").error or "")
    with pytest.raises(DepartmentOutputError, match="undeclared kind 'frames'"):
        Scribe.validate_inputs("n", {"x": {}}, {"x": "frames"})
    with pytest.raises(DepartmentOutputError, match="input 'x' fails its schema"):
        Scribe.validate_inputs("n", {"x": {"text": "hi"}}, {"x": "line"})


# ---- R5: parallelism bounded by AI capacity and GPU leases ------------------


def _lines(n: int) -> list[NodeSpec]:
    return [
        NodeSpec(id=f"l{i}", kind="line", department="scribe", params={"i": i}) for i in range(n)
    ]


def _frames(nid: str, *inputs: str) -> NodeSpec:
    return NodeSpec(id=nid, kind="frames", department="renderer", inputs=inputs)


def test_independent_tasks_run_in_parallel_up_to_ai_capacity() -> None:
    provider = Recording(barrier=threading.Barrier(2), hold=0.05)
    g = ProductionGraph("film")
    g.add_nodes(_lines(4))
    runner = DepartmentRunner([Scribe], provider, limits=CapacityLimits(ai=2), graph=g)
    report = g.run(runner, max_workers=4)
    assert report.executed == ["l0", "l1", "l2", "l3"]
    assert provider.peak == 2  # the barrier proves two ran at once; the limit caps it at two
    assert all(s is NodeStatus.CLEAN for s in g.statuses().values())


def test_gpu_departments_hold_a_lease() -> None:
    provider = Recording(hold=0.05)
    g = ProductionGraph("film")
    g.add_nodes([*_lines(1), *(_frames(f"f{i}", "l0") for i in range(3))])
    runner = DepartmentRunner(
        [Scribe, Renderer], provider, limits=CapacityLimits(ai=4, gpu_leases=1), graph=g
    )
    report = g.run(runner, max_workers=4)
    assert report.executed == ["l0", "f0", "f1", "f2"]
    assert provider.peak == 1


def test_single_worker_runs_sequentially_and_dependants_follow_inputs() -> None:
    provider = Recording(hold=0.01)
    g = ProductionGraph("film")
    g.add_nodes([*_lines(3), _frames("f0", "l0", "l1")])
    runner = DepartmentRunner(
        [Scribe, Renderer], provider, limits=CapacityLimits(ai=4, gpu_leases=2), graph=g
    )
    g.run(runner)
    assert provider.peak == 1
    assert context_of(provider.prompts[-1])["inputs"].keys() == {"l0", "l1"}
    with pytest.raises(ValueError, match="max_workers"):
        g.run(runner, max_workers=0)


def test_parallel_run_matches_sequential_run() -> None:
    def build(workers: int) -> tuple[ProductionGraph, Any]:
        g = ProductionGraph("film", gates=GatePolicy(nodes={"f1": GateMode.HUMAN}))
        g.add_nodes(
            [*_lines(3), _frames("f0", "l0", "l1"), _frames("f1", "l2"), _frames("f2", "f1")]
        )
        g.add_node(NodeSpec(id="bad", kind="line", department="nobody"))
        g.add_node(_frames("after-bad", "bad"))
        runner = DepartmentRunner(
            [Scribe, Renderer],
            MockTextProvider(),
            limits=CapacityLimits(ai=3, gpu_leases=2),
            graph=g,
        )
        return g, g.run(runner, max_workers=workers)

    (g1, r1), (g4, r4) = build(1), build(4)
    assert r1 == r4
    assert r4.awaiting_approval == ["f1"] and r4.failed == ["bad"]
    assert set(r4.blocked) == {"f2", "after-bad"}
    assert g1.statuses() == g4.statuses()
    assert g1.output("f0") == g4.output("f0")


# ---- R6: context from canon, brief and inputs only, size in the ledger ------


def test_context_is_canon_brief_and_declared_inputs_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret-should-never-appear-123456")
    provider = Recording()
    g = ProductionGraph("film")
    g.add_nodes(
        [
            NodeSpec(id="l0", kind="line", department="scribe", params={"tone": "warm"}),
            NodeSpec(id="other", kind="headline", department="scribe", params={"tone": "loud"}),
            _frames("f0", "l0"),
        ]
    )
    runner = DepartmentRunner(
        [Scribe, Renderer],
        provider,
        canon=canon(),
        brief=brief(),
        limits=CapacityLimits(gpu_leases=1),
        graph=g,
    )
    g.run(runner)
    prompt = provider.prompts[-1]
    ctx = context_of(prompt)
    assert set(ctx) == {"canon", "brief", "inputs", "params", "feedback"}
    assert ctx["canon"]["id"] == "spellwise-canon"
    assert ctx["brief"]["product"] == "Spellwise"
    assert ctx["inputs"] == {"l0": g.output("l0")}
    assert ctx["params"] == {} and ctx["feedback"] is None
    assert json.dumps(g.output("other")) not in prompt
    assert "sk-ant-secret" not in prompt
    assert provider.systems[-1] == Renderer.pack().prompt

    entry = g.ledger.entries_for("f0")[0]
    measured = len(prompt.split("\n\n", 1)[1].encode("utf-8"))
    assert entry.ai_usage[CONTEXT_METRIC] == measured > 0
    assert entry.ai_usage["mock_text_requests"] == 1
    assert g.ledger.totals().ai_usage[CONTEXT_METRIC] == sum(
        len(p.split("\n\n", 1)[1].encode("utf-8")) for p in provider.prompts
    )


# ---- R2/R7: mock department end to end with the Critic hook -----------------


def test_mock_department_runs_end_to_end_with_critic_hook() -> None:
    provider = Recording()
    seen: list[tuple[str, str]] = []
    rounds: dict[str, int] = {}

    def judge(dept: type[Department], rubric: str, output: JsonValue) -> CriticVerdict:
        seen.append((dept.name, rubric))
        rounds[dept.name] = rounds.get(dept.name, 0) + 1
        passed = not (dept.name == "scribe" and rounds["scribe"] == 1)
        return CriticVerdict(passed=passed, scores={"clarity": 0.9 if passed else 0.2})

    profile = QualityProfile(
        name="standard",
        description="test",
        stages=("line", "frames"),
        revision_budget={"line": 1, "frames": 0},
        tier={"line": Tier.PREVIEW, "frames": Tier.FINAL},
        gates={"line": GateMode.CRITIC, "frames": GateMode.CRITIC},
    )
    g = ProductionGraph("film", profile=profile)
    g.add_nodes([*_lines(1), _frames("f0", "l0")])
    runner = DepartmentRunner(
        [Scribe, Renderer],
        provider,
        canon=canon(),
        brief=brief(),
        limits=CapacityLimits(ai=2, gpu_leases=1),
        graph=g,
    )
    report = g.run(runner, critic=runner.critic(judge), max_workers=2)
    assert report.executed == ["l0", "f0"] and not report.failed
    assert all(s is NodeStatus.CLEAN for s in g.statuses().values())
    Line.model_validate(g.output("l0"))
    Frames.model_validate(g.output("f0"))
    assert seen[0] == ("scribe", Scribe.rubric()) and seen[-1] == ("renderer", Renderer.rubric())
    # the revision saw the Critic's feedback in its context
    assert context_of(provider.prompts[1])["feedback"]["passed"] is False
    assert [e.outcome for e in g.ledger.entries()] == ["revise", "accepted", "accepted"]
    assert g.ledger.entries_for("f0")[0].tier is Tier.FINAL

    again = g.run(runner, critic=runner.critic(judge), max_workers=2)
    assert not again.did_work and again.executed == [] and again.reused == []
    assert len(provider.prompts) == 3


# ---- mock text provider -----------------------------------------------------


class Shot(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$", max_length=128)
    lens_mm: float = Field(gt=10, le=200)
    take: int = Field(ge=3)
    mode: str = Field(json_schema_extra={"enum": ["wide", "close"]})
    notes: str | None = None


class Board(BaseModel):
    title: str = Field(min_length=20)
    shots: list[Shot] = Field(min_length=2)
    final: bool
    caption: str | None


def test_mock_text_provider_builds_schema_valid_instances() -> None:
    provider = MockTextProvider()
    schema = Board.model_json_schema()
    a = provider.complete("sys", "prompt one", schema=schema)
    b = provider.complete("sys", "prompt one", schema=schema)
    assert a == b
    board = Board.model_validate(a.output)
    assert len(board.shots) == 2 and len(board.title) >= 20
    assert a.usage == {
        "mock_text_requests": 1,
        "mock_text_input_chars": len("sys") + len("prompt one"),
    }
    assert isinstance(provider, TextProvider)
    assert instance_of({"type": "string", "const": "x"}) == "x"
