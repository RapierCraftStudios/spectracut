# SPDX-License-Identifier: AGPL-3.0-or-later
"""Image diffusion candidates through diffusers, one product hero still at 1024x1024.

Usage: ``run.ps1 img image_diffusers.py <zimage|flux2klein> <offload|seq|gguf-Q4_K_M|...>``.

* ``zimage``: Tongyi-MAI/Z-Image-Turbo (Apache-2.0), transformer from ``jayn7/Z-Image-Turbo-GGUF``.
* ``flux2klein``: black-forest-labs/FLUX.2-klein-4B (Apache-2.0), transformer from
  ``unsloth/FLUX.2-klein-4B-GGUF``.

``gguf-<quant>`` loads only the transformer from GGUF (text encoder bf16, model CPU offload).
"""

from __future__ import annotations

import sys
import time
from typing import Any

from common import measure, out_dir, setup_env

PROMPT = (
    "A clean product hero shot of a brushed-aluminium smartwatch on a dark slate surface, "
    "soft rim light, shallow depth of field, crisp readable UI on the watch face showing "
    "'72 bpm', cinematic, 35mm"
)
SIZE = 1024
MODELS: dict[str, dict[str, Any]] = {
    "zimage": {
        "repo": "Tongyi-MAI/Z-Image-Turbo",
        "pipeline": "ZImagePipeline",
        "transformer": "ZImageTransformer2DModel",
        "gguf_repo": "jayn7/Z-Image-Turbo-GGUF",
        "gguf_file": "z_image_turbo-{q}.gguf",
        "steps": 9,
        "guidance": 0.0,
    },
    "flux2klein": {
        "repo": "black-forest-labs/FLUX.2-klein-4B",
        "pipeline": "Flux2KleinPipeline",
        "transformer": "Flux2Transformer2DModel",
        "gguf_repo": "unsloth/FLUX.2-klein-4B-GGUF",
        "gguf_file": "flux-2-klein-4b-{q}.gguf",
        "steps": 4,
        "guidance": 1.0,
    },
}


def main() -> None:
    which, mode = sys.argv[1], sys.argv[2]
    cfg = MODELS[which]
    setup_env()
    import diffusers
    import torch
    from huggingface_hub import hf_hub_download

    settings = f"{SIZE}x{SIZE} steps={cfg['steps']} mode={mode}"
    with measure("image", f"{which} ({cfg['repo']})", settings) as m:
        t = time.time()
        kw: dict[str, Any] = {"torch_dtype": torch.bfloat16}
        if mode.startswith("gguf"):
            q = mode.split("-", 1)[1]
            gf = hf_hub_download(cfg["gguf_repo"], cfg["gguf_file"].format(q=q))
            tr_cls = getattr(diffusers, cfg["transformer"])
            qc = diffusers.GGUFQuantizationConfig(compute_dtype=torch.bfloat16)
            kw["transformer"] = tr_cls.from_single_file(
                gf,
                quantization_config=qc,
                torch_dtype=torch.bfloat16,
                config=cfg["repo"],
                subfolder="transformer",
            )
        pipe = getattr(diffusers, cfg["pipeline"]).from_pretrained(cfg["repo"], **kw)
        if mode == "seq":
            pipe.enable_sequential_cpu_offload()
        else:
            pipe.enable_model_cpu_offload()
        m["load_s"] = round(time.time() - t, 1)
        for i in range(2):  # 0 = warm-up, 1 = steady state
            t = time.time()
            img = pipe(
                PROMPT,
                height=SIZE,
                width=SIZE,
                num_inference_steps=cfg["steps"],
                guidance_scale=cfg["guidance"],
                generator=torch.Generator("cpu").manual_seed(42),
            ).images[0]
            m[f"gen{i}_s"] = round(time.time() - t, 1)
        img.save(str(out_dir() / f"{which}_{mode}.png"))
        m["torch_peak_alloc_mib"] = round(torch.cuda.max_memory_allocated() / 2**20)


if __name__ == "__main__":
    main()
