# SPDX-License-Identifier: AGPL-3.0-or-later
"""The copy gate: every claim traces to a brief source, no forbidden word is used.

- :func:`check_script` fails a script line whose claim has no source id or cites
  one that is not among the brief's sources, and a line whose VO, on-screen text
  or claim text uses one of the brand's forbidden words (case-insensitive, whole
  words or phrases).
- :func:`check_brief` fails a creative brief that declares a source the product
  notes did not, so every proof point traces back to what Discovery read.
- :func:`guarded` wraps a graph runner so script and brief outputs pass the gate
  before they are accepted. A violation raises :class:`CopyGateError`, a
  :class:`~spectracut.studio.department.DepartmentOutputError`, so the graph
  records the node ``failed`` whatever its gate mode (even ``auto``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Literal

from pydantic import JsonValue

from spectracut.spec._base import StrictModel
from spectracut.spec.brief import CreativeBrief
from spectracut.studio.department import DepartmentOutputError
from spectracut.studio.departments.discovery import BRIEF, PRODUCT, ProductNotes
from spectracut.studio.departments.writer import SCRIPT, Script
from spectracut.studio.graph import ProductionGraph, RunContext, Runner, RunResult

ViolationKind = Literal["unsourced", "forbidden_word", "untraced_source", "missing_input"]


class Violation(StrictModel):
    """One copy-gate failure. ``line`` is the 0-based script line, if any."""

    line: int | None = None
    kind: ViolationKind
    detail: str


class CopyGateError(DepartmentOutputError):
    """A department output failed the copy gate."""

    def __init__(self, department: str, node_id: str, violations: Sequence[Violation]) -> None:
        self.violations = tuple(violations)
        parts = [
            (f"line {v.line} " if v.line is not None else "") + f"{v.kind}: {v.detail}"
            for v in self.violations
        ]
        super().__init__(department, node_id, "copy gate: " + "; ".join(parts))


def _normalise(words: Iterable[str]) -> list[str]:
    out: list[str] = []
    for word in words:
        lowered = word.strip().lower()
        if lowered and lowered not in out:
            out.append(lowered)
    return out


def _word_pattern(word: str) -> re.Pattern[str]:
    return re.compile(r"(?<!\w)" + re.escape(word) + r"(?!\w)", re.IGNORECASE)


def check_script(
    script: Script, brief: CreativeBrief, forbidden_words: Iterable[str] = ()
) -> list[Violation]:
    """Violations of ``script`` against the brief's sources and the forbidden words."""
    known = {s.id for s in brief.sources}
    patterns = [(w, _word_pattern(w)) for w in _normalise(forbidden_words)]
    found: list[Violation] = []
    for i, line in enumerate(script.lines):
        for claim in line.claims:
            if not claim.source_id.strip():
                found.append(
                    Violation(
                        line=i, kind="unsourced", detail=f"claim {claim.text!r} cites no source"
                    )
                )
            elif claim.source_id not in known:
                found.append(
                    Violation(
                        line=i,
                        kind="unsourced",
                        detail=(
                            f"claim {claim.text!r} cites {claim.source_id!r}, "
                            "which is not a source in the brief"
                        ),
                    )
                )
        texts = {"vo": line.vo, "on_screen": line.on_screen}
        for j, claim in enumerate(line.claims):
            texts[f"claims[{j}]"] = claim.text
        for word, pattern in patterns:
            for field, text in texts.items():
                if pattern.search(text):
                    found.append(
                        Violation(
                            line=i,
                            kind="forbidden_word",
                            detail=f"{field} uses forbidden word {word!r}",
                        )
                    )
    return found


def check_brief(brief: CreativeBrief, product: ProductNotes) -> list[Violation]:
    """Violations: brief sources the product notes did not declare."""
    known = {s.id for s in product.sources}
    return [
        Violation(
            kind="untraced_source",
            detail=f"brief source {s.id!r} is not one product understanding declared",
        )
        for s in brief.sources
        if s.id not in known
    ]


def _input_of_kind(ctx: RunContext, graph: ProductionGraph, kind: str) -> JsonValue | None:
    for input_id in ctx.spec.inputs:
        if graph.spec(input_id).kind == kind and input_id in ctx.inputs:
            return ctx.inputs[input_id]
    return None


def guarded(
    runner: Runner, *, graph: ProductionGraph, forbidden_words: Iterable[str] = ()
) -> Runner:
    """``runner`` with the copy gate applied to ``script`` and ``brief`` outputs.

    A script is checked against the accepted brief it reads (a script node with
    no brief input fails closed). A brief is checked against the product notes it
    reads, when it reads any. Other node kinds pass through unchanged.
    """
    words = _normalise(forbidden_words)

    def run(ctx: RunContext) -> RunResult:
        result = runner(ctx)
        spec = ctx.spec
        violations: list[Violation] = []
        if spec.kind == SCRIPT:
            brief = _input_of_kind(ctx, graph, BRIEF)
            if brief is None:
                violations.append(
                    Violation(kind="missing_input", detail="script has no brief to check against")
                )
            else:
                violations = check_script(
                    Script.model_validate(result.output),
                    CreativeBrief.model_validate(brief),
                    words,
                )
        elif spec.kind == BRIEF:
            product = _input_of_kind(ctx, graph, PRODUCT)
            if product is not None:
                violations = check_brief(
                    CreativeBrief.model_validate(result.output),
                    ProductNotes.model_validate(product),
                )
        if violations:
            raise CopyGateError(spec.department, spec.id, violations)
        return result

    return run
