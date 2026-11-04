# SPDX-License-Identifier: AGPL-3.0-or-later
"""Registry of every top-level schema, by stable name, for JSON Schema export."""

from __future__ import annotations

from typing import Any, Final

from pydantic import BaseModel

from spectracut.canon.schema import Canon
from spectracut.spec.asset import Asset, LicenseRecord
from spectracut.spec.brand import BrandKit, Look
from spectracut.spec.brief import AudienceProfile, Brief, CreativeBrief
from spectracut.spec.recipe import Recipe
from spectracut.spec.spec import Shot, Spec

SCHEMAS: Final[dict[str, type[BaseModel]]] = {
    "asset": Asset,
    "audience_profile": AudienceProfile,
    "brand_kit": BrandKit,
    "brief": Brief,
    "canon": Canon,
    "creative_brief": CreativeBrief,
    "license_record": LicenseRecord,
    "look": Look,
    "recipe": Recipe,
    "shot": Shot,
    "spec": Spec,
}


def export_json_schemas() -> dict[str, dict[str, Any]]:
    """Return the validation-mode JSON Schema of every registered schema."""
    return {name: model.model_json_schema(mode="validation") for name, model in SCHEMAS.items()}
