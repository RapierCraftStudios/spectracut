# SPDX-License-Identifier: AGPL-3.0-or-later
"""Render profile of a host and the content-addressed frame cache key.

Frames rendered by different Blender versions, GPUs or drivers can differ, so the
frame cache key covers the recipe hash *and* the render profile: frames from
different machines are never mixed in a final shot.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, StringConstraints

from spectracut.spec._base import ContentHash, StrictModel, content_hash

ProfileStr = Annotated[str, StringConstraints(min_length=1, max_length=200)]


class RenderProfile(StrictModel):
    """What a render host reports about itself: Blender version, GPU and driver."""

    blender_version: ProfileStr
    gpu_model: ProfileStr = "unknown"
    gpu_driver: ProfileStr = "unknown"
    vram_gb: int = Field(default=0, ge=0)
    os: ProfileStr = "unknown"

    @property
    def content_hash(self) -> str:
        return content_hash(self)


def frame_cache_key(recipe_hash: ContentHash, profile: RenderProfile) -> str:
    """Return ``sha256:<hex>`` identifying frames of ``recipe_hash`` rendered on ``profile``."""
    if not isinstance(recipe_hash, str) or not recipe_hash.startswith("sha256:"):
        raise ValueError(f"recipe_hash must be a sha256: content hash, got {recipe_hash!r}")
    return content_hash({"recipe_hash": recipe_hash, "render_profile": profile.model_dump()})
