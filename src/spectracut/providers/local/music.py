# SPDX-License-Identifier: AGPL-3.0-or-later
"""Local score generation: a music brief becomes a music-model prompt, in stems when asked.

The ``music`` model is configured in the local registry. A stem is generated from the
same brief with the stem named in the prompt. This module is only the prompt and the
provider accessor; it calls no external API.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from spectracut.providers.local.registry import LocalProvider, get_provider
from spectracut.providers.local.runtime import ModelRuntime

KIND = "music"


def build_music_prompt(
    description: str,
    *,
    size: str = "medium",
    duration_s: float,
    palette: str = "",
    stem: str = "",
) -> str:
    """The prompt for a music brief; ``stem`` (e.g. ``drums``) asks for that stem alone."""
    parts = [description.strip(), f"{duration_s:.1f} seconds"]
    if stem:
        parts.append(f"isolated {stem} stem only")
    if palette.strip():
        parts.append(f"style: {palette.strip()}")
    return ", ".join(parts)


def music_provider(
    runtime: ModelRuntime | None = None, *, config: Mapping[str, Any] | None = None
) -> LocalProvider:
    """The configured local music provider (the mock in mock mode)."""
    return get_provider(KIND, runtime, config=config)
