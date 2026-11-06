# SPDX-License-Identifier: AGPL-3.0-or-later
"""The run supervisor: failure classes, backoff, breaker, escalation, decisions on the node."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import BaseModel, JsonValue, ValidationError

from spectracut.providers.base import ProviderError, ProviderExhausted
from spectracut.studio import (
    CriticVerdict,
    GateMode,
    NodeSpec,
    NodeStatus,
    ProductionGraph,
    QualityProfile,
    RunContext,
    RunResult,
    Tier,
)
from spectracut.studio.producer import Producer
from spectracut.studio.supervisor import FailureClass, Supervisor, WorkerOffline, classify


def _node(nid: str, *inputs: str, kind: str = "brief") -> NodeSpec:
    return NodeSpec(id=nid, kind=kind, department="writer", inputs=inputs)


def _profile(**gates: GateMode) -> QualityProfile:
    return QualityProfile(
        name="t",
        description="test",
        stages=("brief", "script"),
        revision_budget={"brief": 0, "script": 1},
        tier={"brief": Tier.PREVIEW, "script": Tier.PREVIEW},
        gates=dict(gates),
    )


def _validation_error() -> ValidationError:
    class M(BaseModel):
        x: int

    try:
        M.model_validate({})
    except ValidationError as exc:
        return exc
    raise AssertionError


def test_classify_transient_and_permanent() -> None:
    reset = datetime(2030, 1, 1, tzinfo=UTC)
    for exc in (
        ProviderExhausted("claude", reset),
        ConnectionError("x"),
        TimeoutError(),
        WorkerOffline("legion"),
        ProviderError("claude", "provider busy"),
    ):
        assert classify(exc) is FailureClass.TRANSIENT
    for exc in (
        _validation_error(),
        ValueError("bad recipe"),
        ProviderError("claude", "policy"),
        KeyError(),
    ):
        assert classify(exc) is FailureClass.PERMANENT


def test_transient_failures_retry_with_backoff_and_never_trip_the_breaker() -> None:
    sleeps: list[float] = []
    sup = Supervisor(threshold=1, max_retries=3, backoff_s=2, sleep=sleeps.append)
    calls = {"n": 0}

    def runner(ctx: RunContext) -> RunResult:
        calls["n"] += 1
        if calls["n"] <= 3:
            raise ConnectionError("net")
        return RunResult(output=1)

    g = ProductionGraph("p")
    g.add_node(_node("brief"))
    report = Producer().run(g, runner, supervisor=sup)
    assert report.run.executed == ["brief"] and g.status("brief") is NodeStatus.CLEAN
    assert sleeps == [2, 4, 8]
    assert sup.hold_record() is None and report.supervisor_hold is None
    assert len(g.state("brief").decisions) == 3

    always = Supervisor(threshold=1, max_retries=1, sleep=lambda _s: None)

    def down(ctx: RunContext) -> RunResult:
        raise ConnectionError("net")

    g2 = ProductionGraph("p2")
    g2.add_node(_node("brief"))
    Producer().run(g2, down, supervisor=always)
    assert g2.status("brief") is NodeStatus.FAILED and always.hold_record() is None


def test_permanent_burst_stops_admissions_and_records_resumable_hold() -> None:
    clock = {"t": 0.0}
    sup = Supervisor(threshold=2, window_s=60, clock=lambda: clock["t"])

    def runner(ctx: RunContext) -> RunResult:
        if ctx.spec.id in ("a", "b"):
            raise ValueError("bad recipe")
        return RunResult(output=ctx.spec.id)

    g = ProductionGraph("p")
    g.add_nodes([_node("a"), _node("b"), _node("c")])
    report = Producer().run(g, runner, supervisor=sup)
    assert report.run.failed == ["a", "b"]
    assert report.run.held == ["c"] and g.status("c") is NodeStatus.PENDING
    hold = report.supervisor_hold
    assert hold is not None and hold.failures == 2 and hold.nodes == ["a", "b"]
    assert any("held" in d for d in g.state("c").decisions)

    # resumable: after resume, the held node builds
    sup.resume()
    again = Producer().run(g, runner, supervisor=sup)
    assert again.run.executed == ["c"] and g.status("c") is NodeStatus.CLEAN


def test_permanent_failures_outside_the_window_do_not_trip() -> None:
    clock = {"t": 0.0}
    sup = Supervisor(threshold=2, window_s=10, clock=lambda: clock["t"])
    sup._permanent("a")
    clock["t"] = 100.0
    sup._permanent("b")
    assert sup.hold_record() is None


def test_exhausted_revisions_retry_once_at_next_tier_then_reject() -> None:
    sup = Supervisor()
    seen: list[tuple[Tier, int]] = []

    def runner(ctx: RunContext) -> RunResult:
        seen.append((ctx.tier, ctx.attempt))
        return RunResult(output="x")

    def critic(spec: NodeSpec, _out: JsonValue) -> CriticVerdict:
        return CriticVerdict(passed=False, comment="weak")

    g = ProductionGraph("p", profile=_profile(script=GateMode.CRITIC))
    g.add_nodes([_node("brief"), _node("script", "brief", kind="script")])
    report = Producer().run(g, runner, critic=critic, supervisor=sup)
    assert [t for t, _ in seen if t is not Tier.PREVIEW] == [Tier.REVIEW] * 2
    assert seen.count((Tier.PREVIEW, 1)) == 2  # brief + first script attempt
    assert report.run.rejected == ["script"]
    assert g.state("script").tier is Tier.REVIEW
    assert g.state("script").decisions == ("escalated preview -> review after revision budget",)


def test_escalation_that_passes_is_accepted() -> None:
    sup = Supervisor()

    def runner(ctx: RunContext) -> RunResult:
        return RunResult(output=ctx.tier.value)

    def critic(spec: NodeSpec, out: JsonValue) -> CriticVerdict:
        return CriticVerdict(passed=out == "review")

    g = ProductionGraph("p", profile=_profile(script=GateMode.CRITIC))
    g.add_nodes([_node("brief"), _node("script", "brief", kind="script")])
    Producer().run(g, runner, critic=critic, supervisor=sup)
    assert g.status("script") is NodeStatus.CLEAN and g.state("script").tier is Tier.REVIEW


def test_hero_tier_does_not_escalate_and_supervisor_rejects_bad_threshold() -> None:
    from spectracut.studio.node import NodeSpec as N

    assert Supervisor().escalate(N(id="x", kind="k", department="d"), Tier.HERO) is None
    with pytest.raises(ValueError, match="threshold"):
        Supervisor(threshold=0)
