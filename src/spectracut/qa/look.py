# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Look QA gate: a master's grain, haze and bloom against its Look's own thresholds.

Every Look declares ``qa_thresholds`` (``grain_max``, ``haze_max``,
``bloom_max``); :func:`look_gate` compares measured metrics against those and
never against a global constant, so a cinematic Look may carry more grain than
a clean one. The frame measurements that produce :class:`LookMetrics` come
from the automated QA gate (#27), whose report calls this contract.
"""

from __future__ import annotations

from typing import Literal

from spectracut.spec._base import StrictModel, UnitFloat, Version
from spectracut.spec.brand import Look

Metric = Literal["grain", "haze", "bloom"]


class LookMetrics(StrictModel):
    """Measured finishing metrics of a master, each 0..1."""

    grain: UnitFloat
    haze: UnitFloat
    bloom: UnitFloat


class LookCheck(StrictModel):
    metric: Metric
    value: float
    limit: float
    passed: bool


class LookQAReport(StrictModel):
    look_id: str
    look_version: Version
    checks: list[LookCheck]
    passed: bool

    def failures(self) -> list[LookCheck]:
        return [c for c in self.checks if not c.passed]


def look_gate(metrics: LookMetrics, look: Look) -> LookQAReport:
    """Check ``metrics`` against ``look.qa_thresholds``; a value equal to its limit passes."""
    limits = look.qa_thresholds
    rows: list[tuple[Metric, float, float]] = [
        ("grain", metrics.grain, limits.grain_max),
        ("haze", metrics.haze, limits.haze_max),
        ("bloom", metrics.bloom, limits.bloom_max),
    ]
    checks = [
        LookCheck(metric=m, value=value, limit=limit, passed=value <= limit)
        for m, value, limit in rows
    ]
    return LookQAReport(
        look_id=look.id,
        look_version=look.version,
        checks=checks,
        passed=all(c.passed for c in checks),
    )
