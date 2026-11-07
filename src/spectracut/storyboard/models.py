# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pre-production artifacts: art direction, style frames and the storyboard."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, JsonValue, field_validator

from spectracut.spec._base import ContentHash, NonEmptyStr, Slug, StrictModel, ensure_unique_ids
from spectracut.spec.brand import Look, Palette
from spectracut.spec.spec import CameraMove


class MoodEntry(StrictModel):
    """One mood-board entry: a reference, what to take from it and what to avoid."""

    ref: NonEmptyStr
    take: NonEmptyStr
    avoid: str = ""


class ArtDirection(StrictModel):
    """The Art Director's output: mood board, palette, materials and the Look."""

    mood_board: list[MoodEntry] = Field(min_length=1)
    palette: Palette
    materials: list[NonEmptyStr] = Field(default_factory=list)
    look: Look


class ImageRecord(StrictModel):
    """An image on disk with its provenance."""

    path: NonEmptyStr
    sha256: ContentHash
    provider: NonEmptyStr
    """Name of the provider that made it (a local model, or ``codex-cli`` when configured)."""


class StyleFrame(StrictModel):
    """A key still that fixes the look. ``concept`` and ``render`` are filled after the plan."""

    id: Slug
    description: NonEmptyStr
    prompt: NonEmptyStr
    concept_ref: str = ""
    """The approved concept's beat or signature visual this frame shows, copied verbatim."""
    concept: ImageRecord | None = None
    """Image-exploration concept the Blender frame starts from."""
    render: ImageRecord | None = None
    """The Blender frame rendered on a render host."""
    tier: Literal["final"] | None = None


class StyleFrames(StrictModel):
    frames: list[StyleFrame] = Field(min_length=1)
    review_media: dict[str, JsonValue] | None = None
    """The review page's player descriptor for the gate (every rendered frame)."""

    @field_validator("frames")
    @classmethod
    def _unique(cls, v: list[StyleFrame]) -> list[StyleFrame]:
        ensure_unique_ids(v, "style frame")
        return v

    @property
    def rendered(self) -> bool:
        return all(f.render is not None and f.tier == "final" for f in self.frames)


class Board(StrictModel):
    """One storyboard frame: a shot with its camera and action."""

    shot_id: Slug
    beat: str = ""
    """Script beat this board covers; its VO is the board's temp VO."""
    size: NonEmptyStr = "medium"
    lens_mm: float = Field(default=50.0, gt=0)
    move: CameraMove = "static"
    action: NonEmptyStr
    screen_content: str = ""
    text: str = ""
    duration_s: float = Field(gt=0)
    image: str = ""


class Storyboard(StrictModel):
    boards: list[Board] = Field(min_length=1)

    @field_validator("boards")
    @classmethod
    def _one_per_shot(cls, v: list[Board]) -> list[Board]:
        ids = [b.shot_id for b in v]
        if len(set(ids)) != len(ids):
            raise ValueError("a storyboard has one board per shot")
        return v
