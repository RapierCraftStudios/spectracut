# SPDX-License-Identifier: AGPL-3.0-or-later
"""Autonomy policy: which human gates a delegate may pass, and how much spend runs unasked.

The owner keeps every human gate unless the policy delegates it. A delegated
gate (:attr:`~spectracut.studio.gates.GateMode.AUTO_DELEGATED`) passes on its
own only on evidence:

- the review panel ran and found no confirmed HIGH;
- its ``overall`` score is at least :attr:`AutonomyPolicy.min_score`;
- for ``edit`` and ``delivery``, the contract conformance review ran and passed.

Anything else escalates to the owner with the reasons (:meth:`AutonomyPolicy.escalations`).
Missing evidence escalates; it never passes.

A budget hold (a node whose projected spend would exceed a cap) builds without
asking when the projected spend stays within the policy's ceiling for every
exceeded metric; it is recorded as a :class:`BudgetApproval` by ``auto``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError

from spectracut.spec._base import Slug, StrictModel, UnitFloat
from spectracut.studio.conformance import CONFORMANCE_KINDS
from spectracut.studio.gates import CriticVerdict, Severity

OVERALL = "overall"
"""Score key of the aggregate score (``spectracut.critique.critic.OVERALL``)."""

ConformanceOutcome = Literal["passed", "failed", "skipped"]
"""What the contract conformance review did for one attempt."""

Approver = Literal["owner", "delegate", "auto"]


class BudgetCeiling(StrictModel):
    """Projected production spend a delegate may approve past a cap; ``None`` means none."""

    elevenlabs_characters: int | None = Field(default=None, ge=0)
    gpu_minutes: float | None = Field(default=None, ge=0)
    ai_requests: int | None = Field(default=None, ge=0)

    def limit(self, metric: str) -> float | None:
        value = getattr(self, metric, None)
        return float(value) if value is not None else None


class AutonomyPolicy(StrictModel):
    """Delegated gates, the score a delegated gate needs, and the auto-approve spend ceiling."""

    delegate_kinds: tuple[Slug, ...] = ()
    """Human gates of these node kinds are passed by the delegate on evidence."""
    delegate_nodes: tuple[Slug, ...] = ()
    """Human gates of these node ids are passed by the delegate on evidence."""
    min_score: UnitFloat = 0.6
    """Lowest ``overall`` review score a delegated gate passes with."""
    budget_ceiling: BudgetCeiling = Field(default_factory=BudgetCeiling)
    """Spend a budget hold may reach and still build without the owner."""

    def delegates(self, node_id: str, kind: str) -> bool:
        return node_id in self.delegate_nodes or kind in self.delegate_kinds

    def escalations(
        self, kind: str, verdict: CriticVerdict | None, conformance: ConformanceOutcome
    ) -> list[str]:
        """Why a delegated gate must go to the owner; empty when it may pass on its own."""
        if verdict is None:
            return ["no panel review"]
        reasons = [
            f"confirmed HIGH from {f.reviewer}: {f.text[:200]}"
            for f in verdict.findings
            if f.severity is Severity.HIGH and f.confirmed
        ]
        if not verdict.passed and not reasons:
            reasons.append("review did not pass")
        score = verdict.scores.get(OVERALL)
        if score is None:
            reasons.append("no overall score")
        elif score < self.min_score:
            reasons.append(f"overall score {score:g} below min_score {self.min_score:g}")
        if kind in CONFORMANCE_KINDS and conformance != "passed":
            reasons.append(
                "conformance not run" if conformance == "skipped" else "conformance not passed"
            )
        return reasons

    def evidence(
        self, kind: str, verdict: CriticVerdict, conformance: ConformanceOutcome
    ) -> list[str]:
        """What a delegated gate passed on, for ``studio status``."""
        out = [
            "panel: no confirmed HIGH",
            f"overall={verdict.scores.get(OVERALL, 0.0):g}>={self.min_score:g}",
        ]
        if kind in CONFORMANCE_KINDS:
            out.append(f"conformance {conformance}")
        return out

    def within_ceiling(self, metric: str, projected: float) -> bool:
        ceiling = self.budget_ceiling.limit(metric)
        return ceiling is not None and projected <= ceiling


class BudgetApproval(StrictModel):
    """Who let a node build past a budget cap, and on what evidence."""

    node_id: Slug
    metric: Slug | None = None
    projected: float | None = None
    limit: float | None = None
    ceiling: float | None = None
    approver: Literal["owner", "auto"]
    evidence: str = ""

    def text(self) -> str:
        parts = [f"{self.node_id}: by={self.approver}"]
        if self.metric is not None and self.projected is not None:
            parts.append(f"{self.metric} projected={self.projected:g}")
        if self.ceiling is not None:
            parts.append(f"ceiling={self.ceiling:g}")
        if self.evidence:
            parts.append(f"evidence={self.evidence}")
        return " ".join(parts)


def load_autonomy(path: Path) -> AutonomyPolicy:
    """Read an :class:`AutonomyPolicy` JSON file; raises :class:`ValueError` when invalid."""
    try:
        return AutonomyPolicy.model_validate(json.loads(path.read_text("utf-8")))
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        raise ValueError(f"autonomy policy {path}: {exc}") from exc
