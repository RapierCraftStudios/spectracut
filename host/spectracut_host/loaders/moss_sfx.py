# SPDX-License-Identifier: AGPL-3.0-or-later
"""MOSS-SoundEffect 8B in NF4 (family ``moss-sfx``; precedent scripts/model_check/sfx_moss.py).

This is the measured ``nf4-offload`` path for the 8 GB model host. The language model is
loaded in 4-bit on the GPU. The audio tokenizer stays on the CPU and moves to the GPU only
to decode, after generation has finished, so peak VRAM is the 4-bit model plus its KV
cache. Model and processor load from the local snapshot ``spectracut setup`` installs
(the remote-code processor does ``Path(repo_id)``), never from the hub.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

from spectracut.providers.local.runtime import ModelSpec

from ._base import BaseSession, weights_dir
from .ezaudio import write_wav

MAX_NEW_TOKENS = 1024
"""The measured setting for 10 s clips (sfx_moss.py); output is fitted to ``duration_s``."""
DEFAULT_SR = 24000


class MossSfxSession(BaseSession):
    def __init__(self, spec: ModelSpec, model: Any, processor: Any) -> None:
        super().__init__(spec)
        self.model: Any = model
        self.processor: Any = processor

    def _generate(
        self, prompt: str, out_path: Path, duration_s: float = 10.0, seed: int = 0, **_: Any
    ) -> None:
        torch = importlib.import_module("torch")
        proc = self.processor
        torch.manual_seed(int(seed))
        batch = proc([[proc.build_user_message(ambient_sound=prompt)]], mode="generation")
        with torch.no_grad():
            out = self.model.generate(
                input_ids=batch["input_ids"].to("cuda"),
                attention_mask=batch["attention_mask"].to("cuda"),
                max_new_tokens=MAX_NEW_TOKENS,
            )
            torch.cuda.empty_cache()
            proc.audio_tokenizer = proc.audio_tokenizer.to("cuda")
            try:
                msgs = proc.decode(out)
            finally:
                proc.audio_tokenizer = proc.audio_tokenizer.to("cpu")
        audio = msgs[0].audio_codes_list[0].float().cpu().numpy()
        if audio.ndim > 1:
            audio = audio.T
        sr = int(getattr(proc.model_config, "sampling_rate", DEFAULT_SR))
        write_wav(out_path, audio, sr, duration_s)

    def _unload(self) -> None:
        self.model = None
        self.processor = None


def load(spec: ModelSpec) -> MossSfxSession:
    repo = str(weights_dir(spec))
    torch = importlib.import_module("torch")
    transformers = importlib.import_module("transformers")
    torch.backends.cuda.enable_cudnn_sdp(False)
    processor = transformers.AutoProcessor.from_pretrained(
        repo, trust_remote_code=True, local_files_only=True
    )
    quant = transformers.BitsAndBytesConfig(
        load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_compute_dtype=torch.bfloat16
    )
    model = transformers.AutoModel.from_pretrained(
        repo,
        trust_remote_code=True,
        local_files_only=True,
        attn_implementation="sdpa",
        quantization_config=quant,
        device_map={"": 0},
    ).eval()
    processor.audio_tokenizer = processor.audio_tokenizer.to("cpu")
    return MossSfxSession(spec, model, processor)
