"""ACE-Step trimmed checkpoints (turbo DiT + VAE + embedding + LM 0.6B; skips the 3.5 GB 1.7B LM). Run after setup_ace.ps1 cloned the repo."""
import os
from common import ROOT
from huggingface_hub import snapshot_download
ck = os.path.join(ROOT, "repos", "ace", "checkpoints"); os.makedirs(ck, exist_ok=True)
snapshot_download("ACE-Step/Ace-Step1.5", local_dir=ck, allow_patterns=["acestep-v15-turbo/*", "vae/*", "Qwen3-Embedding-0.6B/*", "config.json"])
snapshot_download("ACE-Step/acestep-5Hz-lm-0.6B", local_dir=os.path.join(ck, "acestep-5Hz-lm-0.6B"))
print("DONE ace checkpoints", flush=True)
