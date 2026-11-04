"""Sequential background downloads (legion Wi-Fi is ~2 MB/s). Usage: run.ps1 tts dl_queue.py
Each entry: repo, allow_patterns (None = everything). Logs to out\\dl_queue.log."""
import time, sys
from common import ROOT
from huggingface_hub import snapshot_download
Q = [("Qwen/Qwen3-TTS-12Hz-0.6B-Base", None),
     ("LiquidAI/LFM3-Omni-2B", None),
     ("tencent/HunyuanVideo-Foley", ["hunyuanvideo_foley_xl.pth", "vae_128d_48k.pth", "synchformer_state_dict.pth", "*.json", "*.yaml", "*.md"]),
     ("black-forest-labs/FLUX.2-klein-4B", ["transformer/*", "text_encoder/*", "tokenizer/*", "vae/*", "scheduler/*", "model_index.json"])]
if len(sys.argv) > 1: Q = [q for q in Q if any(a in q[0] for a in sys.argv[1:])]
for repo, pat in Q:
    t = time.time()
    try:
        p = snapshot_download(repo, allow_patterns=pat); print("DONE", repo, p, round(time.time() - t), "s", flush=True)
    except Exception as e:
        print("FAIL", repo, repr(e)[:300], flush=True)
