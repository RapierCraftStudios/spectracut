# SPDX-License-Identifier: AGPL-3.0-or-later
"""Temp TTS candidate 2: Qwen3-TTS-12Hz-0.6B-Base (Apache-2.0) voice clone. Venv ``qtts``.

Reference voice = ``out/kokoro.wav`` and its transcript (run ``tts_kokoro.py`` first).
Usage: ``run.ps1 qtts tts_qwen3.py``. attn=sdpa (no flash-attn on Windows).
"""

from __future__ import annotations

import time

from common import measure, out_dir, setup_env
from tts_kokoro import TEXT

MODEL = "Qwen/Qwen3-TTS-12Hz-0.6B-Base"


def main() -> None:
    setup_env()
    import soundfile as sf
    import torch
    from qwen_tts import Qwen3TTSModel

    ref = str(out_dir() / "kokoro.wav")
    with measure("tts", "Qwen3-TTS-12Hz-0.6B-Base", "voice-clone ref=kokoro.wav bf16 sdpa") as m:
        t = time.time()
        model = Qwen3TTSModel.from_pretrained(
            MODEL, device_map="cuda:0", dtype=torch.bfloat16, attn_implementation="sdpa"
        )
        m["load_s"] = round(time.time() - t, 1)
        for i in range(2):  # 0 = warm-up, 1 = steady state
            t = time.time()
            wavs, sr = model.generate_voice_clone(
                text=TEXT, language="English", ref_audio=ref, ref_text=TEXT
            )
            g = time.time() - t
            dur = len(wavs[0]) / sr
            m.update({f"gen{i}_s": round(g, 2), f"audio{i}_s": round(dur, 2)})
            m[f"rtf{i}"] = round(g / dur, 3)
        sf.write(str(out_dir() / "qwen3tts.wav"), wavs[0], sr)


if __name__ == "__main__":
    main()
