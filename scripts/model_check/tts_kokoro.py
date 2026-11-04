import sys, time, soundfile as sf, numpy as np, torch
from common import measure, OUT
from kokoro import KPipeline
TEXT = ("Welcome to SpectraCut. This temporary narration lets us check timing before the final "
        "voice over is recorded. Every claim on screen traces back to the brief.")
dev = "cuda" if len(sys.argv) < 2 or sys.argv[1] != "cpu" else "cpu"
with measure("tts", "Kokoro-82M", f"voice=af_heart device={dev} chars={len(TEXT)}") as m:
    p = KPipeline(lang_code="a", repo_id="hexgrad/Kokoro-82M", device=dev)
    t = time.time(); list(p(TEXT, voice="af_heart")); m["extra"]["warmup_s"] = round(time.time() - t, 2)
    t = time.time()
    audio = np.concatenate([a.numpy() if hasattr(a, "numpy") else a for _, _, a in p(TEXT, voice="af_heart")])
    gen = time.time() - t
    sf.write(f"{OUT}/kokoro.wav", audio, 24000)
    m["extra"].update(gen_s=round(gen, 2), audio_s=round(len(audio) / 24000, 2), rtf=round(gen / (len(audio) / 24000), 3))
