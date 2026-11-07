# SPDX-License-Identifier: AGPL-3.0-or-later
"""``spectracut make``: one-line idea to delivered film with fake departments (#247).

The departments (``studio.runner_factory``), the reviewer panel and the conformance
reviewer are fakes; everything between them (intake, planning, the graph, gates,
autonomy, the supervisor, the store and the run report) is the real code.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from pydantic import JsonValue
from typer.testing import CliRunner

from spectracut.cli import make as make_mod
from spectracut.cli import studio as studio_mod
from spectracut.cli.app import app
from spectracut.critique.panel import ReviewCall, ReviewerPanel, default_reviewers
from spectracut.providers.base import ProviderAuthError
from spectracut.studio.conformance import ConformanceReview, Evidence, RowJudgement
from spectracut.studio.contract import FilmContract
from spectracut.studio.gates import Critic, Finding, Severity
from spectracut.studio.graph import ProductionGraph, RunContext, RunResult
from spectracut.studio.ledger import Usage
from spectracut.studio.node import NodeSpec, NodeStatus
from spectracut.studio.store import ProductionStore

IDEA = "launch film for AlterLab"
PID = "alterlab-launch"
README = """# AlterLab

AlterLab turns a product README into a launch film.
AlterLab plans, scripts, voices and edits the film with a studio of agents.
Teams ship a launch film in a day instead of a month.
"""


class FakeText:
    name = "fake-text"


def _deliverable(i: int, kind: str) -> dict[str, JsonValue]:
    return {
        "id": f"{kind}-{i}",
        "kind": kind,
        "length_s": None if kind == "master" else 15.0,
        "aspect": "16:9",
        "platform": "web",
        "resolution": [1920, 1080],
        "otio_ref": f"delivery/{kind}-{i}.otio",
        "otio_sha256": "0" * 64,
        "frames": {"0": "f" * 64},
        "qa_passed": True,
    }


DELIVERY: dict[str, JsonValue] = {
    "master": _deliverable(0, "master"),
    "cutdowns": [_deliverable(i, "cutdown") for i in range(1, 9)],
    "passed": True,
}


@dataclass
class Fakes:
    calls: list[str] = field(default_factory=list)
    interrupt: str | None = None
    """Raise KeyboardInterrupt the first time this node kind builds."""

    def runner(self, _production: object, _graph: ProductionGraph) -> Callable[..., RunResult]:
        def run(ctx: RunContext) -> RunResult:
            self.calls.append(ctx.spec.id)
            if ctx.spec.kind == self.interrupt:
                self.interrupt = None
                raise KeyboardInterrupt
            output: JsonValue = (
                DELIVERY
                if ctx.spec.kind == "delivery"
                else {"node": ctx.spec.id, "attempt": ctx.attempt}
            )
            return RunResult(output=output, usage=Usage(ai_usage={"claude_requests": 1}))

        return run


def fake_panel(_provider: object) -> Critic:
    """A MEDIUM finding on each node's first review, then clean: one polish pass per node."""
    seen: set[str] = set()

    def call_for(name: str) -> ReviewCall:
        def call(spec: NodeSpec, _rubric: str, _output: JsonValue) -> list[Finding]:
            if name != "visual-craft" or spec.id in seen:
                return []
            seen.add(spec.id)
            return [Finding(reviewer=name, severity=Severity.MEDIUM, text="tighten the pacing")]

        return call

    return ReviewerPanel(default_reviewers(call_for), lambda *_a: True)


def fake_conformance(status: str) -> Callable[[object], Any]:
    def factory(_provider: object) -> Callable[..., ConformanceReview]:
        def review(_spec: NodeSpec, _out: JsonValue, contract: FilmContract) -> ConformanceReview:
            return ConformanceReview(
                rows=[
                    RowJudgement(row_id=r, status=status, evidence=[Evidence(frame=0)])
                    for r in contract.row_ids()
                ]
            )

        return review

    return factory


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Fakes]:
    home = tmp_path / "home"
    readme = tmp_path / "README.md"
    readme.write_text(README, encoding="utf-8")
    fakes = Fakes()
    monkeypatch.setenv("SPECTRACUT_HOME", str(home))
    monkeypatch.setenv("SPECTRACUT_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.delenv("SPECTRACUT_PROVIDERS", raising=False)
    monkeypatch.delenv("SPECTRACUT_MODEL_HOST", raising=False)
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.setattr(studio_mod, "runner_factory", fakes.runner)
    monkeypatch.setattr(studio_mod, "text_provider_factory", FakeText)
    monkeypatch.setattr(make_mod, "panel_factory", fake_panel)
    monkeypatch.setattr(make_mod, "conformance_factory", fake_conformance("met"))
    return home, readme, fakes


def make(home: Path, readme: Path, *extra: str) -> tuple[int, str]:
    args = ["make", IDEA, "--source", str(readme), "--home", str(home), *extra]
    result = CliRunner().invoke(app, args)
    return result.exit_code, result.output


def report(home: Path, n: int = 1) -> tuple[dict[str, Any], str]:
    run = ProductionStore(home).path(PID) / "runs" / str(n)
    return json.loads((run / "report.json").read_text()), (run / "report.md").read_text()


def test_make_delivers_the_film_without_input_and_writes_the_run_report(env: Any) -> None:
    home, readme, fakes = env
    code, out = make(home, readme)
    assert code == 0, out
    assert "stop: delivered" in out
    graph, _ = ProductionStore(home).load(PID)
    assert {n: graph.status(n) for n in graph.order()} == dict.fromkeys(
        graph.order(), NodeStatus.CLEAN
    )
    assert graph.contract is not None
    data, md = report(home)
    assert data["stop"] == "delivered"
    rows = data["contract"]["rows"]
    assert rows and all(r["verdict"] == "met" for r in rows)
    assert {v["verdict"] for v in data["contract"]["verdicts"]} == {"passed"}
    gates = {g["kind"]: g for g in data["gates"]}
    assert set(gates) == {"brief", "concept", "style_frames", "animatic", "delivery"}
    assert all(g["approver"] == "delegate" and g["evidence"] for g in gates.values())
    assert any(p["source"] == "pass" for p in data["polish"])  # the MEDIUM polish pass
    assert data["ledger"]["totals"]["ai_usage"]["claude_requests"] >= len(graph.order())
    assert len(data["delivered"]) == 9
    assert data["delivered"][0]["otio_ref"] == "delivery/master-0.otio"
    for heading in ("## Contract", "## Gates", "## Polish notes", "## Cost ledger"):
        assert heading in md
    assert "delivery/master-0.otio" in md
    assert "approver=delegate" in md

    # a second run after delivery executes nothing
    built = len(fakes.calls)
    code, out = make(home, readme)
    assert code == 0, out
    assert "resuming alterlab-launch" in out
    assert "executed: nothing" in out
    assert len(fakes.calls) == built
    assert report(home, 2)[0]["executed"] == []


def test_rerun_after_an_interruption_resumes_and_never_rebuilds_finished_nodes(env: Any) -> None:
    home, readme, fakes = env
    fakes.interrupt = "storyboard"
    code, _ = make(home, readme)
    assert code != 0
    store = ProductionStore(home)
    graph, _ = store.load(PID)
    done = [n for n in graph.order() if graph.status(n) is NodeStatus.CLEAN]
    assert done and "storyboard" not in done
    entries = {n: len(graph.ledger.entries_for(n)) for n in done}

    code, out = make(home, readme)
    assert code == 0, out
    assert "resuming alterlab-launch" in out
    data, _ = report(home)
    assert not set(done) & set(data["executed"])
    assert "storyboard" in data["executed"]
    graph, _ = store.load(PID)
    assert {n: len(graph.ledger.entries_for(n)) for n in done} == entries


def test_missing_text_sign_in_stops_naming_the_provider_and_the_mock(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, readme, fakes = env

    def signed_out() -> FakeText:
        raise ProviderAuthError("claude", "not signed in")

    monkeypatch.setattr(studio_mod, "text_provider_factory", signed_out)
    code, out = make(home, readme)
    assert code == 3, out
    assert "stop: missing_prerequisite" in out
    assert "claude" in out and "SPECTRACUT_PROVIDERS=mock" in out
    data, _ = report(home)
    assert data["stop"] == "missing_prerequisite"
    assert "claude" in data["detail"] and "SPECTRACUT_PROVIDERS=mock" in data["detail"]
    assert fakes.calls == []


def test_spend_above_the_ceiling_stops(env: Any) -> None:
    home, readme, _ = env
    code, out = make(home, readme, "--cap-gpu-minutes", "0.001", "--ceiling-gpu-minutes", "0.002")
    assert code == 3, out
    assert "stop: spend_above_ceiling" in out
    data, md = report(home)
    assert data["stop"] == "spend_above_ceiling"
    assert any(b.startswith("needs approval") for b in data["budget"])
    assert "spend_above_ceiling" in md


def test_a_kept_gate_stops_for_the_owner(env: Any) -> None:
    home, readme, _ = env
    code, out = make(home, readme, "--keep", "delivery")
    assert code == 3, out
    assert "stop: owner_gate" in out and "delivery" in out
    data, _ = report(home)
    assert data["stop"] == "owner_gate"
    gate = next(g for g in data["gates"] if g["kind"] == "delivery")
    assert gate["status"] == "awaiting_approval" and gate["approver"] is None


def test_a_failed_contract_stops_after_escalation(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, readme, _ = env
    monkeypatch.setattr(make_mod, "conformance_factory", fake_conformance("unmet"))
    code, out = make(home, readme)
    assert code == 3, out
    assert "stop: contract_failed" in out
    data, _ = report(home)
    assert data["stop"] == "contract_failed"
    assert "edit" in data["detail"]
    assert {v["verdict"] for v in data["contract"]["verdicts"]} == {"failed"}
    assert any("escalated" in p["text"] for p in data["polish"] if p["source"] == "supervisor")
