# SPDX-License-Identifier: AGPL-3.0-or-later
"""Temp VO: Kokoro-82M (``kokoro`` family), a mono 24 kHz wav per prompt.

Loads the pinned local copy of ``hexgrad/Kokoro-82M`` (``config.json``,
``kokoro-v1_0.pth`` and ``voices/<voice>.pt``) that ``spectracut setup`` installs, never
the Hub. Precedent: ``scripts/model_check/tts_kokoro.py`` (voice ``af_heart``).
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path
from typing import Any

from spectracut.providers.base import ProviderError
from spectracut.providers.local.runtime import ModelSpec
from spectracut_host.loaders._base import BaseSession, check_vram_spec, weights_dir

SAMPLE_RATE = 24000
DEFAULT_VOICE = "af_heart"
LANG_CODE = "a"
"""American English, the voice set measured in local-model-selection.md."""
CONFIG = "config.json"
WEIGHTS = "kokoro-v1_0.pth"
_VOICE = re.compile(r"^[a-z]{1,3}_[a-z0-9_]+$")


class KokoroSession(BaseSession):
    def __init__(self, spec: ModelSpec, root: Path, model: Any, pipeline: Any) -> None:
        super().__init__(spec)
        self.root, self.model, self.pipeline = root, model, pipeline

    def voice_path(self, voice: str) -> Path:
        if not isinstance(voice, str) or not _VOICE.match(voice):
            raise ProviderError("local", f"{self.spec.id}: invalid voice {voice!r}")
        path = self.root / "voices" / f"{voice}.pt"
        if not path.is_file():
            raise ProviderError("local", f"{self.spec.id}: voice {voice} not installed at {path}")
        return path

    def _generate(
        self,
        prompt: str,
        out_path: Path,
        voice: str = DEFAULT_VOICE,
        speed: float = 1.0,
        **options: Any,
    ) -> None:
        text = prompt.strip()
        if not text:
            raise ProviderError("local", f"{self.spec.id}: nothing to say (empty text)")
        voice_file = self.voice_path(voice or DEFAULT_VOICE)
        np = importlib.import_module("numpy")
        sf = importlib.import_module("soundfile")
        chunks = []
        for _graphemes, _phonemes, audio in self.pipeline(
            text, voice=str(voice_file), speed=float(speed)
        ):
            if audio is not None:
                chunks.append(audio.numpy() if hasattr(audio, "numpy") else np.asarray(audio))
        if not chunks:
            raise ProviderError("local", f"{self.spec.id}: no audio generated")
        sf.write(str(out_path), np.concatenate(chunks), SAMPLE_RATE, format="WAV", subtype="PCM_16")

    def _unload(self) -> None:
        self.pipeline = None
        self.model = None


def load(spec: ModelSpec) -> KokoroSession:
    check_vram_spec(spec)
    root = weights_dir(spec)
    kokoro = importlib.import_module("kokoro")
    torch = importlib.import_module("torch")
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = (
        kokoro.KModel(repo_id=spec.source, config=str(root / CONFIG), model=str(root / WEIGHTS))
        .to(device)
        .eval()
    )
    pipeline = kokoro.KPipeline(lang_code=LANG_CODE, repo_id=spec.source, model=model)
    return KokoroSession(spec, root, model, pipeline)
