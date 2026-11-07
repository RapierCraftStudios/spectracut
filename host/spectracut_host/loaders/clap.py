# SPDX-License-Identifier: AGPL-3.0-or-later
"""Audio QA: ``laion/larger_clap_general`` (``clap`` family), embedding and caption scores.

Writes JSON ``{"sample_rate": 48000, "embedding": [...]}`` (L2-normalised audio
embedding) plus, with the ``captions`` option, ``"scores": {caption: probability}`` and
``"top"``. The audio is downmixed to mono, cast to float32 and resampled to 48 kHz before
the processor sees it: the precedent (``scripts/model_check/asr_clap.py``) resampled the
file as read, which is the preprocessing flagged in local-model-selection.md:111.
"""

from __future__ import annotations

import importlib
import json
import math
import tempfile
from pathlib import Path
from typing import Any

from spectracut.providers.base import ProviderError
from spectracut.providers.local.runtime import ModelSpec
from spectracut_host.loaders._audio import input_audio
from spectracut_host.loaders._base import BaseSession, check_vram_spec, weights_dir

SAMPLE_RATE = 48000
"""The rate CLAP's feature extractor was trained on."""


def to_mono_48k(data: Any, sr: int) -> Any:
    """``(n,)`` or ``(n, channels)`` samples at ``sr`` as 1-D float32 mono at 48 kHz."""
    torch = importlib.import_module("torch")
    wave = torch.as_tensor(data, dtype=torch.float32)
    if wave.ndim == 2:
        wave = wave.mean(dim=1)
    elif wave.ndim != 1:
        raise ValueError(f"audio must be (n,) or (n, channels), got {wave.ndim} dims")
    if int(sr) != SAMPLE_RATE:
        functional = importlib.import_module("torchaudio.functional")
        wave = functional.resample(wave, int(sr), SAMPLE_RATE)
    return wave.contiguous().numpy()


def _normalise(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec] if norm > 0 else vec


def _softmax(logits: list[float]) -> list[float]:
    top = max(logits)
    exps = [math.exp(x - top) for x in logits]
    total = sum(exps)
    return [e / total for e in exps]


class ClapSession(BaseSession):
    def __init__(self, spec: ModelSpec, model: Any, processor: Any, device: str) -> None:
        super().__init__(spec)
        self.model, self.processor, self.device = model, processor, device

    def _generate(self, prompt: str, out_path: Path, **options: Any) -> None:
        captions = options.get("captions")
        if captions is not None and (
            not isinstance(captions, list)
            or not captions
            or not all(isinstance(c, str) and c.strip() for c in captions)
        ):
            raise ProviderError("local", f"{self.spec.id}: captions must be a list of strings")
        torch = importlib.import_module("torch")
        sf = importlib.import_module("soundfile")
        with tempfile.TemporaryDirectory() as tmp:
            audio = input_audio(prompt, options, Path(tmp), model=self.spec.id)
            data, sr = sf.read(str(audio), always_2d=True, dtype="float32")
        mono = to_mono_48k(data, sr)
        result: dict[str, Any] = {"sample_rate": SAMPLE_RATE}
        with torch.no_grad():
            inputs = self.processor(audio=mono, sampling_rate=SAMPLE_RATE, return_tensors="pt")
            feats = self.model.get_audio_features(**inputs.to(self.device))
            feats = getattr(feats, "pooler_output", feats)
            result["embedding"] = _normalise([float(x) for x in feats[0].float().cpu().tolist()])
            if captions:
                inputs = self.processor(
                    text=captions,
                    audio=mono,
                    sampling_rate=SAMPLE_RATE,
                    return_tensors="pt",
                    padding=True,
                )
                out = self.model(**inputs.to(self.device))
                logits = [float(x) for x in out.logits_per_audio[0].float().cpu().tolist()]
                probs = _softmax(logits)
                result["scores"] = dict(zip(captions, probs, strict=True))
                result["top"] = captions[probs.index(max(probs))]
        if not all(math.isfinite(x) for x in result["embedding"]):
            raise ProviderError("local", f"{self.spec.id}: non-finite embedding")
        out_path.write_text(json.dumps(result), encoding="utf-8")

    def _unload(self) -> None:
        self.model = None
        self.processor = None


def load(spec: ModelSpec) -> ClapSession:
    check_vram_spec(spec)
    path = str(weights_dir(spec))
    torch = importlib.import_module("torch")
    transformers = importlib.import_module("transformers")
    device = "cuda" if torch.cuda.is_available() else "cpu"
    processor = transformers.ClapProcessor.from_pretrained(path)
    model = transformers.ClapModel.from_pretrained(path).to(device).eval()
    return ClapSession(spec, model, processor, device)
