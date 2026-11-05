# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pydantic schemas: Spec, BrandKit, Brief, Recipe, Shot, Variant.

The public records and hashing helpers are re-exported here. ``SCHEMAS`` is
resolved lazily because the registry imports ``spectracut.canon.schema``, which
itself imports this package.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from spectracut.spec._base import HashedModel, StrictModel, canonical_json, content_hash
from spectracut.spec.asset import Asset, LicenseRecord
from spectracut.spec.brand import BrandKit, Look, VoiceChoice
from spectracut.spec.brief import AudienceProfile, Brief, ClaimSource, CreativeBrief, ProofPoint
from spectracut.spec.recipe import Recipe, RecipeStep
from spectracut.spec.spec import Shot, Spec

if TYPE_CHECKING:
    from spectracut.spec.registry import SCHEMAS as SCHEMAS

__all__ = [
    "SCHEMAS",
    "Asset",
    "AudienceProfile",
    "BrandKit",
    "Brief",
    "ClaimSource",
    "CreativeBrief",
    "HashedModel",
    "LicenseRecord",
    "Look",
    "ProofPoint",
    "Recipe",
    "RecipeStep",
    "Shot",
    "Spec",
    "StrictModel",
    "VoiceChoice",
    "canonical_json",
    "content_hash",
]


def __getattr__(name: str) -> Any:
    if name == "SCHEMAS":
        from spectracut.spec.registry import SCHEMAS

        return SCHEMAS
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
