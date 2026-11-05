# SPDX-License-Identifier: AGPL-3.0-or-later
"""SFX candidate: MOSS-SoundEffect 8B (Apache-2.0, OpenMOSS-Team/MOSS-SoundEffect). Venv ``sfx``.

Usage: ``run.ps1 sfx sfx_moss.py [nf4|nf4-offload]``. ``nf4-offload`` keeps the audio tokenizer on
the CPU until the language model has finished generating, so peak VRAM is the 4-bit LM plus its
KV cache (the 8 GB legion path). Needs ``transformers==5.0.0`` (later releases break the
remote-code processor). Windows quirks: load from a local snapshot directory because the processor
does ``Path(repo_id)``; write wavs with soundfile (torchaudio.save needs torchcodec).
"""

from __future__ import annotations

import sys
import time
from typing import Any

from common import measure, out_dir, setup_env

PROMPTS = [
    "Heavy wooden door creaking open slowly in a quiet stone hallway, then a soft thud.",
    "Short sci-fi UI whoosh with a bright digital chirp.",
]


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "nf4-offload"
    setup_env()
    import soundfile as sf
    import torch
    from huggingface_hub import snapshot_download
    from transformers import AutoModel, AutoProcessor, BitsAndBytesConfig

    torch.backends.cuda.enable_cudnn_sdp(False)
    repo = snapshot_download("OpenMOSS-Team/MOSS-SoundEffect")
    offload = mode.endswith("offload")
    with measure("sfx", "MOSS-SoundEffect 8B", f"mode={mode} max_new_tokens=1024") as m:
        t = time.time()
        proc = AutoProcessor.from_pretrained(repo, trust_remote_code=True)
        qc = BitsAndBytesConfig(
            load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_compute_dtype=torch.bfloat16
        )
        kw: dict[str, Any] = {
            "trust_remote_code": True,
            "attn_implementation": "sdpa",
            "quantization_config": qc,
            "device_map": {"": 0},
        }
        model = AutoModel.from_pretrained(repo, **kw).eval()
        if not offload:
            proc.audio_tokenizer = proc.audio_tokenizer.to("cuda")
        m["vram_after_load_mib"] = torch.cuda.memory_allocated() // 2**20
        m["load_s"] = round(time.time() - t, 1)
        sr = int(getattr(proc.model_config, "sampling_rate", 24000))
        for i, p in enumerate(PROMPTS):
            batch = proc([[proc.build_user_message(ambient_sound=p)]], mode="generation")
            t = time.time()
            with torch.no_grad():
                out = model.generate(
                    input_ids=batch["input_ids"].to("cuda"),
                    attention_mask=batch["attention_mask"].to("cuda"),
                    max_new_tokens=1024,
                )
                if offload:
                    torch.cuda.empty_cache()
                    proc.audio_tokenizer = proc.audio_tokenizer.to("cuda")
                msgs = proc.decode(out)
                if offload:
                    proc.audio_tokenizer = proc.audio_tokenizer.to("cpu")
            for msg in msgs:
                a = msg.audio_codes_list[0].float().cpu().numpy()
                sf.write(str(out_dir() / f"moss_{mode}_{i}.wav"), a.T if a.ndim > 1 else a, sr)
                m[f"gen{i}_audio_s"] = round(a.shape[-1] / sr, 2)
            m[f"gen{i}_s"] = round(time.time() - t, 1)


if __name__ == "__main__":
    main()
