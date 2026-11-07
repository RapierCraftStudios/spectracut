# SPDX-License-Identifier: AGPL-3.0-or-later
"""ASR / alignment: faster-whisper large-v3-turbo (``faster-whisper`` family).

Writes the transcript JSON ``{"text", "language", "duration_s", "words": [{"word",
"start", "end", "probability"}]}`` (seconds) that :mod:`spectracut.audio.captions`
reads. Loads the pinned local CTranslate2 weights. Precedent:
``scripts/model_check/asr_clap.py`` (fp16, word timestamps, beam 1).
"""

from __future__ import annotations

import glob
import importlib
import json
import os
import site
import tempfile
from pathlib import Path
from typing import Any

from spectracut.providers.base import ProviderError
from spectracut.providers.local.runtime import ModelSpec
from spectracut_host.loaders._audio import input_audio
from spectracut_host.loaders._base import BaseSession, check_vram_spec, weights_dir

DEVICE = "cuda"
COMPUTE_TYPE = "float16"


def _cuda_dlls() -> None:
    """Make the CUDA DLLs loadable on Windows for CTranslate2 (pip nvidia wheels or the
    cuBLAS/cuDNN DLLs the CUDA torch wheel ships in ``torch/lib``)."""
    for sp in site.getsitepackages():
        dirs = glob.glob(os.path.join(sp, "nvidia", "*", "bin")) + glob.glob(
            os.path.join(sp, "torch", "lib")
        )
        for d in dirs:
            getattr(os, "add_dll_directory")(d)  # noqa: B009 (Windows only)
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")


class WhisperSession(BaseSession):
    def __init__(self, spec: ModelSpec, model: Any) -> None:
        super().__init__(spec)
        self.model = model

    def _generate(self, prompt: str, out_path: Path, **options: Any) -> None:
        language = options.get("language")
        if language is not None and not isinstance(language, str):
            raise ProviderError("local", f"{self.spec.id}: language must be a string")
        with tempfile.TemporaryDirectory() as tmp:
            audio = input_audio(prompt, options, Path(tmp), model=self.spec.id)
            segments, info = self.model.transcribe(
                str(audio), word_timestamps=True, beam_size=1, language=language
            )
            texts: list[str] = []
            words: list[dict[str, Any]] = []
            for seg in segments:  # a generator: decode while the input still exists
                texts.append(str(seg.text))
                for w in seg.words or ():
                    words.append(
                        {
                            "word": str(w.word).strip(),
                            "start": float(w.start),
                            "end": float(w.end),
                            "probability": float(w.probability),
                        }
                    )
        result = {
            "text": "".join(texts).strip(),
            "language": str(getattr(info, "language", "") or ""),
            "duration_s": float(getattr(info, "duration", 0.0) or 0.0),
            "words": words,
        }
        out_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")

    def _unload(self) -> None:
        self.model = None


def load(spec: ModelSpec) -> WhisperSession:
    check_vram_spec(spec)
    path = weights_dir(spec)
    if os.name == "nt":
        _cuda_dlls()
    faster_whisper = importlib.import_module("faster_whisper")
    model = faster_whisper.WhisperModel(str(path), device=DEVICE, compute_type=COMPUTE_TYPE)
    return WhisperSession(spec, model)
