"""Qwen3-TTS-12Hz-0.6B-Base voice clone (ref = the Kokoro sample + its transcript). Venv: tts + qwen-tts. attn=sdpa (no flash-attn on Windows)."""
import time, torch, soundfile as sf
from common import measure, OUT
TEXT = ("Welcome to SpectraCut. This temporary narration lets us check timing before the final "
        "voice over is recorded. Every claim on screen traces back to the brief.")
REF_TEXT = TEXT
with measure("tts", "Qwen3-TTS-12Hz-0.6B-Base", "voice-clone, ref=kokoro.wav, bf16, sdpa") as m:
    from qwen_tts import Qwen3TTSModel
    t = time.time()
    model = Qwen3TTSModel.from_pretrained("Qwen/Qwen3-TTS-12Hz-0.6B-Base", device_map="cuda:0", dtype=torch.bfloat16, attn_implementation="sdpa")
    m["extra"]["load_s"] = round(time.time() - t, 1)
    for i in range(2):
        t = time.time()
        wavs, sr = model.generate_voice_clone(text=TEXT, language="English", ref_audio=f"{OUT}/kokoro.wav", ref_text=REF_TEXT)
        g = time.time() - t; dur = len(wavs[0]) / sr
        m["extra"][f"gen{i}_s"] = round(g, 2); m["extra"][f"audio{i}_s"] = round(dur, 2); m["extra"][f"rtf{i}"] = round(g / dur, 3)
    sf.write(f"{OUT}/qwen3tts.wav", wavs[0], sr)
