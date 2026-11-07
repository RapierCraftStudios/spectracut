# SPDX-License-Identifier: AGPL-3.0-or-later
"""BrandKit and Look: palette, type, Looks, voice, forbidden words and claim sources."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import yaml
from pydantic import Field, ValidationError, ValidationInfo, field_validator

from spectracut.spec._base import (
    HexColor,
    NonEmptyStr,
    Slug,
    StrictModel,
    UnitFloat,
    Version,
    ensure_unique_ids,
)
from spectracut.spec.brief import ClaimSource

RGB = tuple[float, float, float]


class Palette(StrictModel):
    """Named colour swatches, e.g. ``{"primary": "#1a2b3c"}``."""

    swatches: dict[Slug, HexColor] = Field(min_length=1)


class Typography(StrictModel):
    """Font family per role, e.g. ``{"display": "Inter"}``."""

    families: dict[Slug, NonEmptyStr] = Field(min_length=1)
    weights: list[Annotated[int, Field(ge=100, le=900)]] = Field(default_factory=lambda: [400])


class CDL(StrictModel):
    """ASC CDL grade."""

    slope: RGB = (1.0, 1.0, 1.0)
    offset: RGB = (0.0, 0.0, 0.0)
    power: RGB = (1.0, 1.0, 1.0)
    saturation: Annotated[float, Field(ge=0.0)] = 1.0


class Grade(StrictModel):
    """A CDL or a LUT per look; neither means a neutral grade."""

    cdl: CDL | None = None
    lut: NonEmptyStr | None = None

    @field_validator("lut")
    @classmethod
    def _one_grade(cls, v: str | None, info: ValidationInfo) -> str | None:
        if v is not None and info.data.get("cdl") is not None:
            raise ValueError("a grade is a CDL or a LUT, not both")
        return v


class Lens(StrictModel):
    chromatic_aberration: UnitFloat = 0.0
    vignette: UnitFloat = 0.0
    distortion: Annotated[float, Field(ge=-1.0, le=1.0)] = 0.0
    halation: UnitFloat = 0.0


class QAThresholds(StrictModel):
    """Per-look QA limits; QA reads these instead of one global limit."""

    grain_max: UnitFloat = 0.1
    haze_max: UnitFloat = 0.05
    bloom_max: UnitFloat = 0.1


class Look(StrictModel):
    """A versioned finishing preset: grade, lens, bloom, grain, motion blur and DoF."""

    id: Slug
    version: Version = 1
    grade: Grade = Field(default_factory=Grade)
    lens: Lens = Field(default_factory=Lens)
    bloom: UnitFloat = 0.0
    grain: UnitFloat = 0.0
    motion_blur: bool = True
    depth_of_field: bool = True
    qa_thresholds: QAThresholds = Field(default_factory=QAThresholds)

    @field_validator("qa_thresholds")
    @classmethod
    def _within_thresholds(cls, v: QAThresholds, info: ValidationInfo) -> QAThresholds:
        grain = info.data.get("grain")
        bloom = info.data.get("bloom")
        if grain is not None and grain > v.grain_max:
            raise ValueError(f"grain {grain} exceeds grain_max {v.grain_max}")
        if bloom is not None and bloom > v.bloom_max:
            raise ValueError(f"bloom {bloom} exceeds bloom_max {v.bloom_max}")
        return v


class VoiceChoice(StrictModel):
    provider: Slug
    voice_id: NonEmptyStr
    style: str = ""


class TokenSource(StrictModel):
    """Where a kit value was read from in the product: file, line, token and raw value."""

    path: NonEmptyStr
    """File relative to the product repo, e.g. ``src/app/globals.css``."""
    line: int = Field(ge=1)
    token: NonEmptyStr
    """Token read, e.g. ``--background`` or ``next/font/google``."""
    value: NonEmptyStr
    """Raw value, e.g. ``oklch(10% 0 0)`` or ``Inter``."""


class BrandKit(StrictModel):
    """Palette, type, hero object, logo, tone, Looks, voice, forbidden words and claim sources."""

    id: Slug
    name: NonEmptyStr
    palette: Palette
    typography: Typography
    hero_object_mesh: Slug | None = Field(default=None, description="Asset id of the hero mesh")
    logo: Slug | None = Field(default=None, description="Asset id of the logo")
    tone: NonEmptyStr
    looks: list[Look] = Field(min_length=1)
    default_look_id: Slug
    voice: VoiceChoice | None = None
    forbidden_words: list[NonEmptyStr] = Field(default_factory=list)
    claim_sources: list[ClaimSource] = Field(default_factory=list)
    token_sources: dict[str, TokenSource] = Field(
        default_factory=dict,
        description=(
            "Where each palette swatch and font family was read from, keyed "
            "palette.<swatch> or typography.<role>"
        ),
    )

    @field_validator("looks")
    @classmethod
    def _unique_looks(cls, v: list[Look]) -> list[Look]:
        ensure_unique_ids(v, "look")
        return v

    @field_validator("default_look_id")
    @classmethod
    def _default_look_exists(cls, v: str, info: ValidationInfo) -> str:
        looks = info.data.get("looks")
        if looks is not None and v not in {look.id for look in looks}:
            raise ValueError(f"default_look_id {v!r} is not one of the kit's looks")
        return v

    @field_validator("forbidden_words")
    @classmethod
    def _normalise_words(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for word in v:
            lowered = word.lower()
            if lowered not in out:
                out.append(lowered)
        return out

    @field_validator("claim_sources")
    @classmethod
    def _unique_sources(cls, v: list[ClaimSource]) -> list[ClaimSource]:
        ensure_unique_ids(v, "claim source")
        return v

    @field_validator("token_sources")
    @classmethod
    def _sources_name_values(
        cls, v: dict[str, TokenSource], info: ValidationInfo
    ) -> dict[str, TokenSource]:
        # A section that failed its own validation is absent here: skip its keys rather
        # than report a second error for the same fault.
        palette, typography = info.data.get("palette"), info.data.get("typography")
        for key in v:
            kind, _, name = key.partition(".")
            if kind == "palette":
                known = palette is None or name in palette.swatches
            elif kind == "typography":
                known = typography is None or name in typography.families
            else:
                known = False
            if not known:
                raise ValueError(f"token_sources key {key} names no swatch or family")
        return v


def load_brand_kit(path: Path) -> BrandKit:
    """Read a ``brand-kit.yaml`` and validate it as a :class:`BrandKit`.

    Raises :class:`ValueError` naming the file for unreadable YAML, a document
    that is not a mapping, or one that fails the schema.
    """
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read brand kit {path}: {exc.strerror or exc}") from None
    except yaml.YAMLError as exc:
        raise ValueError(f"brand kit {path} is not valid YAML: {exc}") from None
    if not isinstance(raw, dict):
        raise ValueError(f"brand kit {path} must be a YAML mapping")
    try:
        return BrandKit.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(p) for p in first["loc"]) or "<root>"
        raise ValueError(
            f"brand kit {path} is invalid: {exc.error_count()} error(s), "
            f"first: {loc}: {first['msg']}"
        ) from None
