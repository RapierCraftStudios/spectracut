# SPDX-License-Identifier: AGPL-3.0-or-later
"""A severity-aware reviewer panel: only a confirmed HIGH finding blocks a node.

Each :class:`Reviewer` is its own call with its own rubric (visual craft,
story and claims, sound, brand safety, motion) and returns :class:`Finding`
rows with a severity and a confidence. :class:`ReviewerPanel` is a graph
``Critic``: it merges duplicate findings across reviewers, asks an independent
check to confirm every HIGH one, and passes the node unless a HIGH is confirmed.
An unconfirmed HIGH is demoted to MEDIUM, so it earns a polish pass, not a stop.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from pydantic import JsonValue

from spectracut.providers.base import judge_usage
from spectracut.studio.gates import CriticVerdict, Finding, Severity
from spectracut.studio.node import NodeSpec

OVERALL = "overall"

ReviewCall = Callable[[NodeSpec, str, JsonValue], Sequence[Finding]]
"""``call(spec, rubric, output) -> findings``: one reviewer's single judge call."""

Confirm = Callable[[NodeSpec, Finding, JsonValue], bool]
"""``confirm(spec, finding, output) -> bool``: an independent second check of a HIGH finding."""

RUBRICS: dict[str, str] = {
    "visual-craft": "Composition, lighting, legibility, colour and polish of the frames.",
    "story-claims": "Narrative clarity; every on-screen claim traces to a source in the brief.",
    "sound": "Mix, clarity of voice-over, music fit, sync and absence of artefacts.",
    "brand-safety": "No third-party brands or marks, no forbidden words, licensed material only.",
    "motion": "Timing, easing, flicker, judder and continuity between shots.",
}
"""The built-in reviewers' rubrics, by reviewer name."""


@dataclass(frozen=True)
class Reviewer:
    """One panel member: a name, its own rubric, and the call that judges with it."""

    name: str
    rubric: str
    call: ReviewCall
    judge: object | None = None
    """The judge behind ``call``; its ``last_usage`` is added to the verdict's usage."""


def default_reviewers(
    call_for: Callable[[str], ReviewCall],
    judge_for: Callable[[str], object | None] = lambda _n: None,
) -> list[Reviewer]:
    """The five built-in reviewers, each with its own judge call from ``call_for(name)``."""
    return [
        Reviewer(name, rubric, call_for(name), judge_for(name)) for name, rubric in RUBRICS.items()
    ]


_RANK = {Severity.LOW: 0, Severity.MEDIUM: 1, Severity.HIGH: 2}


def _key(finding: Finding) -> tuple[str, int | None]:
    return re.sub(r"\W+", " ", finding.text).strip().lower(), finding.frame


def dedupe(findings: Sequence[Finding]) -> list[Finding]:
    """Merge findings with the same text and frame, keeping the highest severity and confidence."""
    merged: dict[tuple[str, int | None], Finding] = {}
    for f in findings:
        names = f.reviewers or [f.reviewer]
        held = merged.get(_key(f))
        if held is None:
            merged[_key(f)] = f.model_copy(update={"reviewers": list(names)})
            continue
        best = (
            f
            if (_RANK[f.severity], f.confidence) > (_RANK[held.severity], held.confidence)
            else held
        )
        merged[_key(f)] = best.model_copy(
            update={
                "confidence": max(f.confidence, held.confidence),
                "reviewers": sorted({*held.reviewers, *names}),
            }
        )
    return list(merged.values())


class ReviewerPanel:
    """A graph ``Critic`` over several reviewers, blocking only on a confirmed HIGH."""

    def __init__(
        self,
        reviewers: Sequence[Reviewer],
        confirm: Confirm,
        *,
        min_confidence: float = 0.5,
        confirm_judge: object | None = None,
    ) -> None:
        if not reviewers:
            raise ValueError("a panel needs at least one reviewer")
        self.reviewers = tuple(reviewers)
        self.confirm = confirm
        self.min_confidence = min_confidence
        self.confirm_judge = confirm_judge

    def __call__(self, spec: NodeSpec, output: JsonValue) -> CriticVerdict:
        raw: list[Finding] = []
        usage: dict[str, int] = {}
        for reviewer in self.reviewers:
            for f in reviewer.call(spec, reviewer.rubric, output):
                raw.append(f.model_copy(update={"reviewer": reviewer.name, "confirmed": False}))
            _add(usage, judge_usage(reviewer.judge))
        findings: list[Finding] = []
        for f in dedupe(raw):
            if f.severity is Severity.HIGH:
                upheld = f.confidence >= self.min_confidence and self.confirm(spec, f, output)
                if f.confidence >= self.min_confidence:
                    _add(usage, judge_usage(self.confirm_judge))
                f = f.model_copy(
                    update={"confirmed": True}
                    if upheld
                    else {"severity": Severity.MEDIUM, "confirmed": False}
                )
            findings.append(f)
        blocked = any(f.severity is Severity.HIGH and f.confirmed for f in findings)
        counts = {s: sum(1 for f in findings if f.severity is s) for s in Severity}
        head = "revise" if blocked else "pass"
        comment = f"{head}: " + ", ".join(f"{n} {s.value}" for s, n in counts.items())
        lines = [f"[{f.severity.value}] {f.reviewer}: {f.text}" for f in findings]
        return CriticVerdict(
            passed=not blocked,
            scores={OVERALL: 0.0 if blocked else 1.0},
            comment=" | ".join([comment, *lines]),
            findings=findings,
            usage=usage,
        )


def _add(total: dict[str, int], more: dict[str, int]) -> None:
    for k, v in more.items():
        total[k] = total.get(k, 0) + v
