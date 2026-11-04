# SPDX-License-Identifier: AGPL-3.0-or-later
"""Spec and Shot: what to render, shot by shot."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator

from spectracut.spec._base import HashedModel, Slug, StrictModel, ensure_unique_ids

SpecFormat = Literal["tutorial", "explainer", "ad", "trailer"]
Aspect = Literal["16:9", "9:16", "1:1", "4:5"]
CameraMove = Literal["static", "push", "pull", "pan", "tilt", "orbit", "dolly", "crane", "handheld"]


class CameraSpec(StrictModel):
    lens_mm: float = Field(default=50.0, gt=0)
    move: CameraMove = "static"


class Shot(HashedModel):
    """Template id, slots, camera, duration in beats and notes; 1–n recipe versions."""

    id: Slug
    template_id: Slug
    slots: dict[Slug, str] = Field(default_factory=dict)
    camera: CameraSpec = Field(default_factory=CameraSpec)
    duration_beats: float = Field(gt=0)
    notes: str = ""
    recipe_ids: list[Slug] = Field(default_factory=list)


class Spec(StrictModel):
    """Format, length, aspects, shots, VO, captions, music, CTA and variant id."""

    id: Slug
    format: SpecFormat
    length_s: float = Field(gt=0)
    aspects: list[Aspect] = Field(min_length=1)
    shots: list[Shot] = Field(min_length=1)
    vo: str = ""
    captions: bool = True
    music: str = ""
    cta: str = ""
    variant_id: Slug | None = None

    @field_validator("aspects")
    @classmethod
    def _unique_aspects(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("aspects must be unique")
        return v

    @field_validator("shots")
    @classmethod
    def _unique_shots(cls, v: list[Shot]) -> list[Shot]:
        ensure_unique_ids(v, "shot")
        return v
