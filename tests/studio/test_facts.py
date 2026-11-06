# SPDX-License-Identifier: AGPL-3.0-or-later
"""Film facts: bounded records, anchored preserve rows and recurring-finding promotion."""

from __future__ import annotations

from pathlib import Path

from pydantic import JsonValue

from spectracut.studio import (
    CriticVerdict,
    GateMode,
    GatePolicy,
    NodeSpec,
    ProductionGraph,
    RunContext,
    RunResult,
)
from spectracut.studio.facts import (
    MAX_ENTRIES,
    MAX_TEXT,
    Decision,
    FactsDraft,
    FactsStore,
    RejectedApproach,
    propose_failures,
)


def _graph(
    pid: str, root: Path | None = None, critic_nodes: tuple[str, ...] = ()
) -> ProductionGraph:
    gates = GatePolicy(nodes={n: GateMode.CRITIC for n in critic_nodes})
    return ProductionGraph(pid, gates=gates, facts=FactsStore(pid, root))


def _look_runner(ctx: RunContext) -> RunResult:
    if ctx.spec.id == "look":
        draft = FactsDraft(
            decisions=(Decision(text="warm key light", anchors=("hero-look",), rejected=("fog",)),),
            rejected=(RejectedApproach(approach="volumetric fog", why="veils the UI"),),
        )
        return RunResult(output={"style": "warm"}, facts=draft)
    return RunResult(output={"style": "hazy fog"} if ctx.spec.id == "bad" else {"ok": True})


def _node(node_id: str, **params: JsonValue) -> NodeSpec:
    return NodeSpec(id=node_id, kind="shot", department="lighter", params=params)


def test_every_built_node_writes_a_bounded_record() -> None:
    g = _graph("p")
    assert g.facts is not None
    many = tuple(Decision(text="x" * 1000) for _ in range(MAX_ENTRIES + 10))

    def runner(_ctx: RunContext) -> RunResult:
        return RunResult(output=1, facts=FactsDraft(decisions=many))

    g.add_nodes([_node("a", n=1), _node("b", n=2)])
    g.run(runner)
    for nid in ("a", "b"):
        rec = g.facts.get(nid)
        assert rec is not None
        assert len(rec.decisions) == MAX_ENTRIES
        assert all(len(d.text) <= MAX_TEXT for d in rec.decisions)


def test_anchored_decision_reaches_touching_node_and_gate_checks_it() -> None:
    g = _graph("p", critic_nodes=("bad", "good"))
    g.add_nodes(
        [
            NodeSpec(id="look", kind="look", department="lighter"),
            NodeSpec(
                id="bad",
                kind="shot",
                department="lighter",
                inputs=("look",),
                params={"look": "hero-look"},
            ),
            NodeSpec(id="good", kind="shot", department="lighter", inputs=("look",)),
            _node("other"),
        ]
    )
    seen: dict[str, RunContext] = {}

    def runner(ctx: RunContext) -> RunResult:
        seen[ctx.spec.id] = ctx
        return _look_runner(ctx)

    def critic(_spec: NodeSpec, _out: JsonValue) -> CriticVerdict:
        return CriticVerdict(passed=True)

    g.run(runner, critic=critic)
    assert [r.decision for r in seen["bad"].preserve] == ["warm key light"]
    assert seen["bad"].preserve[0].anchor == "hero-look"
    assert seen["other"].preserve == ()
    # "good" touches no anchored id: no rows; "bad" reuses the rejected phrase and fails the gate
    assert seen["good"].preserve == ()
    decision = g.decision("bad")
    assert decision is not None and decision.critic is not None
    assert not decision.critic.passed and "preserve-violation" in decision.critic.findings


def test_recurring_finding_proposed_at_three_productions_not_two(tmp_path: Path) -> None:
    def build(pid: str) -> None:
        g = _graph(pid, tmp_path / pid / "facts", critic_nodes=("shot",))
        g.add_nodes([_node("shot")])
        g.run(
            lambda _c: RunResult(output=pid),
            critic=lambda _s, _o: CriticVerdict(
                passed=True, comment=f"flat light in {pid}", findings=("flat-light",)
            ),
        )

    failures = tmp_path / "failures"
    build("one")
    build("two")
    assert propose_failures(tmp_path, failures) == []
    build("three")
    (written,) = propose_failures(tmp_path, failures)
    text = written.read_text()
    assert written.name == "flat-light.md" and "flat light in one" in text and "three/shot" in text
    assert propose_failures(tmp_path, failures) == []  # never overwrites
