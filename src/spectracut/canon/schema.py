# SPDX-License-Identifier: AGPL-3.0-or-later
"""Canon: the versioned story bible shared by one production or a whole universe.

Characters, environments, props, the Look, music themes and per-scene continuity
(time of day, damage, who holds what). Productions pin the canon version they use.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, ValidationInfo, field_validator

from spectracut.spec._base import NonEmptyStr, Slug, StrictModel, Version, ensure_unique_ids
from spectracut.spec.brand import VoiceChoice

CanonScope = Literal["production", "universe"]


class CharacterSheet(StrictModel):
    """Model, rig, wardrobe states and voice of one character."""

    id: Slug
    name: NonEmptyStr
    model_asset_id: Slug
    rig_asset_id: Slug | None = None
    wardrobe_states: list[NonEmptyStr] = Field(default_factory=list)
    voice: VoiceChoice | None = None


class Environment(StrictModel):
    id: Slug
    name: NonEmptyStr
    asset_ids: list[Slug] = Field(default_factory=list)


class Prop(StrictModel):
    id: Slug
    name: NonEmptyStr
    asset_id: Slug


class ContinuityState(StrictModel):
    """Per-scene continuity: time of day, damage by subject, props held by character."""

    scene_id: Slug
    time_of_day: str = ""
    damage: dict[Slug, NonEmptyStr] = Field(default_factory=dict)
    holdings: dict[Slug, list[Slug]] = Field(default_factory=dict)


class Canon(StrictModel):
    """Story bible: everything that must stay the same everywhere."""

    id: Slug
    scope: CanonScope
    version: Version = 1
    characters: list[CharacterSheet] = Field(default_factory=list)
    environments: list[Environment] = Field(default_factory=list)
    props: list[Prop] = Field(default_factory=list)
    look_id: Slug | None = None
    music_themes: list[NonEmptyStr] = Field(default_factory=list)
    continuity: list[ContinuityState] = Field(default_factory=list)

    @field_validator("characters")
    @classmethod
    def _unique_characters(cls, v: list[CharacterSheet]) -> list[CharacterSheet]:
        ensure_unique_ids(v, "character")
        return v

    @field_validator("environments")
    @classmethod
    def _unique_environments(cls, v: list[Environment]) -> list[Environment]:
        ensure_unique_ids(v, "environment")
        return v

    @field_validator("props")
    @classmethod
    def _unique_props(cls, v: list[Prop]) -> list[Prop]:
        ensure_unique_ids(v, "prop")
        return v

    @field_validator("continuity")
    @classmethod
    def _holdings_resolve(
        cls, v: list[ContinuityState], info: ValidationInfo
    ) -> list[ContinuityState]:
        characters = info.data.get("characters")
        props = info.data.get("props")
        if characters is None or props is None:  # reported on those fields
            return v
        char_ids = {c.id for c in characters}
        prop_ids = {p.id for p in props}
        for i, state in enumerate(v):
            for who, held in state.holdings.items():
                if who not in char_ids:
                    raise ValueError(f"continuity[{i}].holdings: unknown character {who!r}")
                for prop in held:
                    if prop not in prop_ids:
                        raise ValueError(
                            f"continuity[{i}].holdings[{who!r}]: unknown prop {prop!r}"
                        )
        return v


class CanonPin(StrictModel):
    """A production's pin on a canon version, so later episodes can't change earlier ones."""

    canon_id: Slug
    version: Version
