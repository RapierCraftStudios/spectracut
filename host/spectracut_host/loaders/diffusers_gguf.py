# SPDX-License-Identifier: AGPL-3.0-or-later
"""Z-Image-Turbo concept-image loader (family ``diffusers-gguf``).

The transformer is the Q4_K_M GGUF; the text encoder, VAE, scheduler and transformer
config come from the pinned ``Tongyi-MAI/Z-Image-Turbo`` copy that ``spectracut setup``
puts under the model root. Model CPU offload keeps the peak within the 8 GB card
(``scripts/model_check/image_diffusers.py`` measured it).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from spectracut.providers.base import ProviderError
from spectracut.providers.local.runtime import ModelSpec

from ._base import BaseSession, check_vram_spec, weights_dir

GGUF_REPO = "jayn7/Z-Image-Turbo-GGUF"
GGUF_FILE = "z_image_turbo-Q4_K_M.gguf"
BASE_REPO = "Tongyi-MAI/Z-Image-Turbo"
STEPS = 9
GUIDANCE = 0.0
SIZE = 1024
SEED = 42


def _dimension(options: dict[str, Any], key: str) -> int:
    value = options.get(key, SIZE)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0 or value % 16:
        raise ProviderError("local", f"{key} must be a positive multiple of 16, got {value!r}")
    return value


class ZImageSession(BaseSession):
    """A loaded Z-Image-Turbo pipeline; ``generate`` writes one PNG."""

    def __init__(self, spec: ModelSpec, pipe: Any, torch: Any) -> None:
        super().__init__(spec)
        self.pipe: Any = pipe
        self._torch = torch

    def _generate(self, prompt: str, out_path: Path, **options: Any) -> None:
        width, height = _dimension(options, "width"), _dimension(options, "height")
        steps = options.get("steps", STEPS)
        if isinstance(steps, bool) or not isinstance(steps, int) or steps <= 0:
            raise ProviderError("local", f"steps must be a positive integer, got {steps!r}")
        seed = int(options.get("seed", SEED))
        generator = self._torch.Generator("cpu").manual_seed(seed)
        image = self.pipe(
            prompt,
            height=height,
            width=width,
            num_inference_steps=steps,
            guidance_scale=GUIDANCE,
            generator=generator,
        ).images[0]
        # serve() writes to an extension-less path, so name the format.
        image.save(str(out_path), format="PNG")

    def _unload(self) -> None:
        self.pipe = None


def load(spec: ModelSpec) -> ZImageSession:
    check_vram_spec(spec)
    gguf = weights_dir(spec, GGUF_REPO) / GGUF_FILE
    if not gguf.is_file():
        raise ProviderError("local", f"{spec.id}: {gguf} is missing: run `spectracut setup`")
    base = weights_dir(spec, BASE_REPO)
    import diffusers
    import torch

    transformer = diffusers.ZImageTransformer2DModel.from_single_file(
        str(gguf),
        quantization_config=diffusers.GGUFQuantizationConfig(compute_dtype=torch.bfloat16),
        torch_dtype=torch.bfloat16,
        config=str(base),
        subfolder="transformer",
    )
    pipe = diffusers.ZImagePipeline.from_pretrained(
        str(base), transformer=transformer, torch_dtype=torch.bfloat16
    )
    pipe.enable_model_cpu_offload()
    return ZImageSession(spec, pipe, torch)
