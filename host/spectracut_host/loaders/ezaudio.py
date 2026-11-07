# SPDX-License-Identifier: AGPL-3.0-or-later
"""EzAudio-XL sound effects (family ``ezaudio``; precedent scripts/model_check/sfx_ezaudio.py).

The loader imports ``api.ezaudio`` from the pinned source checkout ``spectracut setup``
puts at ``<model root>/ezaudio/<model id>/src``. EzAudio reads its model config
(``ckpts/ezaudio-xl.yml``) relative to the working directory, so construction runs from
the source checkout. The DiT checkpoint, the VAE and the flan-t5-xl text encoder are
always read from the installed weights, never downloaded: the checkpoint paths are
passed in, and the text encoder name in the config is replaced with its local copy.
"""

from __future__ import annotations

import contextlib
import importlib
import math
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from spectracut.providers.base import ProviderError
from spectracut.providers.local.runtime import ModelSpec

from ._base import BaseSession, source_dir, weights_dir

EZAUDIO_REPO = "OpenSound/EzAudio"
T5_REPO = "google/flan-t5-xl"
MODEL_NAME = "s3_xl"
CKPT = Path("ckpts/s3/ezaudio_s3_xl.pt")
VAE = Path("ckpts/vae/1m.pt")
MAX_SECONDS = 10
"""EzAudio-XL generates at most 500 latent frames (10 s at 50 Hz); longer cues are padded."""
DDIM_STEPS = 50
GUIDANCE = 5.0
"""The settings measured on legion (sfx_ezaudio.py: steps=50 guidance=5)."""


def write_wav(out_path: Path, audio: Any, sr: int, duration_s: float) -> None:
    """Write ``audio`` (samples first) as a PCM wav of exactly ``duration_s`` seconds.

    Longer audio is trimmed, shorter audio is padded with silence. The format is explicit
    because ``models-serve`` hands sessions a path without a suffix.
    """
    np = importlib.import_module("numpy")
    sf = importlib.import_module("soundfile")
    n = max(1, round(float(duration_s) * sr))
    have = int(audio.shape[0])
    if have > n:
        audio = audio[:n]
    elif have < n:
        pad = np.zeros((n - have, *tuple(audio.shape[1:])), dtype=audio.dtype)
        audio = np.concatenate([audio, pad])
    sf.write(str(out_path), audio, sr, format="WAV", subtype="PCM_16")


def _require(path: Path, spec: ModelSpec) -> Path:
    if not path.is_file():
        raise ProviderError(
            "local", f"{spec.id}: weights not installed at {path}: run `spectracut setup`"
        )
    return path


class EzAudioSession(BaseSession):
    def __init__(self, spec: ModelSpec, model: Any, src: str) -> None:
        super().__init__(spec)
        self.model: Any = model
        self._src = src

    def _generate(
        self, prompt: str, out_path: Path, duration_s: float = 10.0, seed: int = 0, **_: Any
    ) -> None:
        seconds = min(MAX_SECONDS, max(1, math.ceil(float(duration_s))))
        sr, audio = self.model.generate_audio(
            prompt,
            length=seconds,
            guidance_scale=GUIDANCE,
            ddim_steps=DDIM_STEPS,
            random_seed=int(seed),
        )
        write_wav(out_path, audio, int(sr), duration_s)

    def _unload(self) -> None:
        self.model = None
        with contextlib.suppress(ValueError):
            sys.path.remove(self._src)


def load(spec: ModelSpec) -> EzAudioSession:
    src = source_dir(spec)
    weights = weights_dir(spec, EZAUDIO_REPO)
    t5 = weights_dir(spec, T5_REPO)
    ckpt, vae = _require(weights / CKPT, spec), _require(weights / VAE, spec)
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    with contextlib.chdir(src):
        api: Any = importlib.import_module("api.ezaudio")
        read_config: Callable[[str], dict[str, Any]] = api.load_yaml_with_includes

        def local_config(name: str) -> dict[str, Any]:
            params = read_config(name)
            params["text_encoder"]["model"] = str(t5)
            return params

        api.load_yaml_with_includes = local_config
        try:
            model = api.EzAudio(
                model_name=MODEL_NAME, ckpt_path=str(ckpt), vae_path=str(vae), device="cuda"
            )
        finally:
            api.load_yaml_with_includes = read_config
    return EzAudioSession(spec, model, str(src))
