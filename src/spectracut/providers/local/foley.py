# SPDX-License-Identifier: AGPL-3.0-or-later
"""Local foley generation: a video-to-audio model hears the rendered picture.

The ``foley`` model gets the rendered clip as the ``video_path`` option and the
description as its text prompt. This module is only the prompt and the provider
accessor; it calls no external API.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from spectracut.providers.local.registry import LocalProvider, get_provider
from spectracut.providers.local.runtime import ModelRuntime

KIND = "foley"


def build_foley_prompt(
    description: str, *, size: str = "medium", duration_s: float, palette: str = ""
) -> str:
    """The foley prompt: the action to voice, matched to the picture it is given."""
    parts = [description.strip(), f"matched to the picture, {duration_s:.2f} seconds"]
    if palette.strip():
        parts.append(f"sonic palette: {palette.strip()}")
    return ", ".join(parts)


def foley_provider(
    runtime: ModelRuntime | None = None, *, config: Mapping[str, Any] | None = None
) -> LocalProvider:
    """The configured local foley provider (the mock in mock mode)."""
    return get_provider(KIND, runtime, config=config)
