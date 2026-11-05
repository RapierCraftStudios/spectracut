# SPDX-License-Identifier: AGPL-3.0-or-later
"""Music candidate 1: ACE-Step 1.5 turbo (MIT), 30 s instrumental cue. Venv ``ace`` (repo uv sync).

Usage: ``run_repo.ps1 ace ace music_ace.py [lm|nolm] [offload]``. Checkpoints: ``dl_ace.py``.
"""

from __future__ import annotations

import sys
import time

from common import measure, models_root, out_dir, setup_env

CAPTION = (
    "Cinematic tech product reveal, warm analog synth pads, soft pulsing bass, light percussion, "
    "building confident and optimistic, instrumental"
)
DUR = 30


def main() -> None:
    use_lm = "nolm" not in sys.argv
    offload = "offload" in sys.argv
    setup_env()
    repo = models_root() / "repos" / "ace"
    sys.path.insert(0, str(repo))
    from acestep.handler import AceStepHandler
    from acestep.inference import GenerationConfig, GenerationParams, generate_music
    from acestep.llm_inference import LLMHandler

    name = "ACE-Step-1.5 turbo" + (" +LM0.6B" if use_lm else " DiT-only")
    with measure("music", name, f"dur={DUR}s steps=8 offload={offload} lm={use_lm}") as m:
        t = time.time()
        dit = AceStepHandler()
        st, ok = dit.initialize_service(
            project_root=str(repo),
            config_path="acestep-v15-turbo",
            device="auto",
            offload_to_cpu=offload,
        )
        if not ok:
            raise RuntimeError(st)
        llm = LLMHandler()
        if use_lm:
            st, ok = llm.initialize(
                checkpoint_dir=str(repo / "checkpoints"),
                lm_model_path="acestep-5Hz-lm-0.6B",
                backend="pt",
                device="auto",
                offload_to_cpu=offload,
                dtype=None,
            )
            if not ok:
                raise RuntimeError(st)
        m["load_s"] = round(time.time() - t, 1)
        for i in range(2):  # 0 = warm-up, 1 = steady state
            p = GenerationParams(
                task_type="text2music",
                thinking=use_lm,
                caption=CAPTION,
                lyrics="[Instrumental]",
                duration=DUR,
                inference_steps=8,
                guidance_scale=1.0,
                seed=7 + i,
                vocal_language="unknown",
            )
            t = time.time()
            cfg = GenerationConfig(batch_size=1, audio_format="wav")
            r = generate_music(dit, llm, params=p, config=cfg, save_dir=str(out_dir()))
            m[f"gen{i}_s"] = round(time.time() - t, 1)
            m[f"gen{i}_ok"] = bool(r.success)
            m[f"gen{i}_path"] = r.audios[0].get("path") if r.success else str(r.status_message)


if __name__ == "__main__":
    main()
