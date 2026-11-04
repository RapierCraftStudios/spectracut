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

from spectracut.spec._base import NonEmptyStr, Slug, StrictModel
from spectracut.studio.gates import GateMode, GatePolicy
from spectracut.studio.node import Tier

PROFILE_NAMES: tuple[str, ...] = ("draft", "standard", "premium", "cinema")
"""Built-in profiles, cheapest first."""


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
        return self

    def includes(self, stage: str) -> bool:
        return stage in self.stages

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
