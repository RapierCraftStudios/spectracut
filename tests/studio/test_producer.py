# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Producer: breakdown, add/drop steps, budget caps and scheduling (#53)."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import BaseModel

from spectracut.providers.base import ProviderExhausted
from spectracut.queue.executors.base import JobStatus
from spectracut.spec.brief import AudienceProfile, Brief, ClaimSource, Persona, ProofPoint
from spectracut.spec.spec import Shot, Spec
from spectracut.studio import (
    BudgetCaps,
    CapacityLimits,
    Department,
    NodeSpec,
    NodeStatus,
    Producer,
    ProductionGraph,
    RunContext,
    RunResult,
    Usage,
    load_profile,
)
from spectracut.studio.estimate import BRIEF_PARAM, PLAN_PARAM
from spectracut.studio.producer import ProducerReport, TaskState, scheduled
from spectracut.studio.store import ProductionMeta, ProductionStore


def _brief() -> Brief:
    return Brief(
        id="spellwise",
        product="Spellwise",
        audience=AudienceProfile(personas=[Persona(name="Dev", description="ships often")]),
        sources=[ClaimSource(id="docs", title="Docs", ref="https://example.test/docs")],
        proof_points=[ProofPoint(claim="Fast spellcheck", source_id="docs")],
        cta="Try it",
    )


class Recorder:
    """Fake runner: records calls; voiceover reports ElevenLabs characters."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, ctx: RunContext) -> RunResult:
        self.calls.append(ctx.spec.id)
        chars = 900 if ctx.spec.kind == "voiceover" else 0
        return RunResult(
            output={"node": ctx.spec.id},
            usage=Usage(ai_usage={"claude_requests": 1}, elevenlabs_characters=chars),
        )


def _standard_graph(producer: Producer) -> ProductionGraph:
    profile = load_profile("standard")
    plan = producer.breakdown(_brief(), profile, duration_s=60)
    return producer.new_graph("spellwise", plan, profile)


def _drive(
    producer: Producer, graph: ProductionGraph, runner: Recorder, **kw: object
) -> list[ProducerReport]:
    """Run, approving every human gate, until a run neither builds nor waits on people."""
    reports = []
    for _ in range(40):
        report = producer.run(graph, runner, **kw)  # type: ignore[arg-type]
        reports.append(report)
        for nid in report.run.awaiting_approval:
            graph.approve(nid, "ok")
        if not report.run.executed and not report.run.awaiting_approval:
            break
    return reports


# ---- R3: breakdown and estimate ---------------------------------------------


def test_60s_brief_standard_profile_yields_every_stage_and_an_estimate() -> None:
    producer = Producer()
    profile = load_profile("standard")
    plan = producer.breakdown(_brief(), profile, duration_s=60)
    assert len(plan.shots) == 15
    assert plan.shots[0].id == "shot-01" and plan.shots[-1].id == "shot-15"
    assert {a.id for a in plan.assets} >= {"product-ui", "brand-kit", "voiceover", "music"}
    assert [n.id for n in plan.nodes] == list(profile.stages)
    assert plan.gates["brief"].value == "human" and plan.gates["script"].value == "critic"

    graph = producer.new_graph("spellwise", plan, profile)
    assert list(graph.order()) == list(profile.stages)
    first = graph.spec("brief")
    assert BRIEF_PARAM in first.params and PLAN_PARAM in first.params

    est = producer.estimate(graph)
    assert [s.node_id for s in est.stages] == list(profile.stages)
    assert est.duration_s == 60 and est.shots == 15
    assert est.gpu_hours > 0
    assert est.total.elevenlabs_characters > 0
    assert est.total.ai_requests > 0
    assert est.wall_minutes > 0
    assert est.human_gates == 5
    text = "\n".join(est.lines())
    assert "  voiceover: department=voiceover " in text
    assert "human gates=5" in text and "gpu_hours=" in text and "wall_minutes=" in text


def test_script_driven_breakdown_uses_script_shots_length_and_vo() -> None:
    script = Spec(
        id="spellwise-ad",
        format="ad",
        length_s=20,
        aspects=["16:9"],
        shots=[
            Shot(id="open", template_id="hero", duration_beats=1),
            Shot(id="close", template_id="cta", duration_beats=3),
        ],
        vo="Spellwise checks as you type.",
    )
    plan = Producer().breakdown(_brief(), load_profile("standard"), script=script)
    assert plan.duration_s == 20
    assert [(s.id, s.duration_s, s.template_id) for s in plan.shots] == [
        ("open", 5.0, "hero"),
        ("close", 15.0, "cta"),
    ]
    assert plan.vo_characters == len(script.vo)
    graph = Producer().new_graph("ad", plan, load_profile("standard"))
    voiceover = Producer().estimate(graph).stage("voiceover")
    assert voiceover.cost.elevenlabs_characters == len(script.vo) * voiceover.max_runs


def test_breakdown_needs_a_duration() -> None:
    with pytest.raises(ValueError, match="duration"):
        Producer().breakdown(_brief(), load_profile("standard"))


# ---- R4: adding and dropping a step -------------------------------------------


def test_add_then_drop_step_updates_graph_and_estimate() -> None:
    producer = Producer()
    graph = _standard_graph(producer)
    _drive(producer, graph, Recorder())
    assert all(s is NodeStatus.CLEAN for s in graph.statuses().values())
    before = producer.estimate(graph)

    added = producer.add_step(graph, "grade", after="shots", tier=None)
    order = graph.order()
    assert order.index("shots") < order.index("grade") < order.index("voiceover")
    assert graph.spec("grade").inputs == ("shots",)
    assert graph.spec("voiceover").inputs == ("grade",)
    assert graph.status("grade") is NodeStatus.PENDING
    assert graph.status("shots") is NodeStatus.CLEAN
    downstream = order[order.index("voiceover") :]
    assert all(graph.status(n) is NodeStatus.DIRTY for n in downstream)
    assert all(graph.status(n) is NodeStatus.CLEAN for n in order[: order.index("grade")])
    assert len(added.stages) == len(before.stages) + 1
    assert added.stage("grade").cost.gpu_minutes > 0
    assert added.total.gpu_minutes > before.total.gpu_minutes
    assert graph.profile is not None and "grade" in graph.profile.stages

    _drive(producer, graph, Recorder())
    dropped = producer.drop_step(graph, "grade")
    assert "grade" not in graph.order()
    assert graph.spec("voiceover").inputs == ("shots",)
    assert graph.status("voiceover") is NodeStatus.DIRTY
    assert graph.status("shots") is NodeStatus.CLEAN
    assert len(dropped.stages) == len(before.stages)
    assert dropped.total == before.total
    assert graph.profile is not None and "grade" not in graph.profile.stages


def test_dropping_the_first_step_carries_the_brief_and_plan() -> None:
    producer = Producer()
    graph = _standard_graph(producer)
    est = producer.drop_step(graph, "brief")
    concept = graph.spec("concept")
    assert concept.inputs == ()
    assert BRIEF_PARAM in concept.params and PLAN_PARAM in concept.params
    assert est.duration_s == 60 and est.human_gates == 4


# ---- R5: budget caps ----------------------------------------------------------


def test_elevenlabs_cap_holds_the_node_and_asks_for_approval() -> None:
    producer = Producer()
    graph = _standard_graph(producer)
    cap = 1000
    assert producer.estimate(graph).stage("voiceover").cost.elevenlabs_characters > cap
    runner = Recorder()
    caps = BudgetCaps(elevenlabs_characters=cap)

    reports = _drive(producer, graph, runner, caps=caps)
    last = reports[-1]
    assert [h.node_id for h in last.needs_approval] == ["voiceover"]
    hold = last.needs_approval[0]
    assert hold.metric == "elevenlabs_characters" and hold.limit == cap
    assert hold.projected > cap
    assert last.run.held == ["voiceover"]
    assert "needs budget approval" in last.run.held_reasons["voiceover"]
    assert "voiceover" not in runner.calls
    assert graph.status("voiceover") is NodeStatus.PENDING
    assert "edit" not in runner.calls and "edit" in last.run.blocked
    assert "shots" in runner.calls and graph.status("shots") is NodeStatus.CLEAN
    assert graph.ledger.totals().elevenlabs_characters <= cap

    _drive(producer, graph, runner, caps=caps, approved={"voiceover"})
    assert "voiceover" in runner.calls and "edit" in runner.calls
    assert graph.status("voiceover") is NodeStatus.CLEAN


def test_budget_caps_and_approvals_persist_in_production_meta(tmp_path: Path) -> None:
    producer = Producer()
    graph = _standard_graph(producer)
    store = ProductionStore(tmp_path)
    store.create("spellwise", _brief(), graph, caps=BudgetCaps(gpu_minutes=5))
    meta = store.meta("spellwise")
    assert meta.caps == BudgetCaps(gpu_minutes=5) and meta.budget_approvals == ()
    store.update_meta("spellwise", meta.model_copy(update={"budget_approvals": ("render",)}))
    assert store.meta("spellwise").budget_approvals == ("render",)
    old = ProductionMeta.model_validate_json(
        '{"id":"x","brief_kind":"brief","brief_hash":"sha256:' + "0" * 64 + '",'
        '"created_at":"2026-01-01T00:00:00Z"}'
    )
    assert old.caps is None and old.budget_approvals == ()


# ---- R6: scheduling -------------------------------------------------------------


def test_provider_exhausted_waits_for_ai_then_retries() -> None:
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    slept: list[float] = []
    attempts: list[int] = []

    def flaky(ctx: RunContext) -> RunResult:
        attempts.append(1)
        if len(attempts) == 1:
            raise ProviderExhausted("claude", t0 + timedelta(seconds=30))
        return RunResult(output={"ok": True})

    sched = scheduled(flaky, sleep=slept.append, now=lambda: t0)
    graph = ProductionGraph("p")
    graph.add_node(NodeSpec(id="script", kind="script", department="writer"))
    report = graph.run(sched)
    assert report.executed == ["script"] and not report.failed
    assert graph.status("script") is NodeStatus.CLEAN
    assert slept == [30.0]
    assert sched.history["script"] == [
        TaskState.QUEUED,
        TaskState.RUNNING,
        TaskState.WAITING_FOR_AI,
        TaskState.RUNNING,
        TaskState.DONE,
    ]
    assert TaskState.WAITING_FOR_AI.value == JobStatus.WAITING_FOR_AI.value


class _Out(BaseModel):
    ok: bool = True


class GpuDept(Department):
    name = "gpu"
    produces = frozenset({"frames"})
    output = _Out
    uses_gpu = True


def test_gpu_tasks_wait_for_a_lease_instead_of_overlapping() -> None:
    lock = threading.Lock()
    active = 0
    peak = 0

    def render(ctx: RunContext) -> RunResult:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return RunResult(output={"node": ctx.spec.id})

    sched = scheduled(render, departments={"gpu": GpuDept}, limits=CapacityLimits(gpu_leases=1))
    graph = ProductionGraph("p")
    graph.add_nodes(
        [
            NodeSpec(id="a", kind="frames", department="gpu"),
            NodeSpec(id="b", kind="frames", department="gpu"),
        ]
    )
    report = graph.run(sched, max_workers=2)
    assert sorted(report.executed) == ["a", "b"] and not report.failed
    assert peak == 1
    assert all(graph.status(n) is NodeStatus.CLEAN for n in ("a", "b"))
    waited = [n for n in ("a", "b") if TaskState.WAITING_FOR_GPU in sched.history[n]]
    assert len(waited) == 1
    assert all(sched.state(n) is TaskState.DONE for n in ("a", "b"))


# ---- graph hooks ---------------------------------------------------------------


def test_graph_remove_node_and_hold_hook() -> None:
    graph = ProductionGraph("p")
    graph.add_nodes(
        [
            NodeSpec(id="a", kind="a", department="d"),
            NodeSpec(id="b", kind="b", department="d", inputs=("a",)),
        ]
    )
    with pytest.raises(ValueError, match="input of"):
        graph.remove_node("a")

    def ok(ctx: RunContext) -> RunResult:
        return RunResult(output={"n": ctx.spec.id})

    for workers in (1, 2):
        report = graph.run(ok, max_workers=workers, hold=lambda s: "wait" if s.id == "a" else None)
        assert report.held == ["a"] and report.held_reasons == {"a": "wait"}
        assert report.blocked == ["b"] and not report.executed
        assert graph.status("a") is NodeStatus.PENDING

    assert graph.run(ok).executed == ["a", "b"]
    graph.remove_node("b")
    assert list(graph.order()) == ["a"]
    with pytest.raises(KeyError):
        graph.spec("b")
