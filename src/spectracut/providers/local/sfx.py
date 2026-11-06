# SPDX-License-Identifier: AGPL-3.0-or-later
"""Local sound-effect generation: a cue's description becomes a text-to-audio prompt.

Sound effects and designed layers (impacts, whooshes, textures, ambiences) come from
the ``sfx`` model configured in the local registry. This module is only the prompt
and the provider accessor; it calls no external API.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from spectracut.providers.local.registry import LocalProvider, get_provider
from spectracut.providers.local.runtime import ModelRuntime

KIND = "sfx"


def build_prompt(
    description: str, *, size: str = "medium", duration_s: float, palette: str = ""
) -> str:
    """The prompt for one cue: what it is, how big, how long, in the Look's sonic palette."""
    parts = [description.strip(), f"{size} size", f"{duration_s:.2f} seconds"]
    if palette.strip():
        parts.append(f"sonic palette: {palette.strip()}")
    return ", ".join(parts)


def provider(
    runtime: ModelRuntime | None = None, *, config: Mapping[str, Any] | None = None
) -> LocalProvider:
    """The configured local sound-effect provider (the mock in mock mode)."""
    return get_provider(KIND, runtime, config=config)
