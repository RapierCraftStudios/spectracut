# SPDX-License-Identifier: AGPL-3.0-or-later
"""Autonomy policy: delegated gates pass on evidence or escalate; budget holds under a ceiling."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import pytest
from pydantic import JsonValue
from typer.testing import CliRunner

from spectracut.cli import studio as studio_mod
from spectracut.cli.app import app
from spectracut.queue.signing import SIGNING_KEY_ENV
from spectracut.spec.brief import AudienceProfile, Brief, ClaimSource, Persona, ProofPoint
from spectracut.studio import load_profile
from spectracut.studio.autonomy import AutonomyPolicy, BudgetCeiling, load_autonomy
from spectracut.studio.conformance import Conformance, ConformanceReview, Evidence, RowJudgement
from spectracut.studio.contract import FilmContract
from spectracut.studio.gates import Critic, CriticVerdict, Finding, GateMode, GatePolicy, Severity
from spectracut.studio.graph import GraphSnapshot, ProductionGraph, RunContext, RunResult
from spectracut.studio.ledger import Usage
from spectracut.studio.node import NodeSpec, NodeStatus
from spectracut.studio.producer import BudgetCaps, Producer
from spectracut.studio.store import ProductionStore

BRIEF: dict[str, JsonValue] = {
    "id": "spellwise",
    "message": "Spelling that keeps up",
    "proof_points": [{"claim": "Fast spellcheck", "source_id": "docs"}],
    "tone": "calm",
    "cta": "Try it",
}
KINDS = ("brief", "style_frames", "animatic", "edit", "delivery")
GATES = GatePolicy(defaults={k: GateMode.HUMAN for k in KINDS})
DELEGATE = AutonomyPolicy(delegate_kinds=("brief", "style_frames", "edit"))


def _graph(policy: AutonomyPolicy | None = DELEGATE) -> ProductionGraph:
    g = ProductionGraph("launch", gates=GATES, autonomy=policy)
    prev: tuple[str, ...] = ()
    for kind in KINDS:
        params: dict[str, JsonValue] = {"brief": BRIEF} if kind == "brief" else {}
        g.add_node(NodeSpec(id=kind, kind=kind, department="d", inputs=prev, params=params))
        prev = (kind,)
    return g


def _runner(ctx: RunContext) -> RunResult:
    return RunResult(output={"n": ctx.spec.id}, usage=Usage())


def _critic(score: float = 0.9, findings: list[Finding] | None = None) -> Critic:
    def critic(spec: NodeSpec, output: JsonValue) -> CriticVerdict:
        blocked = any(f.severity is Severity.HIGH and f.confirmed for f in findings or [])
        return CriticVerdict(
            passed=not blocked, scores={"overall": score}, findings=list(findings or [])
        )

    return critic


def _conformance(status: Literal["met", "unmet"] = "met") -> Conformance:
    def review(spec: NodeSpec, output: JsonValue, c: FilmContract) -> ConformanceReview:
        ev = [Evidence(timecode_s=1.0, quote="Try it")]
        return ConformanceReview(
            rows=[RowJudgement(row_id=r, status=status, evidence=ev) for r in c.row_ids()]
        )

    return review


# ---- R1: delegate some gates, keep others -------------------------------------


def test_policy_delegates_some_gates_and_keeps_the_rest_for_the_owner() -> None:
    g = _graph()
    modes = {k: g.gate_for(g.spec(k)) for k in KINDS}
    assert modes == {
        "brief": GateMode.AUTO_DELEGATED,
        "style_frames": GateMode.AUTO_DELEGATED,
        "animatic": GateMode.HUMAN,
        "edit": GateMode.AUTO_DELEGATED,
        "delivery": GateMode.HUMAN,
    }
    by_node = AutonomyPolicy(delegate_nodes=("animatic",))
    assert _graph(by_node).gate_for(g.spec("animatic")) is GateMode.AUTO_DELEGATED
    standard = load_profile("standard")
    sg = ProductionGraph("s", profile=standard, autonomy=AutonomyPolicy(delegate_kinds=("brief",)))
    sg.add_node(NodeSpec(id="b", kind="brief", department="d"))
    sg.add_node(NodeSpec(id="a", kind="animatic", department="d"))
    assert sg.gate_for(sg.spec("b")) is GateMode.AUTO_DELEGATED
    assert sg.gate_for(sg.spec("a")) is GateMode.HUMAN


def test_without_a_policy_every_human_gate_waits() -> None:
    g = _graph(None)
    report = g.run(_runner, critic=_critic())
    assert report.awaiting_approval == ["brief"]
    assert g.contract is None


# ---- R2: delegated gates pass on evidence, else escalate ----------------------


def test_delegated_gates_pass_on_panel_and_conformance_then_owner_keeps_animatic() -> None:
    g = _graph()
    report = g.run(_runner, critic=_critic(), conformance=_conformance())
    assert g.status("brief") is NodeStatus.CLEAN
    assert g.status("style_frames") is NodeStatus.CLEAN
    assert report.awaiting_approval == ["animatic"]
    brief = g.decision("brief")
    assert brief is not None and brief.approver == "delegate" and brief.human is None
    assert "panel: no confirmed HIGH" in brief.evidence
    assert g.contract is not None and g.contract.version == 1  # a delegated brief freezes it

    g.approve("animatic", "looks right")
    animatic = g.decision("animatic")
    assert animatic is not None and animatic.approver == "owner"
    g.run(_runner, critic=_critic(), conformance=_conformance())
    edit = g.decision("edit")
    assert g.status("edit") is NodeStatus.CLEAN
    assert edit is not None and edit.approver == "delegate"
    assert "conformance passed" in edit.evidence
    assert g.status("delivery") is NodeStatus.AWAITING_APPROVAL


@pytest.mark.parametrize(
    ("critic", "reason"),
    [
        (None, "no panel review"),
        (_critic(score=0.4), "overall score 0.4 below min_score 0.6"),
    ],
)
def test_delegated_gate_without_evidence_escalates_to_the_owner(
    critic: object, reason: str
) -> None:
    g = _graph()
    report = g.run(_runner, critic=critic)  # type: ignore[arg-type]
    assert report.awaiting_approval == ["brief"]
    decision = g.decision("brief")
    assert decision is not None and decision.approver is None
    assert decision.mode is GateMode.AUTO_DELEGATED and reason in decision.escalated
    g.approve("brief")
    after = g.decision("brief")
    assert after is not None and after.approver == "owner" and after.human == "approved"
    assert after.mode is GateMode.AUTO_DELEGATED


def _to_edit(g: ProductionGraph) -> None:
    g.run(_runner, critic=_critic())
    g.approve("animatic")


def test_delegated_edit_without_conformance_escalates() -> None:
    g = _graph()
    _to_edit(g)
    g.run(_runner, critic=_critic())
    assert g.status("edit") is NodeStatus.AWAITING_APPROVAL
    decision = g.decision("edit")
    assert decision is not None and decision.escalated == ["conformance not run"]


def test_failed_conformance_or_confirmed_high_never_auto_passes() -> None:
    g = _graph()
    _to_edit(g)
    g.run(_runner, critic=_critic(), conformance=_conformance("unmet"))
    assert g.status("edit") is NodeStatus.REJECTED
    high = Finding(
        reviewer="legibility", severity=Severity.HIGH, text="UI unreadable", confirmed=True
    )
    g2 = _graph()
    g2.run(_runner, critic=_critic(findings=[high]))
    assert g2.status("brief") is NodeStatus.REJECTED
    decision = g2.decision("brief")
    assert decision is not None and decision.approver is None


def test_escalations_list_every_reason() -> None:
    high = Finding(reviewer="r", severity=Severity.HIGH, text="bad", confirmed=True)
    verdict = CriticVerdict(passed=False, scores={}, findings=[high])
    reasons = DELEGATE.escalations("delivery", verdict, "failed")
    assert reasons == [
        "confirmed HIGH from r: bad",
        "no overall score",
        "conformance not passed",
    ]
    clean = CriticVerdict(passed=True, scores={"overall": 0.6})
    assert DELEGATE.escalations("brief", clean, "skipped") == []


# ---- R3: budget holds under the ceiling auto-approve ---------------------------


def _voice(ctx: RunContext) -> RunResult:
    chars = 900 if ctx.spec.kind == "voiceover" else 0
    return RunResult(output={"n": ctx.spec.id}, usage=Usage(elevenlabs_characters=chars))


def _voice_graph(policy: AutonomyPolicy | None) -> tuple[Producer, ProductionGraph]:
    producer = Producer()
    profile = load_profile("standard")
    plan = producer.breakdown(_brief(), profile, duration_s=60)
    graph = producer.new_graph("spellwise", plan, profile)
    graph.autonomy = policy
    return producer, graph


def _drive(producer: Producer, graph: ProductionGraph, **kw: object) -> list[object]:
    reports: list[object] = []
    for _ in range(20):
        report = producer.run(graph, _voice, **kw)  # type: ignore[arg-type]
        reports.append(report)
        waiting = [n for n in graph.order() if graph.status(n) is NodeStatus.AWAITING_APPROVAL]
        for nid in waiting:
            graph.approve(nid)
        if not report.run.did_work and not waiting:
            break
    return reports


def test_budget_hold_under_the_ceiling_auto_approves_and_is_recorded() -> None:
    caps = BudgetCaps(elevenlabs_characters=1000)
    producer, graph = _voice_graph(None)
    need = producer.estimate(graph).stage("voiceover").cost.elevenlabs_characters
    policy = AutonomyPolicy(budget_ceiling=BudgetCeiling(elevenlabs_characters=need + 10))
    reports = _drive(producer, graph, caps=caps, autonomy=policy)
    approved = [a for r in reports for a in r.auto_approved]  # type: ignore[attr-defined]
    assert [a.node_id for a in approved] == ["voiceover"]
    assert approved[0].approver == "auto" and approved[0].metric == "elevenlabs_characters"
    assert approved[0].ceiling == need + 10 and "<= ceiling" in approved[0].evidence
    assert graph.status("voiceover") is NodeStatus.CLEAN
    assert all(not r.needs_approval for r in reports)  # type: ignore[attr-defined]


def test_budget_hold_above_the_ceiling_still_waits() -> None:
    caps = BudgetCaps(elevenlabs_characters=1000)
    for policy in (None, AutonomyPolicy(budget_ceiling=BudgetCeiling(elevenlabs_characters=1001))):
        producer, graph = _voice_graph(None)
        reports = _drive(producer, graph, caps=caps, autonomy=policy)
        last = reports[-1]
        assert [h.node_id for h in last.needs_approval] == ["voiceover"]  # type: ignore[attr-defined]
        assert not last.auto_approved  # type: ignore[attr-defined]
        assert graph.status("voiceover") is NodeStatus.PENDING


# ---- persistence ---------------------------------------------------------------


def test_snapshot_keeps_policy_and_approvers() -> None:
    g = _graph()
    g.run(_runner, critic=_critic())
    snap = GraphSnapshot.model_validate_json(g.snapshot().model_dump_json())
    restored = ProductionGraph.from_snapshot(snap)
    assert restored.autonomy == DELEGATE
    assert restored.decision("brief") == g.decision("brief")
    legacy = json.loads(g.snapshot().model_dump_json())
    legacy.pop("autonomy")
    for d in legacy["decisions"].values():
        for key in ("approver", "evidence", "escalated"):
            d.pop(key)
    old = ProductionGraph.from_snapshot(GraphSnapshot.model_validate(legacy))
    assert old.autonomy is None and old.gate_for(old.spec("brief")) is GateMode.HUMAN


def test_load_autonomy_rejects_bad_files(tmp_path: Path) -> None:
    good = tmp_path / "p.json"
    good.write_text('{"delegate_kinds": ["brief"], "min_score": 0.7}', encoding="utf-8")
    assert load_autonomy(good) == AutonomyPolicy(delegate_kinds=("brief",), min_score=0.7)
    bad = tmp_path / "bad.json"
    bad.write_text('{"min_score": 3}', encoding="utf-8")
    with pytest.raises(ValueError, match="autonomy policy"):
        load_autonomy(bad)


# ---- R4: studio status shows who approved and on what evidence -----------------

cli = CliRunner()


def _brief() -> Brief:
    return Brief(
        id="spellwise",
        product="Spellwise",
        audience=AudienceProfile(personas=[Persona(name="Dev", description="ships often")]),
        sources=[ClaimSource(id="docs", title="Docs", ref="https://example.test/docs")],
        proof_points=[ProofPoint(claim="Fast spellcheck", source_id="docs")],
        cta="Try it",
    )


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "home"
    monkeypatch.setenv("SPECTRACUT_HOME", str(root))
    monkeypatch.setenv("SPECTRACUT_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.delenv(SIGNING_KEY_ENV, raising=False)
    monkeypatch.setattr(studio_mod, "runner_factory", lambda production, graph: _voice)
    return root


@pytest.fixture
def brief_file(tmp_path: Path) -> Path:
    path = tmp_path / "brief.json"
    path.write_text(_brief().model_dump_json(), encoding="utf-8")
    return path


def _line(output: str, node: str) -> str:
    return next(line for line in output.splitlines() if line.startswith(f"  {node}: "))


def test_cli_delegate_escalates_without_panel_and_status_shows_the_owner(
    home: Path, brief_file: Path
) -> None:
    created = cli.invoke(app, ["studio", "new", str(brief_file), "--delegate", "brief"])
    assert created.exit_code == 0, created.output
    assert "human gates=4" in created.output
    first = cli.invoke(app, ["studio", "run", "spellwise"])
    assert first.exit_code == 0 and "awaiting approval: brief" in first.output
    status = cli.invoke(app, ["studio", "status", "spellwise"])
    line = _line(status.output, "brief")
    # `studio run` reviews with shot_critic, which passes non-shot nodes without an
    # overall score (#250), so the delegated brief gate still escalates to the owner.
    assert "gate=auto_delegated" in line and "escalated=no overall score" in line
    assert cli.invoke(app, ["studio", "approve", "spellwise", "brief"]).exit_code == 0
    status = cli.invoke(app, ["studio", "status", "spellwise"])
    approved = _line(status.output, "brief")
    assert "human=approved" in approved and "by=owner" in approved


def test_cli_status_shows_delegate_evidence(home: Path, brief_file: Path) -> None:
    assert cli.invoke(app, ["studio", "new", str(brief_file), "--delegate", "brief"]).exit_code == 0
    store = ProductionStore(home)
    graph, _ = store.load("spellwise")
    graph.run(_voice, critic=_critic())
    store.save("spellwise", graph)
    status = cli.invoke(app, ["studio", "status", "spellwise"])
    line = _line(status.output, "brief")
    assert "by=delegate evidence=panel: no confirmed HIGH; overall=0.9>=0.6" in line


def test_cli_autonomy_file_auto_approves_budget_and_status_lists_it(
    home: Path, brief_file: Path, tmp_path: Path
) -> None:
    policy = tmp_path / "autonomy.json"
    policy.write_text(
        json.dumps({"budget_ceiling": {"elevenlabs_characters": 100000}}), encoding="utf-8"
    )
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    refused = cli.invoke(app, ["studio", "new", str(brief_file), "--autonomy", str(bad)])
    assert refused.exit_code == 2 and "error:" in refused.output
    created = cli.invoke(
        app,
        ["studio", "new", str(brief_file), "--cap-elevenlabs-characters", "1000",
         "--autonomy", str(policy)],
    )  # fmt: skip
    assert created.exit_code == 0, created.output
    seen = ""
    for _ in range(20):
        result = cli.invoke(app, ["studio", "run", "spellwise"])
        assert result.exit_code == 0, result.output
        assert "needs approval:" not in result.output
        seen += result.output
        graph, _ = ProductionStore(home).load("spellwise")
        waiting = [n for n in graph.order() if graph.status(n) is NodeStatus.AWAITING_APPROVAL]
        for nid in waiting:
            assert cli.invoke(app, ["studio", "approve", "spellwise", nid]).exit_code == 0
        if "auto-approved budget" in seen:
            break
    assert "auto-approved budget: voiceover: by=auto elevenlabs_characters projected=" in seen
    status = cli.invoke(app, ["studio", "status", "spellwise"])
    assert "budget approvals:" in status.output
    assert "  voiceover: by=auto elevenlabs_characters projected=" in status.output
    assert "ceiling=100000" in status.output
    assert "by=auto" in _line(status.output, "script")
