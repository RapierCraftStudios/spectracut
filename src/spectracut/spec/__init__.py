# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pydantic schemas: Spec, BrandKit, Brief, Recipe, Shot, Variant.

``SCHEMAS`` and ``export_json_schemas`` load lazily from
:mod:`spectracut.spec.registry`, which also imports :mod:`spectracut.canon.schema`;
importing them eagerly here would make ``canon.schema`` circular.
"""

from __future__ import annotations

from typing import Any

from spectracut.spec._base import (
    HashedModel,
    StrictModel,
    canonical_json,
    content_hash,
)
from spectracut.spec.asset import Asset, AssetKind, LicenseRecord
from spectracut.spec.brand import (
    CDL,
    BrandKit,
    Grade,
    Lens,
    Look,
    Palette,
    QAThresholds,
    Typography,
    VoiceChoice,
)
from spectracut.spec.brief import (
    AudienceProfile,
    Brief,
    ClaimSource,
    CreativeBrief,
    Persona,
    ProofPoint,
)
from spectracut.spec.kitsu import (
    KITSU_ENTITY,
    KITSU_REVIEW_STATUS,
    KITSU_STATUS,
    EntityKind,
    ReviewState,
    TaskStatus,
)
from spectracut.spec.recipe import Recipe, RecipeStep
from spectracut.spec.spec import CameraSpec, Shot, Spec

__all__ = [
    "CDL",
    "KITSU_ENTITY",
    "KITSU_REVIEW_STATUS",
    "KITSU_STATUS",
    "SCHEMAS",
    "Asset",
    "AssetKind",
    "AudienceProfile",
    "BrandKit",
    "Brief",
    "CameraSpec",
    "ClaimSource",
    "CreativeBrief",
    "EntityKind",
    "Grade",
    "HashedModel",
    "Lens",
    "LicenseRecord",
    "Look",
    "Palette",
    "Persona",
    "ProofPoint",
    "QAThresholds",
    "Recipe",
    "RecipeStep",
    "ReviewState",
    "Shot",
    "Spec",
    "StrictModel",
    "TaskStatus",
    "Typography",
    "VoiceChoice",
    "canonical_json",
    "content_hash",
    "export_json_schemas",
]


def __getattr__(name: str) -> Any:
    if name in ("SCHEMAS", "export_json_schemas"):
        from spectracut.spec import registry

        return getattr(registry, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
