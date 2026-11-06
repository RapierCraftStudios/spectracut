# SPDX-License-Identifier: AGPL-3.0-or-later
"""Image exploration: local models by default, ChatGPT only when configured."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from spectracut.providers.base import ProviderError
from spectracut.storyboard.models import ImageRecord

CONFIG_KEY = "image_provider"
"""Key in the ``[ai]`` config table; ``chatgpt`` opts in to ChatGPT images."""


class ExploreProvider(Protocol):
    name: str

    def generate(self, prompt: str, out_path: Path, **options: Any) -> Path: ...


def choose_exploration_provider(
    config: Mapping[str, Any] | None,
    *,
    local: ExploreProvider,
    chatgpt: ExploreProvider | None = None,
) -> ExploreProvider:
    """The provider for exploration: ``local`` unless ``config[image_provider]`` is ``chatgpt``.

    Opting in without a ChatGPT provider is an error, never a silent fall back.
    """
    choice = str((config or {}).get(CONFIG_KEY, "local")).strip().lower() or "local"
    if choice == "local":
        return local
    if choice == "chatgpt":
        if chatgpt is None:
            raise ProviderError(
                "image", "image_provider is chatgpt but no ChatGPT provider is set up"
            )
        return chatgpt
    raise ProviderError("image", f"unknown image_provider {choice!r}; expected local or chatgpt")


def record_image(path: Path, provider: str) -> ImageRecord:
    digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    return ImageRecord(path=str(path), sha256=f"sha256:{digest}", provider=provider)


def explore(provider: ExploreProvider, prompt: str, out_path: Path) -> ImageRecord:
    """Generate one concept image and record its provenance."""
    return record_image(provider.generate(prompt, out_path), provider.name)
