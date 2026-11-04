# SPDX-License-Identifier: AGPL-3.0-or-later
"""Asset and LicenseRecord: reusable artifacts and their provenance.

Every asset records its provenance. Material from non-commercial sources can
never be marked commercial-safe, so it never reaches a published master.
"""

from __future__ import annotations

from typing import Literal

from pydantic import ValidationInfo, field_validator

from spectracut.spec._base import HashedModel, NonEmptyStr, Slug, StrictModel, Version

AssetKind = Literal["model", "material", "rig", "look", "sound", "effect"]


class LicenseRecord(StrictModel):
    """Per asset: source, licence and whether it is commercial-safe."""

    source: NonEmptyStr
    licence: NonEmptyStr
    url: NonEmptyStr | None = None
    attribution: str = ""
    non_commercial: bool = False
    commercial_safe: bool

    @field_validator("commercial_safe")
    @classmethod
    def _nc_never_commercial(cls, v: bool, info: ValidationInfo) -> bool:
        if v and info.data.get("non_commercial"):
            raise ValueError("a non-commercial licence cannot be commercial-safe")
        return v


class Asset(HashedModel):
    """Modeled object, material, rig, Look, sound or effect; versioned and reusable."""

    id: Slug
    kind: AssetKind
    version: Version = 1
    name: NonEmptyStr
    recipe_ref: Slug | None = None
    file_ref: NonEmptyStr | None = None
    license: LicenseRecord

    @field_validator("file_ref")
    @classmethod
    def _exactly_one_ref(cls, v: str | None, info: ValidationInfo) -> str | None:
        if "recipe_ref" not in info.data:  # recipe_ref failed; reported there
            return v
        if (v is None) == (info.data["recipe_ref"] is None):
            raise ValueError("set exactly one of recipe_ref or file_ref")
        return v
