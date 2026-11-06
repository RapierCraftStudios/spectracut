# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

from spectracut.critique.panel import Reviewer, ReviewerPanel, dedupe, default_reviewers
from spectracut.studio import NodeSpec
from spectracut.studio.gates import Finding, Severity


def _spec() -> NodeSpec:
    return NodeSpec(id="shot", kind="shots", department="shots")


def _f(severity: Severity, text: str, conf: float = 0.9) -> Finding:
    return Finding(reviewer="x", severity=severity, text=text, confidence=conf)


def test_each_reviewer_runs_its_own_call_with_its_own_rubric() -> None:
    seen: list[tuple[str, str]] = []

    def call_for(name: str):
        def call(_spec, rubric, _out):
            seen.append((name, rubric))
            return []

        return call

    panel = ReviewerPanel(default_reviewers(call_for), lambda *_: True)
    verdict = panel(_spec(), {})
    assert verdict.passed
    assert [n for n, _ in seen] == [
        "visual-craft",
        "story-claims",
        "sound",
        "brand-safety",
        "motion",
    ]
    assert len({r for _, r in seen}) == 5


def test_high_blocks_only_when_second_check_confirms() -> None:
    reviewer = Reviewer("sound", "mix", lambda *_: [_f(Severity.HIGH, "clipping")])
    blocked = ReviewerPanel([reviewer], lambda *_: True)(_spec(), {})
    assert not blocked.passed and blocked.findings[0].confirmed

    demoted = ReviewerPanel([reviewer], lambda *_: False)(_spec(), {})
    assert demoted.passed
    assert demoted.findings[0].severity is Severity.MEDIUM and not demoted.findings[0].confirmed


def test_low_confidence_high_is_not_confirmed_and_skips_the_check() -> None:
    calls: list[str] = []
    reviewer = Reviewer("sound", "mix", lambda *_: [_f(Severity.HIGH, "hum", conf=0.1)])
    verdict = ReviewerPanel([reviewer], lambda *_: calls.append("c") or True)(_spec(), {})
    assert verdict.passed and not calls


def test_findings_are_deduplicated_across_reviewers() -> None:
    a = Reviewer("sound", "mix", lambda *_: [_f(Severity.LOW, "Hiss in the VO.")])
    b = Reviewer("motion", "m", lambda *_: [_f(Severity.MEDIUM, "hiss in the vo", 0.7)])
    verdict = ReviewerPanel([a, b], lambda *_: True)(_spec(), {})
    assert len(verdict.findings) == 1
    only = verdict.findings[0]
    assert only.severity is Severity.MEDIUM and only.reviewers == ["motion", "sound"]
    assert len(dedupe([_f(Severity.LOW, "a"), _f(Severity.LOW, "b")])) == 2


def test_judge_usage_is_summed() -> None:
    class J:
        last_usage = {"input_tokens": 10, "output_tokens": 2}

    r1 = Reviewer("sound", "mix", lambda *_: [_f(Severity.HIGH, "x")], judge=J())
    verdict = ReviewerPanel([r1], lambda *_: True, confirm_judge=J())(_spec(), {})
    assert verdict.usage == {"input_tokens": 20, "output_tokens": 4}
