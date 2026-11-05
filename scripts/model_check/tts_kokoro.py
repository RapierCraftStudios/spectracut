# SPDX-License-Identifier: AGPL-3.0-or-later
"""Temp TTS candidate 1: Kokoro-82M (Apache-2.0, hexgrad/Kokoro-82M). Venv ``tts``.

Usage: ``run.ps1 tts tts_kokoro.py [cpu]``. Writes ``out/kokoro.wav`` (the reference clip the other
audio checks reuse) and one results row.
"""

from __future__ import annotations

import sys
import time

from common import measure, out_dir, setup_env

TEXT = (
    "Welcome to SpectraCut. This temporary narration lets us check timing before the final "
    "voice over is recorded. Every claim on screen traces back to the brief."
)


def main() -> None:
    setup_env()
    import numpy as np
    import soundfile as sf
    from kokoro import KPipeline

    dev = "cpu" if sys.argv[1:2] == ["cpu"] else "cuda"
    with measure("tts", "Kokoro-82M", f"voice=af_heart device={dev} chars={len(TEXT)}") as m:
        p = KPipeline(lang_code="a", repo_id="hexgrad/Kokoro-82M", device=dev)
        t = time.time()
        list(p(TEXT, voice="af_heart"))
        m["warmup_s"] = round(time.time() - t, 2)
        t = time.time()
        chunks = [a.numpy() if hasattr(a, "numpy") else a for _, _, a in p(TEXT, voice="af_heart")]
        audio = np.concatenate(chunks)
        gen = time.time() - t
        sf.write(str(out_dir() / "kokoro.wav"), audio, 24000)
        dur = len(audio) / 24000
        m.update(gen_s=round(gen, 2), audio_s=round(dur, 2), rtf=round(gen / dur, 3))


if __name__ == "__main__":
    main()
