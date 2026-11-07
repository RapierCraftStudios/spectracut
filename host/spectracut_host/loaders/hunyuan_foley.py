# SPDX-License-Identifier: AGPL-3.0-or-later
"""HunyuanVideo-Foley XL (``hunyuan-foley``): video-to-audio foley on the model host.

``load(spec)`` imports the pinned source checkout (``.../hunyuan-foley-xl/src``, from
``spectracut setup``) and loads the XL checkpoint with model offload, as measured with
``infer.py --enable_offload`` (scripts/model_check/run_cli.py), so it fits the 8 GB card.
``generate(prompt, out_path=..., video_path=...)`` hears the clip at ``video_path`` (a
file on this host) and writes a 16-bit PCM wav. The pinned source loads its SigLIP2 and
CLAP encoders by Hugging Face hub id; the loader points those ids at the copies setup
pulled and runs with ``HF_HUB_OFFLINE=1``, so nothing is fetched at runtime.
"""

from __future__ import annotations

import importlib
import os
import random
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from spectracut.providers.base import ProviderError
from spectracut.providers.local.runtime import ModelSpec
from spectracut_host.loaders._base import (
    BaseSession,
    check_vram_spec,
    source_dir,
    weights_dir,
)

CONFIG = Path("configs") / "hunyuanvideo-foley-xl.yaml"
MODEL_SIZE = "xl"
SIGLIP2 = "google/siglip2-base-patch16-512"
CLAP = "laion/larger_clap_general"
#: The names in the upstream ``model_utils`` that load an encoder by hub id.
HUB_LOADERS = ("AutoModel", "AutoTokenizer", "ClapTextModelWithProjection")
OFFLINE_ENV = ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")


@contextmanager
def offline() -> Iterator[None]:
    """Run with the Hugging Face hub offline, restoring the previous env after."""
    saved = {k: os.environ.get(k) for k in OFFLINE_ENV}
    os.environ.update(dict.fromkeys(OFFLINE_ENV, "1"))
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class _LocalHub:
    """Wraps a transformers class so ``from_pretrained(<hub id>)`` reads a local copy."""

    def __init__(self, cls: Any, paths: dict[str, str]) -> None:
        self.wrapped = cls
        self.paths = paths

    def from_pretrained(self, name: Any, *args: Any, **kwargs: Any) -> Any:
        return self.wrapped.from_pretrained(self.paths.get(str(name), name), *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.wrapped, name)


def redirect_hub_ids(module: Any, paths: dict[str, str]) -> None:
    """Point ``module``'s hub-id loaders at ``paths`` (idempotent)."""
    for name in HUB_LOADERS:
        cls = getattr(module, name, None)
        if cls is None:
            continue
        if isinstance(cls, _LocalHub):
            cls = cls.wrapped
        setattr(module, name, _LocalHub(cls, paths))


def _import_source(src: Path) -> tuple[Any, Any]:
    path = str(src)
    if path not in sys.path:
        sys.path.insert(0, path)
    try:
        model_utils = importlib.import_module("hunyuanvideo_foley.utils.model_utils")
        feature_utils = importlib.import_module("hunyuanvideo_foley.utils.feature_utils")
    except ImportError as exc:
        raise ProviderError(
            "local",
            f"hunyuan-foley: cannot import the source at {src}: {exc}: run `spectracut setup`",
        ) from exc
    return model_utils, feature_utils


def _device(torch: Any) -> Any:
    return torch.device("cuda:0" if torch.cuda.is_available() else "cpu")


def _seed(torch: Any, seed: int) -> None:
    random.seed(seed)
    importlib.import_module("numpy").random.seed(seed)
    torch.manual_seed(seed)


def _write_wav(audio: Any, sample_rate: int, out_path: Path) -> None:
    """Write ``audio`` (``[channels, samples]`` or ``[samples]``) as 16-bit PCM."""
    soundfile = importlib.import_module("soundfile")
    if hasattr(audio, "detach"):
        audio = audio.detach().float().cpu().numpy()
    if getattr(audio, "ndim", 1) == 2:
        audio = audio.T
    soundfile.write(str(out_path), audio, int(sample_rate), subtype="PCM_16", format="WAV")


class HunyuanFoleySession(BaseSession):
    """The XL checkpoint loaded with offload; one wav per ``generate``."""

    def __init__(self, spec: ModelSpec, torch: Any, model_utils: Any, feature_utils: Any) -> None:
        super().__init__(spec)
        self.torch = torch
        self.model_utils: Any = model_utils
        self.feature_utils: Any = feature_utils
        self.model_dict: Any = None
        self.cfg: Any = None

    def _generate(
        self,
        prompt: str,
        out_path: Path,
        *,
        video_path: str | Path | None = None,
        seed: int | None = None,
        duration_s: float | None = None,
        guidance_scale: float = 4.5,
        num_inference_steps: int = 50,
        neg_prompt: str | None = None,
        **_: Any,
    ) -> None:
        # duration_s is accepted for the shared option shape; the clip sets the length.
        if video_path is None or not str(video_path).strip():
            raise ProviderError("local", f"{self.spec.id}: foley needs video_path")
        video = Path(video_path)
        if not video.is_file():
            raise ProviderError("local", f"{self.spec.id}: no video at {video} on this host")
        if seed is not None:
            _seed(self.torch, int(seed))
        with offline():
            visual, text, audio_len_s = self.feature_utils.feature_process(
                str(video), prompt, self.model_dict, self.cfg, neg_prompt=neg_prompt
            )
            audio, sample_rate = self.model_utils.denoise_process(
                visual,
                text,
                audio_len_s,
                self.model_dict,
                self.cfg,
                guidance_scale=guidance_scale,
                num_inference_steps=num_inference_steps,
            )
        _write_wav(audio[0], sample_rate, out_path)

    def _unload(self) -> None:
        self.model_dict = None
        self.cfg = None


def load(spec: ModelSpec) -> HunyuanFoleySession:
    """Load HunyuanVideo-Foley XL with offload from the files ``spectracut setup`` put here."""
    check_vram_spec(spec)
    src = source_dir(spec)
    weights = weights_dir(spec)
    encoders = {SIGLIP2: str(weights_dir(spec, SIGLIP2)), CLAP: str(weights_dir(spec, CLAP))}
    config = src / CONFIG
    if not config.is_file():
        raise ProviderError(
            "local", f"{spec.id}: config not found at {config}: run `spectracut setup`"
        )
    with offline():
        torch = importlib.import_module("torch")
        model_utils, feature_utils = _import_source(src)
        redirect_hub_ids(model_utils, encoders)
        session = HunyuanFoleySession(spec, torch, model_utils, feature_utils)
        try:
            session.model_dict, session.cfg = model_utils.load_model(
                str(weights),
                str(config),
                _device(torch),
                enable_offload=True,
                model_size=MODEL_SIZE,
            )
        except BaseException:
            session.close()
            raise
    return session
