# SPDX-License-Identifier: AGPL-3.0-or-later
"""SFX candidate (owner action first): Stable Audio Open Small / 1.0 (Stability AI Community
Licence). Both repos are gated: the owner must accept the licence on Hugging Face and set a read
token as ``HF_TOKEN`` in legion's environment. The token is never stored in the repo or results.

Usage: ``run.ps1 sfx sfx_stable_audio.py [small|1.0]`` (venv with ``stable-audio-tools``).
"""

from __future__ import annotations

import os
import sys
import time

from common import measure, out_dir, setup_env
from sfx_moss import PROMPTS

REPOS = {
    "small": ("stabilityai/stable-audio-open-small", 8, 1.0),
    "1.0": ("stabilityai/stable-audio-open-1.0", 100, 7.0),
}


def main() -> None:
    which = sys.argv[1] if len(sys.argv) > 1 else "small"
    if not os.environ.get("HF_TOKEN"):
        sys.exit("HF_TOKEN is not set: Stable Audio Open is gated (owner action, see the doc)")
    repo, steps, cfg_scale = REPOS[which]
    setup_env()
    import soundfile as sf
    import torch
    from stable_audio_tools import get_pretrained_model
    from stable_audio_tools.inference.generation import generate_diffusion_cond

    with measure("sfx", f"Stable Audio Open {which}", f"steps={steps} cfg={cfg_scale} 10 s") as m:
        t = time.time()
        model, cfg = get_pretrained_model(repo)
        model = model.to("cuda").half()
        sr, n = int(cfg["sample_rate"]), int(cfg["sample_size"])
        m["load_s"] = round(time.time() - t, 1)
        for i, p in enumerate(PROMPTS):
            t = time.time()
            cond = [{"prompt": p, "seconds_start": 0, "seconds_total": 10}]
            audio = generate_diffusion_cond(
                model, steps=steps, cfg_scale=cfg_scale, conditioning=cond, sample_size=n,
                device="cuda",
            )
            m[f"gen{i}_s"] = round(time.time() - t, 1)
            wav = audio[0].float().clamp(-1, 1)[:, : sr * 10].cpu().numpy().T
            sf.write(str(out_dir() / f"sao_{which}_{i}.wav"), wav, sr)
            torch.cuda.synchronize()


if __name__ == "__main__":
    main()
