# SPDX-License-Identifier: AGPL-3.0-or-later
"""Quality profiles: data files that choose stages, revision budgets, tiers and gates.

Each profile is ``profiles/<name>.json`` in this package (spike § The studio,
Quality profiles choose the steps). Profiles are data, not code, so they can be
reviewed, diffed and later tuned from the step ledger.
"""

from __future__ import annotations

import json
from importlib import resources
from typing import Annotated

from pydantic import Field, model_validator

from spectracut.spec._base import NonEmptyStr, Slug, StrictModel, UnitFloat
from spectracut.studio.gates import GateMode, GatePolicy
from spectracut.studio.node import Tier

PROFILE_NAMES: tuple[str, ...] = ("draft", "standard", "premium", "cinema", "device-shot")
"""Built-in profiles, cheapest first."""


PosInt = Annotated[int, Field(ge=1)]


class BudgetSplit(StrictModel):
    """How one stage spends its Critic budget per attempt."""

    candidates: PosInt
    """Candidates generated per attempt (best-of-N); the Critic keeps one."""
    verify_samples: PosInt
    """Critic reviews per candidate; their scores are averaged."""


class CritiqueSettings(StrictModel):
    """The Critic's knobs for a profile: pass mark, loop window and per-stage budget split.

    A stage missing from ``best_of_n`` or ``verify_samples`` takes the BlenderGym
    budget rule (:meth:`QualityProfile.split_budget`).
    """

    pass_score: UnitFloat = 0.6
    """Aggregate Critic score an artifact needs to pass."""
    window: PosInt = 3
    """Recent iterations the revise loop keeps (plans, diffs, renders, verdicts)."""
    best_of_n: dict[Slug, PosInt] = Field(default_factory=dict)
    """Candidates per attempt, by stage."""
    verify_samples: dict[Slug, PosInt] = Field(default_factory=dict)
    """Critic reviews per candidate, by stage."""


class QualityProfile(StrictModel):
    """Which stages run, how many revisions each gets, at what tier, behind which gate."""

    name: Slug
    description: NonEmptyStr
    stages: tuple[Slug, ...] = Field(min_length=1)
    """Stages (node kinds) in production order."""
    revision_budget: dict[Slug, Annotated[int, Field(ge=0)]]
    """Critic-requested revisions allowed per stage, after the first attempt."""
    tier: dict[Slug, Tier]
    """Render tier per stage."""
    gates: dict[Slug, GateMode] = Field(default_factory=dict)
    """Gate per stage; a stage not listed is ``auto``."""
    critique: CritiqueSettings = Field(default_factory=CritiqueSettings)
    """Critic pass mark, revise-loop window and best-of-N / verify budget split."""

    @model_validator(mode="after")
    def _stages_covered(self) -> QualityProfile:
        stages = set(self.stages)
        if len(stages) != len(self.stages):
            raise ValueError("stages lists a stage twice")
        for field in ("revision_budget", "tier"):
            table: dict[str, object] = getattr(self, field)
            missing = [s for s in self.stages if s not in table]
            if missing:
                raise ValueError(f"{field} has no entry for stages {missing}")
        for field in ("revision_budget", "tier", "gates"):
            table = getattr(self, field)
            unknown = sorted(set(table) - stages)
            if unknown:
                raise ValueError(f"{field} names stages not in stages: {unknown}")
        for field in ("best_of_n", "verify_samples"):
            table = getattr(self.critique, field)
            unknown = sorted(set(table) - stages)
            if unknown:
                raise ValueError(f"critique.{field} names stages not in stages: {unknown}")
        return self

    def includes(self, stage: str) -> bool:
        return stage in self.stages

    def split_budget(self, stage: str) -> BudgetSplit:
        """Candidates and Critic reviews per attempt for ``stage``.

        Explicit ``critique`` values win. Otherwise the BlenderGym rule applies:
        with few revisions (0 or 1) spend on generating candidates (2 candidates,
        1 review); with more revisions spend on verifying (1 candidate, 2 reviews).
        """
        few = self.revision_budget.get(stage, 0) <= 1
        return BudgetSplit(
            candidates=self.critique.best_of_n.get(stage, 2 if few else 1),
            verify_samples=self.critique.verify_samples.get(stage, 1 if few else 2),
        )

    def gate_policy(
        self,
        production: dict[str, GateMode] | None = None,
        nodes: dict[str, GateMode] | None = None,
    ) -> GatePolicy:
        """Gate policy with this profile's gates as kind defaults plus overrides."""
        return GatePolicy(defaults=dict(self.gates), production=production or {}, nodes=nodes or {})


def available_profiles() -> tuple[str, ...]:
    return PROFILE_NAMES


def load_profile(name: str) -> QualityProfile:
    """Load and validate a built-in profile by name."""
    if name not in PROFILE_NAMES:
        raise ValueError(f"unknown quality profile {name!r}; expected one of {PROFILE_NAMES}")
    text = resources.files(__package__).joinpath("profiles", f"{name}.json").read_text("utf-8")
    profile = QualityProfile.model_validate(json.loads(text))
    if profile.name != name:
        raise ValueError(f"profile file {name}.json declares name {profile.name!r}")
    return profile
