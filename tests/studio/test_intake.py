# SPDX-License-Identifier: AGPL-3.0-or-later
"""Intake autofill: idea and sources to a sourced CreativeBrief with assumptions."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from spectracut.spec.brief import CreativeBrief
from spectracut.studio import intake

README = """# AlterLab

AlterLab turns a short idea into a rendered product film automatically.
Every render host runs only declarative recipes for safety.

```
ignored code sentence that is long enough to count.
```
Banned synergy is how the old pitch described this product line.
"""


def _sources() -> list[intake.Source]:
    return intake.load_sources(["https://example.test/README.md"], fetch=lambda _u: README)


def test_headless_brief_is_valid_with_assumptions_and_cited_proofs() -> None:
    brief = intake.build_brief("launch film for AlterLab", _sources(), headless=True)
    CreativeBrief.model_validate_json(brief.model_dump_json())
    assert brief.proof_points and {p.source_id for p in brief.proof_points} == {"readme-md"}
    assert not any("ignored code" in p.claim for p in brief.proof_points)
    aspects = {a.aspect for a in brief.assumptions}
    assert {"audience", "tone", "success_measure", "cta", "platforms", "offer"} <= aspects
    assert all(a.rationale for a in brief.assumptions)
    page = intake.assumptions_markdown(brief)
    assert "**tone**" in page and "## Dropped claims" in page


def test_doubts_are_batched_by_four_and_answers_are_used() -> None:
    seen: list[int] = []

    def ask(batch: Sequence[intake.Aspect]) -> list[int]:
        seen.append(len(batch))
        assert all(len(a.options) > 1 and 0 <= a.recommended < len(a.options) for a in batch)
        return [1] * len(batch)

    brief = intake.build_brief("launch film for AlterLab", _sources(), headless=False, ask=ask)
    assert seen and all(n <= intake.MAX_DOUBTS for n in seen)
    assert brief.tone == "Playful and bold"
    assert "tone" not in {a.aspect for a in brief.assumptions}


def test_batches_cap_at_four() -> None:
    aspects = intake.QUESTION_BANK["launch"] * 2
    assert [len(b) for b in intake.batches(aspects)] == [4, 4, 4]


def test_unsourced_claims_are_dropped_and_listed() -> None:
    brief = intake.build_brief(
        "launch film for AlterLab",
        _sources(),
        claims=[("Loved by everyone", None), ("Ten times faster", "ghost")],
    )
    assert all(p.claim not in {"Loved by everyone", "Ten times faster"} for p in brief.proof_points)
    assert sum("no fetched source" in c for c in brief.dropped_claims) == 2


def test_forbidden_words_enforced() -> None:
    brief = intake.build_brief("launch film for AlterLab", _sources(), forbidden_words=["Synergy"])
    assert all("synergy" not in p.claim.lower() for p in brief.proof_points)
    assert any("forbidden word" in c for c in brief.dropped_claims)
    with pytest.raises(intake.IntakeError, match="forbidden word"):
        intake.build_brief("launch film for AlterLab", _sources(), forbidden_words=["alterlab"])


def test_requires_sources_and_idea() -> None:
    with pytest.raises(intake.IntakeError):
        intake.build_brief("launch film for AlterLab", [])
    with pytest.raises(intake.IntakeError):
        intake.resolve("  ")
