# SPDX-License-Identifier: AGPL-3.0-or-later
"""Temp VO: local text-to-speech for boards and animatics. No network, no ElevenLabs.

Final VO is ElevenLabs only (:mod:`spectracut.providers.elevenlabs`); everything before
the script locks is voiced here by the local ``tts`` model (Kokoro by default).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from spectracut.providers.local.registry import LocalProvider, get_provider
from spectracut.providers.local.runtime import ModelRuntime


class LocalTts:
    """Synthesises temp VO lines through the local ``tts`` provider."""

    def __init__(
        self, provider: LocalProvider | None = None, runtime: ModelRuntime | None = None
    ) -> None:
        self._provider = provider if provider is not None else get_provider("tts", runtime)

    @property
    def name(self) -> str:
        return self._provider.name

    @contextmanager
    def batch(self) -> Iterator[None]:
        """Keep the model warm for every line synthesised inside."""
        with self._provider.batch():
            yield

    def synthesize(self, text: str, out_path: Path, *, voice: str | None = None) -> Path:
        options = {"voice": voice} if voice else {}
        return self._provider.generate(text, Path(out_path), **options)
