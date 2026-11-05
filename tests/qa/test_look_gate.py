# SPDX-License-Identifier: AGPL-3.0-or-later
"""Each Look declares its QA thresholds and the QA gate reads them (#54)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from spectracut.finishing.looks import load_looks
from spectracut.qa.look import LookMetrics, look_gate
from spectracut.spec.brand import Look, QAThresholds

LOOKS = Path(__file__).resolve().parents[2] / "looks"


def test_gate_reads_each_looks_own_thresholds() -> None:
    looks = load_looks(LOOKS)
    grainy = LookMetrics(grain=0.08, haze=0.01, bloom=0.01)
    assert look_gate(grainy, looks["cinematic"]).passed
    report = look_gate(grainy, looks["clean"])
    assert not report.passed
    assert [(c.metric, c.limit) for c in report.failures()] == [("grain", 0.05)]
    assert (report.look_id, report.look_version) == ("clean", 1)


def test_thresholds_are_not_a_global_constant() -> None:
    metrics = LookMetrics(grain=0.3, haze=0.3, bloom=0.3)
    loose = Look(id="loose", qa_thresholds=QAThresholds(grain_max=0.5, haze_max=0.5, bloom_max=0.5))
    tight = loose.model_copy(
        update={"id": "tight", "qa_thresholds": QAThresholds(grain_max=0.2, haze_max=0.2)}
    )
    assert look_gate(metrics, loose).passed
    failed = look_gate(metrics, tight).failures()
    assert [(c.metric, c.limit) for c in failed] == [("grain", 0.2), ("haze", 0.2), ("bloom", 0.1)]


def test_limit_is_inclusive_and_every_metric_is_reported() -> None:
    look = Look(id="edge", qa_thresholds=QAThresholds(grain_max=0.1, haze_max=0.05, bloom_max=0.1))
    report = look_gate(LookMetrics(grain=0.1, haze=0.05, bloom=0.1), look)
    assert report.passed
    assert [(c.metric, c.value, c.limit, c.passed) for c in report.checks] == [
        ("grain", 0.1, 0.1, True),
        ("haze", 0.05, 0.05, True),
        ("bloom", 0.1, 0.1, True),
    ]
    over = look_gate(LookMetrics(grain=0.1, haze=0.0500001, bloom=0.0), look)
    assert [c.metric for c in over.failures()] == ["haze"]


def test_metrics_are_unit_floats() -> None:
    with pytest.raises(ValidationError):
        LookMetrics(grain=1.5, haze=0.0, bloom=0.0)
