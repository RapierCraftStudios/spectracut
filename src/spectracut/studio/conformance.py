# SPDX-License-Identifier: AGPL-3.0-or-later
"""Conformance review: one judgement per contract row, the verdict computed by code.

A reviewer returns a :class:`ConformanceReview` of per-row judgements, each with
evidence. :func:`conformance_verdict` ignores the reviewer's own summary:

- ``incomplete``: a row is missing, repeated or unknown, or a judgement has no evidence;
- ``failed``: every row is judged but at least one is ``unmet``;
- ``passed``: every row is ``met`` with evidence.

Only ``passed`` lets edit and delivery through.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import Field, JsonValue, model_validator

from spectracut.spec._base import StrictModel
from spectracut.studio.contract import FilmContract, RowId

if TYPE_CHECKING:
    from spectracut.studio.node import NodeSpec

ConformanceVerdict = Literal["passed", "failed", "incomplete"]
CONFORMANCE_KINDS = frozenset({"edit", "delivery"})
"""Node kinds that run the conformance review."""


class Evidence(StrictModel):
    """Where a row is met or missed: a timecode with quoted text, or a frame id."""

    timecode_s: Annotated[float, Field(ge=0)] | None = None
    quote: str = ""
    """Quoted VO or on-screen text."""
    frame: Annotated[int, Field(ge=0)] | None = None

    @model_validator(mode="after")
    def _located(self) -> Evidence:
        if self.frame is None and not (self.quote.strip() and self.timecode_s is not None):
            raise ValueError("evidence needs a frame id, or a timecode with quoted text")
        return self


class RowJudgement(StrictModel):
    row_id: RowId
    status: Literal["met", "unmet"]
    evidence: list[Evidence] = Field(default_factory=list)
    note: str = ""


class ConformanceReview(StrictModel):
    rows: list[RowJudgement] = Field(default_factory=list)
    summary: str = ""
    """The reviewer's own words; never used for the verdict."""
    claimed: str = ""
    """The reviewer's own verdict; ignored."""


Conformance = Callable[["NodeSpec", JsonValue, FilmContract], ConformanceReview]
"""Reviews a node's output against the contract: ``conformance(spec, output, contract)``."""


def problems(contract: FilmContract, review: ConformanceReview) -> list[str]:
    """Why ``review`` is incomplete; empty when every row has one evidenced judgement."""
    known = set(contract.row_ids())
    seen: set[str] = set()
    out: list[str] = []
    for j in review.rows:
        if j.row_id not in known:
            out.append(f"unknown row {j.row_id}")
        elif j.row_id in seen:
            out.append(f"row {j.row_id} judged twice")
        elif not j.evidence:
            out.append(f"row {j.row_id} has no evidence")
        seen.add(j.row_id)
    out.extend(f"row {r} missing" for r in contract.row_ids() if r not in seen)
    return out


def conformance_verdict(contract: FilmContract, review: ConformanceReview) -> ConformanceVerdict:
    """The verdict, computed from the rows alone."""
    if problems(contract, review):
        return "incomplete"
    if any(j.status == "unmet" for j in review.rows):
        return "failed"
    return "passed"


def blockers(contract: FilmContract, review: ConformanceReview) -> list[str]:
    """Human-readable reasons a review is not ``passed``; empty when it passed."""
    reasons = problems(contract, review)
    reasons.extend(f"row {j.row_id} unmet" for j in review.rows if j.status == "unmet")
    return reasons
