# SPDX-License-Identifier: AGPL-3.0-or-later
"""Production graph engine: dirty propagation, reuse by hash, gates, profiles, ledger."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import JsonValue, ValidationError

from spectracut.studio.gates import Finding, Severity
from spectracut.studio import (
    CriticVerdict,
    GateMode,
    GatePolicy,
    Ledger,
    LedgerEntry,
    NodeSpec,
    NodeStatus,
    ProductionGraph,
    QualityProfile,
    RunContext,
    RunResult,
    Tier,
    Usage,
    available_profiles,
    load_profile,
)


class FakeRunner:
    """Deterministic department: output = params plus input outputs."""

    def __init__(self, fail: frozenset[str] = frozenset()) -> None:
        self.calls: list[str] = []
        self.contexts: list[RunContext] = []
        self.fail = fail

    def __call__(self, ctx: RunContext) -> RunResult:
        self.calls.append(ctx.spec.id)
        self.contexts.append(ctx)
        if ctx.spec.id in self.fail:
            raise RuntimeError("render host lost")
        output: JsonValue = {
            "kind": ctx.spec.kind,
            "params": ctx.spec.params,
            "inputs": ctx.inputs,
            "tier": ctx.tier.value,
        }
        return RunResult(
            output=output,
            usage=Usage(
                gpu_minutes=1.5,
                ai_usage={"claude_tokens": 100},
                elevenlabs_characters=40 if ctx.spec.kind == "voiceover" else 0,
            ),
        )


def node(nid: str, *inputs: str, kind: str | None = None, **params: Any) -> NodeSpec:
    return NodeSpec(
        id=nid, kind=kind or nid, department="dept", inputs=tuple(inputs), params=params
    )


def film() -> list[NodeSpec]:
    """brief -> script -> {voiceover, board}; asset_a -> shot_1; asset_b -> shot_2;
    board -> shot_1, shot_2; shots + voiceover -> edit."""
    return [
        node("brief", msg="one"),
        node("script", "brief"),
        node("voiceover", "script"),
        node("board", "script", kind="storyboard"),
        node("asset_a", kind="assets", name="phone"),
        node("asset_b", kind="assets", name="laptop"),
        node("shot_1", "board", "asset_a", kind="shots"),
        node("shot_2", "board", "asset_b", kind="shots"),
        node("edit", "shot_1", "shot_2", "voiceover"),
    ]


def built(**kw: Any) -> tuple[ProductionGraph, FakeRunner]:
    g = ProductionGraph("launch", **kw)
    g.add_nodes(film())
    r = FakeRunner()
    report = g.run(r)
    assert set(report.executed) == set(g.order())
    assert all(s is NodeStatus.CLEAN for s in g.statuses().values())
    return g, r


# ---- R1: dirty propagation -------------------------------------------------


@pytest.mark.parametrize(
    ("changed", "expected"),
    [
        ("asset_a", {"asset_a", "shot_1", "edit"}),
        ("asset_b", {"asset_b", "shot_2", "edit"}),
        ("voiceover", {"voiceover", "edit"}),
        ("board", {"board", "shot_1", "shot_2", "edit"}),
        ("brief", {"brief", "script", "voiceover", "board", "shot_1", "shot_2", "edit"}),
        ("edit", {"edit"}),
    ],
)
def test_change_marks_exactly_transitive_dependants_dirty(changed: str, expected: set[str]) -> None:
    g, _ = built()
    marked = g.set_params(changed, {"revised": True})
    assert marked == expected
    assert g.dirty() == expected
    for nid, status in g.statuses().items():
        assert status is (NodeStatus.DIRTY if nid in expected else NodeStatus.CLEAN), nid


def test_rerun_after_change_runs_only_dirty_nodes() -> None:
    g, r = built()
    r.calls.clear()
    g.set_params("asset_a", {"name": "tablet"})
    report = g.run(r)
    assert r.calls == ["asset_a", "shot_1", "edit"]
    assert report.executed == ["asset_a", "shot_1", "edit"]
    assert g.dirty() == frozenset()


def test_unchanged_update_marks_nothing() -> None:
    g, _ = built()
    assert g.update_node(g.spec("brief")) == frozenset()
    assert g.dirty() == frozenset()


# ---- R2: clean graph does no work; reuse by content hash -------------------


def test_rerunning_clean_graph_does_no_work() -> None:
    g, r = built()
    r.calls.clear()
    entries = len(g.ledger)
    report = g.run(r)
    assert r.calls == []
    assert not report.did_work
    assert len(g.ledger) == entries


def test_outputs_reused_by_content_hash() -> None:
    g, r = built()
    first = g.output("shot_1")
    g.set_params("asset_a", {"name": "tablet"})
    g.run(r)
    r.calls.clear()
    g.set_params("asset_a", {"name": "phone"})  # back to the original input
    report = g.run(r)
    assert r.calls == []
    assert set(report.reused) == {"asset_a", "shot_1", "edit"}
    assert g.output("shot_1") == first


def test_identical_work_shares_output_across_nodes() -> None:
    g = ProductionGraph("p")
    g.add_nodes([node("a", kind="assets", x=1), node("b", kind="assets", x=1)])
    r = FakeRunner()
    report = g.run(r)
    assert r.calls == ["a"]
    assert report.reused == ["b"]
    assert g.state("a").key == g.state("b").key


def test_unchanged_upstream_output_stops_propagation_at_run() -> None:
    g, r = built()
    r.calls.clear()
    marked = g.invalidate("asset_a")  # forced rebuild, same inputs
    assert marked == {"asset_a", "shot_1", "edit"}
    g.run(r)
    assert r.calls == []  # same key, so cached


def test_tier_is_part_of_the_key() -> None:
    g, r = built()
    r.calls.clear()
    g.update_node(g.spec("shot_1").model_copy(update={"tier": Tier.FINAL}))
    g.run(r)
    assert r.calls == ["shot_1", "edit"]
    assert g.state("shot_1").tier is Tier.FINAL


# ---- R3: gates ---------------------------------------------------------------


def passing(_spec: NodeSpec, _output: JsonValue) -> CriticVerdict:
    return CriticVerdict(passed=True, scores={"clarity": 0.9})


def test_gate_resolution_per_kind_and_per_production() -> None:
    policy = GatePolicy(
        defaults={"brief": GateMode.HUMAN, "shots": GateMode.CRITIC},
        production={"shots": GateMode.AUTO, "edit": GateMode.HUMAN},
        nodes={"shot_2": GateMode.HUMAN},
    )
    assert policy.resolve("brief", "brief") is GateMode.HUMAN
    assert policy.resolve("shot_1", "shots") is GateMode.AUTO
    assert policy.resolve("shot_2", "shots") is GateMode.HUMAN
    assert policy.resolve("edit", "edit") is GateMode.HUMAN
    assert policy.resolve("script", "script") is GateMode.AUTO


def test_profile_gates_with_production_override() -> None:
    profile = load_profile("standard")
    g = ProductionGraph(
        "p", profile=profile, gates=profile.gate_policy(production={"brief": GateMode.AUTO})
    )
    g.add_nodes(film())
    assert g.gate_for(g.spec("brief")) is GateMode.AUTO
    assert g.gate_for(g.spec("script")) is GateMode.CRITIC
    assert g.gate_for(g.spec("board")) is GateMode.CRITIC
    assert ProductionGraph("q", profile=profile).gate_for(g.spec("brief")) is GateMode.HUMAN


def test_human_gate_blocks_dependants_until_approved() -> None:
    g = ProductionGraph("p", gates=GatePolicy(defaults={"script": GateMode.HUMAN}))
    g.add_nodes(film())
    r = FakeRunner()
    report = g.run(r, critic=passing)
    assert sorted(r.calls) == ["asset_a", "asset_b", "brief", "script"]
    assert report.awaiting_approval == ["script"]
    assert g.status("script") is NodeStatus.AWAITING_APPROVAL
    downstream = {"voiceover", "board", "shot_1", "shot_2", "edit"}
    assert set(report.blocked) == downstream
    assert all(g.status(n) is NodeStatus.PENDING for n in downstream)
    with pytest.raises(ValueError):
        g.output("script")

    r.calls.clear()
    g.run(r)  # still waiting: nothing downstream runs
    assert r.calls == []

    g.approve("script", "good")
    decision = g.decision("script")
    assert decision is not None and decision.human == "approved"
    assert decision.critic is not None and decision.critic.passed
    g.run(r)
    assert set(r.calls) == downstream
    assert all(s is NodeStatus.CLEAN for s in g.statuses().values())


def test_human_rejection_keeps_dependants_blocked_until_rebuilt() -> None:
    g = ProductionGraph("p", gates=GatePolicy(production={"brief": GateMode.HUMAN}))
    g.add_nodes([node("brief"), node("script", "brief")])
    r = FakeRunner()
    g.run(r)
    g.reject("brief", "wrong audience")
    assert g.status("brief") is NodeStatus.REJECTED
    r.calls.clear()
    g.run(r)
    assert r.calls == []
    g.set_params("brief", {"audience": "developers"})
    g.run(r)
    assert r.calls == ["brief"]
    g.approve("brief")
    g.run(r)
    assert r.calls == ["brief", "script"]
    with pytest.raises(ValueError):
        g.approve("script")


def test_critic_gate_revises_within_budget_then_rejects() -> None:
    profile = QualityProfile(
        name="t",
        description="test",
        stages=("brief", "script"),
        revision_budget={"brief": 0, "script": 2},
        tier={"brief": Tier.PREVIEW, "script": Tier.PREVIEW},
        gates={"script": GateMode.CRITIC},
    )
    verdicts: list[str] = []

    def critic(spec: NodeSpec, _out: JsonValue) -> CriticVerdict:
        verdicts.append(spec.id)
        return CriticVerdict(passed=spec.id != "script", scores={"hook": 0.2}, comment="weak")

    g = ProductionGraph("p", profile=profile)
    g.add_nodes([node("brief"), node("script", "brief"), node("vo", "script", kind="voiceover")])
    r = FakeRunner()
    report = g.run(r, critic=critic)
    assert verdicts == ["script"] * 3  # brief is auto, so the Critic never sees it
    assert r.calls == ["brief", "script", "script", "script"]
    assert [c.attempt for c in r.contexts if c.spec.id == "script"] == [1, 2, 3]
    assert r.contexts[2].feedback is not None and r.contexts[2].feedback.comment == "weak"
    assert report.rejected == ["script"] and report.blocked == ["vo"]
    assert g.status("script") is NodeStatus.REJECTED
    outcomes = [e.outcome for e in g.ledger.entries_for("script")]
    assert outcomes == ["revise", "revise", "rejected"]


def test_critic_judge_usage_is_added_to_each_attempts_ledger_entry() -> None:
    profile = QualityProfile(
        name="t",
        description="test",
        stages=("brief", "script"),
        revision_budget={"brief": 0, "script": 1},
        tier={"brief": Tier.PREVIEW, "script": Tier.PREVIEW},
        gates={"script": GateMode.CRITIC},
    )
    calls: list[int] = []

    def critic(spec: NodeSpec, _out: JsonValue) -> CriticVerdict:
        calls.append(1)
        return CriticVerdict(
            passed=len(calls) > 1,
            comment="ok",
            usage={"critic_input_tokens": 100, "critic_requests": 1},
        )

    class Runner(FakeRunner):
        def __call__(self, ctx: RunContext) -> RunResult:
            res = super().__call__(ctx)
            return RunResult(output=res.output, usage=Usage(ai_usage={"critic_requests": 5}))

    g = ProductionGraph("p", profile=profile)
    g.add_nodes([node("brief"), node("script", "brief")])
    g.run(Runner(), critic=critic)
    entries = g.ledger.entries_for("script")
    assert [e.outcome for e in entries] == ["revise", "accepted"]
    assert [e.ai_usage for e in entries] == [{"critic_requests": 6, "critic_input_tokens": 100}] * 2


def test_department_failure_is_recorded_and_blocks_dependants() -> None:
    g = ProductionGraph("p")
    g.add_nodes([node("brief"), node("script", "brief")])
    report = g.run(FakeRunner(fail=frozenset({"brief"})))
    assert report.failed == ["brief"] and report.blocked == ["script"]
    state = g.state("brief")
    assert state.status is NodeStatus.FAILED and state.error == "RuntimeError: render host lost"
    assert [e.outcome for e in g.ledger.entries()] == ["failed"]


class _UsageError(RuntimeError):
    def __init__(self, msg: str, usage: Usage) -> None:
        super().__init__(msg)
        self.usage = usage


def test_failed_department_records_usage_carried_by_exception() -> None:
    def runner(ctx: RunContext) -> RunResult:
        if ctx.spec.id == "brief":
            raise _UsageError("gpu job failed", Usage(gpu_minutes=80.0))
        raise RuntimeError("no usage")

    g = ProductionGraph("p")
    g.add_nodes([node("brief"), node("other")])
    g.run(runner)
    assert g.state("brief").error == "_UsageError: gpu job failed"
    by_node = {e.node_id: e for e in g.ledger.entries()}
    assert by_node["brief"].outcome == "failed" and by_node["brief"].gpu_minutes == 80.0
    assert by_node["other"].outcome == "failed" and by_node["other"].gpu_minutes == 0.0
    assert g.ledger.totals().gpu_minutes == 80.0


# ---- graph structure -------------------------------------------------------


def test_structure_errors_are_rejected() -> None:
    g = ProductionGraph("p")
    with pytest.raises(ValueError, match="unknown node 'ghost'"):
        g.add_node(node("a", "ghost"))
    g.add_nodes([node("b", "a"), node("a")])  # any order
    assert g.order() == ("a", "b")
    with pytest.raises(ValueError, match="duplicate node id"):
        g.add_node(node("a"))
    with pytest.raises(ValueError, match="cycle"):
        g.update_node(node("a", "b"))
    assert g.order() == ("a", "b")  # unchanged after a rejected update
    with pytest.raises(ValidationError):
        node("c", "c")


# ---- R4: quality profiles are data files -----------------------------------


def test_profiles_are_data_files_selecting_stages_budgets_and_tiers() -> None:
    assert available_profiles() == ("draft", "standard", "premium", "cinema", "device-shot")
    ladder = ("draft", "standard", "premium", "cinema")
    previous: set[str] = set()
    for name in available_profiles():
        p = load_profile(name)
        assert p.name == name
        assert set(p.revision_budget) == set(p.stages) == set(p.tier)
        if name in ladder:
            assert previous <= set(p.stages), f"{name} drops stages of the cheaper profile"
            previous = set(p.stages)
    draft, standard, premium, cinema = (load_profile(n) for n in ladder)
    assert set(draft.tier.values()) == {Tier.PREVIEW}
    assert all(m is GateMode.AUTO for m in draft.gates.values())
    assert standard.tier["render"] is Tier.FINAL and "storyboard" in standard.stages
    assert "storyboard" not in draft.stages
    assert {"audience_research", "previs", "fx", "grade"} <= set(premium.stages)
    assert "reference_analysis" in cinema.stages and cinema.tier["render"] is Tier.HERO
    assert cinema.revision_budget["shots"] > premium.revision_budget["shots"] > 0
    assert standard.gates["animatic"] is GateMode.HUMAN


def test_profile_selects_node_tier_and_budget() -> None:
    cinema = load_profile("cinema")
    g = ProductionGraph("p", profile=cinema, gates=GatePolicy())  # all auto
    g.add_nodes(film())
    assert g.tier_for(g.spec("shot_1")) is Tier.FINAL
    assert g.tier_for(g.spec("brief")) is Tier.PREVIEW
    assert g.revision_budget(g.spec("shot_1")) == 3
    g.run(FakeRunner(), critic=passing)
    assert g.state("shot_1").tier is Tier.FINAL


def test_bad_profiles_are_rejected() -> None:
    with pytest.raises(ValueError, match="unknown quality profile"):
        load_profile("ultra")
    good = json.loads(load_profile("draft").model_dump_json())
    missing = {**good, "tier": {k: v for k, v in good["tier"].items() if k != "brief"}}
    with pytest.raises(ValidationError, match="tier has no entry"):
        QualityProfile.model_validate(missing)
    stray = {**good, "gates": {**good["gates"], "mixx": "auto"}}
    with pytest.raises(ValidationError, match="gates names stages"):
        QualityProfile.model_validate(stray)
    with pytest.raises(ValidationError) as err:
        QualityProfile.model_validate({**good, "tier": {**good["tier"], "brief": "ultra"}})
    assert err.value.errors()[0]["loc"] == ("tier", "brief")


# ---- R5: ledger --------------------------------------------------------------


def test_every_node_run_writes_a_ledger_entry() -> None:
    ticks = iter(float(i) for i in range(1000))
    g = ProductionGraph(
        "launch",
        clock=lambda: next(ticks),
        now=lambda: datetime(2026, 11, 4, tzinfo=UTC),
    )
    g.add_nodes(film())
    g.run(FakeRunner())
    entries = g.ledger.entries()
    assert [e.node_id for e in entries] == list(g.order())
    for e in entries:
        assert e.production_id == "launch"
        assert e.department == "dept"
        assert e.duration_s == 1.0
        assert e.gpu_minutes == 1.5
        assert e.ai_usage == {"claude_tokens": 100}
        assert e.outcome == "accepted" and e.node_key == g.state(e.node_id).key
    assert g.ledger.entries_for("voiceover")[0].elevenlabs_characters == 40
    totals = g.ledger.totals()
    assert totals.runs == 9 and totals.gpu_minutes == 13.5
    assert totals.ai_usage == {"claude_tokens": 900} and totals.elevenlabs_characters == 40
    lines = g.ledger.to_jsonl().splitlines()
    assert len(lines) == 9
    assert LedgerEntry.model_validate_json(lines[0]) == entries[0]
    assert len(Ledger(entries)) == 9


def test_ledger_rejects_negative_cost() -> None:
    with pytest.raises(ValidationError):
        Usage(gpu_minutes=-1.0)
    with pytest.raises(ValidationError):
        Usage(elevenlabs_characters=-5)


# ---- revision notes (#35) ---------------------------------------------------

NOTE_AT = datetime(2026, 11, 5, tzinfo=UTC)


def _reviewed_board() -> tuple[ProductionGraph, FakeRunner]:
    g = ProductionGraph("p", gates=GatePolicy(defaults={"board": GateMode.HUMAN}))
    g.add_nodes([node("board"), node("render", "board")])
    runner = FakeRunner()
    g.run(runner)
    assert g.status("board") is NodeStatus.AWAITING_APPROVAL
    return g, runner


def test_frame_note_reaches_the_department_on_rebuild() -> None:
    from spectracut.studio import RevisionNote

    g, runner = _reviewed_board()
    note = RevisionNote(
        node_id="board", frame=12, timecode="00:00:00:12", text="logo too small", created_at=NOTE_AT
    )
    g.add_note("board", note)
    g.reject("board", "see notes")
    g.invalidate("board")
    runner.contexts.clear()
    g.run(runner)
    ctx = next(c for c in runner.contexts if c.spec.id == "board")
    assert ctx.notes == (note,)
    assert ctx.notes[0].frame == 12 and ctx.notes[0].text == "logo too small"


def test_notes_survive_snapshot_and_old_snapshots_load() -> None:
    from spectracut.studio import RevisionNote
    from spectracut.studio.graph import GraphSnapshot

    g, _ = _reviewed_board()
    g.add_note(
        "board", RevisionNote(node_id="board", frame=3, text="warmer key", created_at=NOTE_AT)
    )
    snap = GraphSnapshot.model_validate_json(g.snapshot().model_dump_json())
    again = ProductionGraph.from_snapshot(snap)
    assert [n.text for n in again.notes("board")] == ["warmer key"]

    legacy = json.loads(g.snapshot().model_dump_json())
    del legacy["notes"]
    old = ProductionGraph.from_snapshot(GraphSnapshot.model_validate(legacy))
    assert old.notes("board") == ()


def test_approve_clears_notes_and_unknown_node_note_fails() -> None:
    from spectracut.studio import RevisionNote

    g, _ = _reviewed_board()
    g.add_note(
        "board", RevisionNote(node_id="board", frame=0, text="ok after fix", created_at=NOTE_AT)
    )
    g.approve("board")
    assert g.notes("board") == ()
    with pytest.raises(KeyError):
        g.add_note("nope", RevisionNote(node_id="nope", text="x", created_at=NOTE_AT))
    with pytest.raises(ValueError):
        g.add_note("board", RevisionNote(node_id="render", text="x", created_at=NOTE_AT))


def _severity_graph() -> ProductionGraph:
    profile = QualityProfile(
        name="t",
        description="test",
        stages=("brief", "script"),
        revision_budget={"brief": 0, "script": 0},
        tier={"brief": Tier.PREVIEW, "script": Tier.PREVIEW},
        gates={"script": GateMode.CRITIC},
    )
    g = ProductionGraph("p", profile=profile)
    g.add_nodes([node("brief"), node("script", "brief"), node("vo", "script", kind="voiceover")])
    return g


def _finding(severity: Severity, text: str = "note", *, confirmed: bool = False) -> Finding:
    return Finding(reviewer="sound", severity=severity, text=text, confirmed=confirmed)


def test_confirmed_high_finding_blocks_dependants() -> None:
    g = _severity_graph()
    high = _finding(Severity.HIGH, confirmed=True)
    report = g.run(
        FakeRunner(),
        critic=lambda *_: CriticVerdict(passed=False, findings=[high]),
    )
    assert report.rejected == ["script"] and report.blocked == ["vo"]


def test_medium_finding_gets_one_polish_revision_then_is_accepted_with_notes() -> None:
    g = _severity_graph()
    r = FakeRunner()
    verdict = CriticVerdict(
        passed=True,
        findings=[
            _finding(Severity.MEDIUM, "tighten the cut"),
            _finding(Severity.LOW, "slight hiss"),
        ],
    )
    report = g.run(r, critic=lambda *_: verdict)
    assert r.calls.count("script") == 2  # one polish revision, never more
    assert g.status("script") is NodeStatus.CLEAN and not report.rejected
    assert r.contexts[2].feedback is not None
    outcomes = [e.outcome for e in g.ledger.entries_for("script")]
    assert outcomes == ["revise", "accepted"]
    texts = [n.text for n in g.notes("script")]
    assert any("tighten the cut" in t for t in texts) and any("slight hiss" in t for t in texts)
    assert len(texts) == 2


def test_low_findings_are_noted_without_a_revision() -> None:
    g = _severity_graph()
    r = FakeRunner()
    verdict = CriticVerdict(passed=True, findings=[_finding(Severity.LOW, "slight hiss")])
    g.run(r, critic=lambda *_: verdict)
    assert r.calls.count("script") == 1
    assert [n.text for n in g.notes("script")] == ["[low] sound: slight hiss"]
