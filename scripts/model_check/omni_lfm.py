"""LFM3-Omni-2B as listening critic (audio in -> text out). Venv: omni (transformers>=5.20.2, librosa, soundfile, torchcodec optional)."""
import sys, time, torch
from common import measure, OUT
from transformers import AutoModelForMultimodalLM, AutoProcessor
PROMPT = "You are a sound editor. Listen to this clip and describe in 2 sentences what you hear, then rate its technical quality 1-5 and list any defects (clipping, noise, glitches, silence)."
clips = sys.argv[1:] or [f"{OUT}/kokoro.wav", f"{OUT}/ace_lm.wav"]
mid = "LiquidAI/LFM3-Omni-2B"
with measure("omni", "LFM3-Omni-2B", "bf16, text out, max_new_tokens=200") as m:
    t = time.time()
    model = AutoModelForMultimodalLM.from_pretrained(mid, dtype=torch.bfloat16, device_map="cuda", trust_remote_code=True).eval()
    proc = AutoProcessor.from_pretrained(mid, trust_remote_code=True)
    m["extra"]["load_s"] = round(time.time() - t, 1)
    for c in clips:
        conv = [{"role": "system", "content": "Respond with text."},
                {"role": "user", "content": [{"type": "audio", "audio": c}, {"type": "text", "text": PROMPT}]}]
        inp = proc.apply_chat_template(conv, add_generation_prompt=True, tokenize=True, return_dict=True, return_tensors="pt").to("cuda")
        t = time.time()
        with torch.no_grad():
            out = model.generate(**inp, max_new_tokens=200, generation_mode="text")
        txt = proc.decode(out.sequences[0, inp["input_ids"].shape[1]:out.sequence_lengths[0]], skip_special_tokens=True)
        m["extra"][c.replace("\\", "/").split("/")[-1]] = {"s": round(time.time() - t, 1), "answer": txt.strip()[:400]}
