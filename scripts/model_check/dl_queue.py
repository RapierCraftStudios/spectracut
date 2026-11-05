# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sequential downloads into the HF cache on legion (its link is about 2-3 MB/s).

Usage: ``run.ps1 tts dl_queue.py [name-filter ...]``; with filters only matching repos are fetched.
Gated repos (Stable Audio Open) need ``HF_TOKEN`` in the environment and are not in the queue.
"""

from __future__ import annotations

import sys
import time

from common import setup_env

QUEUE: list[tuple[str, list[str] | None]] = [
    ("Qwen/Qwen3-TTS-12Hz-0.6B-Base", None),
    ("LiquidAI/LFM3-Omni-2B", None),
    ("Systran/faster-whisper-large-v3-turbo", None),
    ("laion/larger_clap_general", ["*.json", "*.txt", "model.safetensors"]),
    ("OpenSound/EzAudio", ["ckpts/s3/ezaudio_s3_xl.pt", "ckpts/vae/*", "ckpts/*.yml"]),
    ("google/flan-t5-xl", ["*.json", "spiece.model", "*.safetensors"]),
    ("unsloth/FLUX.2-klein-4B-GGUF", ["flux-2-klein-4b-Q8_0.gguf"]),
    (
        "black-forest-labs/FLUX.2-klein-4B",
        [
            "text_encoder/*",
            "tokenizer/*",
            "vae/*",
            "scheduler/*",
            "transformer/config.json",
            "*.json",
        ],
    ),
    (
        "tencent/HunyuanVideo-Foley",
        ["hunyuanvideo_foley_xl.pth", "vae_128d_48k.pth", "synchformer_state_dict.pth", "*.yaml"],
    ),
    ("OpenMOSS-Team/MOSS-SoundEffect", None),
]


def select(queue: list[tuple[str, list[str] | None]], filters: list[str]) -> list[str]:
    return [repo for repo, _ in queue if not filters or any(f in repo for f in filters)]


def main() -> None:
    setup_env()
    from huggingface_hub import snapshot_download

    wanted = set(select(QUEUE, sys.argv[1:]))
    for repo, pat in QUEUE:
        if repo not in wanted:
            continue
        t = time.time()
        try:
            p = snapshot_download(repo, allow_patterns=pat)
            print("DONE", repo, p, round(time.time() - t), "s", flush=True)
        except Exception as e:  # keep going: one failed repo should not stop the queue
            print("FAIL", repo, repr(e)[:300], flush=True)


if __name__ == "__main__":
    main()
