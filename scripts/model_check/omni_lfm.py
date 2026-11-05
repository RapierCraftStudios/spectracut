# SPDX-License-Identifier: AGPL-3.0-or-later
"""Omni critic candidate 2: LFM3-Omni-2B (LFM Open License v1.0) listening to a clip, text out.

Venv ``omni`` (transformers>=5.20.2, librosa, soundfile). Usage: ``run.ps1 omni omni_lfm.py [wav ...]``
(default: ``out/kokoro.wav`` and ``out/ace_lm.wav``, the clips Qwen2.5-Omni was asked about).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from common import measure, out_dir, setup_env

PROMPT = (
    "You are a sound editor. Listen to this clip and describe in 2 sentences what you hear, then "
    "rate its technical quality 1-5 and list any defects (clipping, noise, glitches, silence)."
)
MODEL = "LiquidAI/LFM3-Omni-2B"


def main() -> None:
    clips = sys.argv[1:] or [str(out_dir() / "kokoro.wav"), str(out_dir() / "ace_lm.wav")]
    setup_env()
    import torch
    from transformers import AutoModelForMultimodalLM, AutoProcessor

    with measure("omni", "LFM3-Omni-2B", "bf16 text out max_new_tokens=200") as m:
        t = time.time()
        model = AutoModelForMultimodalLM.from_pretrained(
            MODEL, dtype=torch.bfloat16, device_map="cuda", trust_remote_code=True
        ).eval()
        proc = AutoProcessor.from_pretrained(MODEL, trust_remote_code=True)
        m["load_s"] = round(time.time() - t, 1)
        for c in clips:
            user = [{"type": "audio", "audio": c}, {"type": "text", "text": PROMPT}]
            conv = [
                {"role": "system", "content": "Respond with text."},
                {"role": "user", "content": user},
            ]
            inp = proc.apply_chat_template(
                conv,
                add_generation_prompt=True,
                tokenize=True,
                return_dict=True,
                return_tensors="pt",
            ).to("cuda")
            t = time.time()
            with torch.no_grad():
                out = model.generate(**inp, max_new_tokens=200, generation_mode="text")
            start = inp["input_ids"].shape[1]
            seq = out.sequences[0, start : out.sequence_lengths[0]]
            txt = proc.decode(seq, skip_special_tokens=True)
            m[Path(c).name] = {"s": round(time.time() - t, 1), "answer": txt.strip()[:400]}


if __name__ == "__main__":
    main()
