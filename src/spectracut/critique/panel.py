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

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from pydantic import JsonValue

from spectracut.providers.base import TextProvider, judge_usage
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


# ---- a panel over a text provider ------------------------------------------

FINDINGS_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["findings"],
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["severity", "text", "confidence"],
                "properties": {
                    "severity": {"type": "string", "enum": [s.value for s in Severity]},
                    "text": {"type": "string", "minLength": 1, "maxLength": 4000},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
            },
        }
    },
}
"""What one reviewer call returns: its findings, each with a severity and a confidence."""

CONFIRM_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["upheld"],
    "properties": {"upheld": {"type": "boolean"}, "why": {"type": "string"}},
}
"""What the independent confirm call returns."""

_REVIEW_SYSTEM = (
    "You are the {name} reviewer of a film production. Judge only against your rubric: "
    "{rubric} Report each problem once with severity high (the output must not ship), "
    "medium (worth one polish pass) or low (a note), and how sure you are (0-1). "
    "Report no findings when there is nothing to fix."
)
_CONFIRM_SYSTEM = (
    "You independently check a HIGH finding another reviewer raised on a film production "
    "node. Uphold it only when the output really has the problem and it must not ship."
)


def _prompt(spec: NodeSpec, output: JsonValue, extra: str = "") -> str:
    body = json.dumps(output, sort_keys=True, default=str)[:20000]
    return f"Node {spec.id} (kind {spec.kind}).{extra}\nOutput JSON:\n{body}"


def provider_review_call(provider: TextProvider) -> Callable[[str], ReviewCall]:
    """``call_for(name)``: one structured ``complete`` call per reviewer, returning findings."""

    def call_for(name: str) -> ReviewCall:
        def call(spec: NodeSpec, rubric: str, output: JsonValue) -> list[Finding]:
            done = provider.complete(
                _REVIEW_SYSTEM.format(name=name, rubric=rubric),
                _prompt(spec, output),
                schema=FINDINGS_SCHEMA,
            )
            rows = done.output.get("findings") if isinstance(done.output, dict) else None
            if not isinstance(rows, list):
                raise ValueError(f"reviewer {name}: {provider.name} returned no findings list")
            return [
                Finding.model_validate({**row, "reviewer": name})
                for row in rows
                if isinstance(row, dict)
            ]

        return call

    return call_for


def provider_confirm(provider: TextProvider) -> Confirm:
    """An independent confirm call for a HIGH finding; anything but ``upheld: true`` demotes it."""

    def confirm(spec: NodeSpec, finding: Finding, output: JsonValue) -> bool:
        done = provider.complete(
            _CONFIRM_SYSTEM,
            _prompt(spec, output, f"\nFinding ({finding.reviewer}): {finding.text}"),
            schema=CONFIRM_SCHEMA,
        )
        return isinstance(done.output, dict) and done.output.get("upheld") is True

    return confirm


def text_panel(provider: TextProvider) -> ReviewerPanel:
    """The five built-in reviewers and the confirm check, each a call to ``provider``."""
    return ReviewerPanel(
        default_reviewers(provider_review_call(provider)), provider_confirm(provider)
    )
