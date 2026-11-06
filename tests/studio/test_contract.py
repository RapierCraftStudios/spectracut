# SPDX-License-Identifier: AGPL-3.0-or-later
"""Film contract freezing, versioning, conformance verdicts and the graph block."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import JsonValue, ValidationError

from spectracut.studio.conformance import (
    ConformanceReview,
    Evidence,
    RowJudgement,
    conformance_verdict,
)
from spectracut.studio.contract import FilmContract, freeze_contract
from spectracut.studio.graph import ProductionGraph
from spectracut.studio.node import NodeSpec, NodeStatus
from spectracut.studio.store import ProductionError, ProductionStore

BRIEF: dict[str, JsonValue] = {
    "id": "spellwise",
    "message": "Spelling that keeps up",
    "proof_points": [
        {"claim": "Fast spellcheck", "source_id": "docs"},
        {"claim": "Works offline", "source_id": "docs"},
    ],
    "tone": "calm",
    "cta": "Try it",
}
EV = [Evidence(timecode_s=3.0, quote="Try it")]


def contract() -> FilmContract:
    return freeze_contract(BRIEF, length_s=30, aspects=["16:9"], brand_rules=["cheap"])


def review(contract: FilmContract, **status: str) -> ConformanceReview:
    return ConformanceReview(
        rows=[
            RowJudgement(row_id=r, status=status.get(r, "met"), evidence=EV)
            for r in contract.row_ids()
        ]
    )


def test_freeze_numbers_every_requirement() -> None:
    c = contract()
    assert c.row_ids() == tuple(f"R{i}" for i in range(1, 9))
    assert [r.kind for r in c.rows] == [
        "message", "proof_point", "proof_point", "cta", "length", "aspect", "tone", "brand_rule",
    ]  # fmt: skip
    assert c.rows[1].source_id == "docs"


def test_contract_is_immutable_and_brief_edit_adds_a_version() -> None:
    v1 = contract()
    with pytest.raises(ValidationError):
        v1.version = 2  # type: ignore[misc]
    assert (
        freeze_contract(BRIEF, length_s=30, aspects=["16:9"], brand_rules=["cheap"], prior=v1) == v1
    )
    edited = {**BRIEF, "cta": "Start free"}
    v2 = freeze_contract(edited, length_s=30, prior=v1)
    assert v2.version == 2 and v2.brief_hash != v1.brief_hash
    assert v1.rows[3].text == "Try it"


def test_store_writes_each_version_once(tmp_path: Path) -> None:
    store = ProductionStore(tmp_path)
    store.path("p").mkdir(parents=True)
    v1 = contract()
    store.save_contract("p", v1)
    store.save_contract("p", v1)
    v2 = freeze_contract({**BRIEF, "cta": "Go"}, prior=v1)
    store.save_contract("p", v2)
    assert store.contract("p") == v2 and store.contract("p", 1) == v1
    with pytest.raises(ProductionError):
        store.save_contract("p", v1.model_copy(update={"brief_hash": v2.brief_hash}))


def test_verdict_passed_failed_incomplete() -> None:
    c = contract()
    assert conformance_verdict(c, review(c)) == "passed"
    assert conformance_verdict(c, review(c, R2="unmet")) == "failed"
    missing = ConformanceReview(rows=review(c).rows[:-1])
    assert conformance_verdict(c, missing) == "incomplete"
    unevidenced = ConformanceReview(
        rows=[*review(c).rows[:-1], RowJudgement(row_id="R8", status="met")]
    )
    assert conformance_verdict(c, unevidenced) == "incomplete"
    dup = ConformanceReview(rows=[*review(c).rows, review(c).rows[0]])
    assert conformance_verdict(c, dup) == "incomplete"


def test_model_summary_does_not_decide_the_verdict() -> None:
    c = contract()
    bad = review(c, R1="unmet").model_copy(update={"summary": "all good", "claimed": "passed"})
    assert conformance_verdict(c, bad) == "failed"
    partial = ConformanceReview(rows=[], claimed="passed", summary="perfect")
    assert conformance_verdict(c, partial) == "incomplete"


def test_evidence_needs_a_location() -> None:
    with pytest.raises(ValidationError):
        Evidence(timecode_s=1.0)
    assert Evidence(frame=12).frame == 12


def _graph() -> ProductionGraph:
    g = ProductionGraph("launch")
    g.add_nodes(
        [
            NodeSpec(id="brief", kind="brief", department="d", params={"brief": BRIEF}),
            NodeSpec(id="edit", kind="edit", department="d", inputs=("brief",)),
            NodeSpec(id="delivery", kind="delivery", department="d", inputs=("edit",)),
        ]
    )
    return g


def run(g: ProductionGraph, conformance: Any | None) -> Any:
    from spectracut.studio.graph import RunResult
    from spectracut.studio.ledger import Usage

    return g.run(
        lambda ctx: RunResult(output={"n": ctx.spec.id}, usage=Usage()), conformance=conformance
    )


def test_approving_the_brief_freezes_a_contract() -> None:
    g = _graph()
    run(g, None)
    assert g.contract is not None and g.contract.version == 1 and len(g.contract.rows) == 5


@pytest.mark.parametrize("bad", ["failed", "incomplete"])
def test_failed_or_incomplete_conformance_blocks_delivery(bad: str) -> None:
    g = _graph()

    def reviewer(spec: NodeSpec, _out: JsonValue, c: FilmContract) -> ConformanceReview:
        if spec.kind != "edit":
            return review(c)
        r = review(c, R1="unmet") if bad == "failed" else review(c)
        return r if bad == "failed" else ConformanceReview(rows=r.rows[:-1], claimed="passed")

    report = run(g, reviewer)
    assert report.rejected == ["edit"]
    assert g.status("edit") is NodeStatus.REJECTED
    assert g.status("delivery") is not NodeStatus.CLEAN


def test_passing_conformance_lets_delivery_through() -> None:
    g = _graph()
    seen: list[str] = []

    def reviewer(spec: NodeSpec, _out: JsonValue, c: FilmContract) -> ConformanceReview:
        seen.append(spec.kind)
        return review(c)

    run(g, reviewer)
    assert seen == ["edit", "delivery"]
    assert g.status("delivery") is NodeStatus.CLEAN


def test_without_a_reviewer_the_graph_is_unchanged() -> None:
    g = _graph()
    run(g, None)
    assert all(s is NodeStatus.CLEAN for s in g.statuses().values())
