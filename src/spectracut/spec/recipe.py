# SPDX-License-Identifier: AGPL-3.0-or-later
"""Recipe: a replayable, hashed log of declarative tool calls for one shot.

Recipes carry tool names and JSON arguments only, never code: hosts execute
declarative recipes through the bpy runtime, never network-supplied Python.
"""

from __future__ import annotations

from typing import Annotated, ClassVar

from pydantic import Field, JsonValue, StringConstraints, field_validator

from spectracut.spec._base import (
    ContentHash,
    Slug,
    StrictModel,
    Version,
    content_hash,
    ensure_finite,
)

ToolName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")]
BlenderVersion = Annotated[str, StringConstraints(pattern=r"^\d+\.\d+(\.\d+)?$")]


class RecipeStep(StrictModel):
    """One tool call, e.g. ``camera.set_lens`` with ``{"mm": 35}``."""

    index: int = Field(ge=0)
    tool: ToolName
    args: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("args")
    @classmethod
    def _finite(cls, v: dict[str, JsonValue]) -> dict[str, JsonValue]:
        ensure_finite(v, "args")
        return v


class Recipe(StrictModel):
    """Replayable tool-call log for a scene, identified by its content hash."""

    HASH_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"shot_id", "blender_version", "steps", "parent_hash"}
    )

    id: Slug
    shot_id: Slug
    version: Version = 1
    blender_version: BlenderVersion
    steps: list[RecipeStep] = Field(default_factory=list)
    parent_hash: ContentHash | None = None

    @field_validator("steps")
    @classmethod
    def _contiguous(cls, v: list[RecipeStep]) -> list[RecipeStep]:
        for i, step in enumerate(v):
            if step.index != i:
                raise ValueError(f"steps[{i}].index is {step.index}, expected {i}")
        return v

    @property
    def content_hash(self) -> str:
        """Hash of the replay content only; id and version do not change it."""
        return content_hash(self, include=self.HASH_FIELDS)
