# SPDX-License-Identifier: AGPL-3.0-or-later
"""ACE-Step 1.5 music loader (``ace-step``): turbo DiT + 5 Hz LM 0.6B with CPU offload.

Follows ``scripts/model_check/music_ace.py``. ``spectracut setup`` puts the weights and the
pinned ACE-Step source under the host model root; this loader links them into the
``checkpoints`` folder the ACE-Step handlers read (no copy), runs one instrumental
``text2music`` cue and writes a WAV of exactly ``duration_s`` seconds to ``out_path``.
torch, soundfile and ``acestep`` are imported only when a model loads.
"""

from __future__ import annotations

import importlib
import sys
import tempfile
from pathlib import Path
from typing import Any

from spectracut.providers.base import ProviderError
from spectracut.providers.local.runtime import ModelSpec

from ._base import BaseSession, source_dir, weights_dir

DIT_REPO = "ACE-Step/Ace-Step1.5"
LM_REPO = "ACE-Step/acestep-5Hz-lm-0.6B"
DIT_CONFIG = "acestep-v15-turbo"
LM_NAME = "acestep-5Hz-lm-0.6B"
DEFAULT_DURATION_S = 30.0
STEPS = 8


def _link(target: Path, link: Path) -> None:
    if link.is_symlink() or link.exists():
        return
    link.symlink_to(target, target_is_directory=target.is_dir())


def link_checkpoints(src: Path, dit: Path, lm: Path) -> Path:
    """``<src>/checkpoints`` holding the DiT repo's entries and the LM dir, as symlinks."""
    ck = src / "checkpoints"
    ck.mkdir(exist_ok=True)
    for entry in dit.iterdir():
        _link(entry, ck / entry.name)
    _link(lm, ck / LM_NAME)
    return ck


def fit_frames(data: Any, frames: int) -> Any:
    """Trim or zero-pad ``data`` (frames x channels) along the first axis to ``frames``."""
    n = len(data)
    if n >= frames:
        return data[:frames]
    np = importlib.import_module("numpy")
    pad = np.zeros((frames - n, *data.shape[1:]), dtype=data.dtype)
    return np.concatenate([data, pad])


class AceStepSession(BaseSession):
    def __init__(self, spec: ModelSpec, dit: Any, llm: Any, src: Path, added_path: bool) -> None:
        super().__init__(spec)
        self.dit, self.llm, self.src, self.added_path = dit, llm, src, added_path

    def _generate(self, prompt: str, out_path: Path, **options: Any) -> None:
        duration = float(options.get("duration_s", DEFAULT_DURATION_S))
        if duration <= 0:
            raise ProviderError("local", f"{self.spec.id}: duration_s must be positive")
        seed = options.get("seed")
        inference = importlib.import_module("acestep.inference")
        sf = importlib.import_module("soundfile")
        params = inference.GenerationParams(
            task_type="text2music",
            thinking=True,
            caption=prompt,
            lyrics="[Instrumental]",
            duration=duration,
            inference_steps=STEPS,
            guidance_scale=1.0,
            seed=-1 if seed is None else int(seed),
            vocal_language="unknown",
        )
        config = inference.GenerationConfig(batch_size=1, audio_format="wav")
        with tempfile.TemporaryDirectory(prefix="ace-step-") as tmp:
            r = inference.generate_music(
                self.dit, self.llm, params=params, config=config, save_dir=tmp
            )
            if not r.success or not r.audios:
                raise ProviderError("local", f"{self.spec.id}: {r.status_message}")
            data, rate = sf.read(r.audios[0]["path"], always_2d=True)
            sf.write(str(out_path), fit_frames(data, round(duration * rate)), rate, format="WAV")

    def _unload(self) -> None:
        self.dit = self.llm = None
        if self.added_path:
            try:
                sys.path.remove(str(self.src))
            except ValueError:
                pass


def load(spec: ModelSpec) -> AceStepSession:
    src = source_dir(spec)
    dit_dir = weights_dir(spec, DIT_REPO)
    lm_dir = weights_dir(spec, LM_REPO)
    link_checkpoints(src, dit_dir, lm_dir)
    added = str(src) not in sys.path
    if added:
        sys.path.insert(0, str(src))
    try:
        handler = importlib.import_module("acestep.handler")
        llm_mod = importlib.import_module("acestep.llm_inference")
        dit = handler.AceStepHandler()
        status, ok = dit.initialize_service(
            project_root=str(src), config_path=DIT_CONFIG, device="auto", offload_to_cpu=True
        )
        if not ok:
            raise ProviderError("local", f"{spec.id}: DiT init failed: {status}")
        llm = llm_mod.LLMHandler()
        status, ok = llm.initialize(
            checkpoint_dir=str(src / "checkpoints"),
            lm_model_path=LM_NAME,
            backend="pt",
            device="auto",
            offload_to_cpu=True,
            dtype=None,
        )
        if not ok:
            raise ProviderError("local", f"{spec.id}: LM init failed: {status}")
        return AceStepSession(spec, dit, llm, src, added)
    except BaseException:
        if added:
            try:
                sys.path.remove(str(src))
            except ValueError:
                pass
        raise
