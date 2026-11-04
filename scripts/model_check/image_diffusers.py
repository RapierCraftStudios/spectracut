"""Text-to-image via diffusers. Usage: run.ps1 img image_diffusers.py <zimage|flux2klein> <full|offload|seq|gguf-Q4_K_M> """
import sys, time, torch
from common import measure, OUT
which, mode = sys.argv[1], sys.argv[2]
PROMPT = ("A clean product hero shot of a brushed-aluminium smartwatch on a dark slate surface, "
          "soft rim light, shallow depth of field, crisp readable UI on the watch face showing '72 bpm', "
          "cinematic, 35mm")
H = W = 1024
if which == "zimage":
    from diffusers import ZImagePipeline as P
    repo, steps, guid = "Tongyi-MAI/Z-Image-Turbo", 9, 0.0
else:
    from diffusers import Flux2KleinPipeline as P
    repo, steps, guid = "black-forest-labs/FLUX.2-klein-4B", 4, 1.0
with measure("image", f"{which} ({repo})", f"{W}x{H} steps={steps} mode={mode}") as m:
    t = time.time()
    kw = dict(torch_dtype=torch.bfloat16)
    if mode.startswith("gguf"):
        from diffusers import GGUFQuantizationConfig, ZImageTransformer2DModel
        q = mode.split("-", 1)[1]
        gguf_repo = {"zimage": "jayn7/Z-Image-Turbo-GGUF"}[which]
        from huggingface_hub import hf_hub_download
        gf = hf_hub_download(gguf_repo, f"z_image_turbo-{q}.gguf")
        tr = ZImageTransformer2DModel.from_single_file(
            gf,
            quantization_config=GGUFQuantizationConfig(compute_dtype=torch.bfloat16), torch_dtype=torch.bfloat16)
        kw["transformer"] = tr
    pipe = P.from_pretrained(repo, **kw)
    if mode == "full": pipe.to("cuda")
    elif mode == "seq": pipe.enable_sequential_cpu_offload()
    else: pipe.enable_model_cpu_offload()
    m["extra"]["load_s"] = round(time.time() - t, 1)
    for i in range(2):
        t = time.time()
        img = pipe(PROMPT, height=H, width=W, num_inference_steps=steps, guidance_scale=guid,
                   generator=torch.Generator("cpu").manual_seed(42)).images[0]
        m["extra"][f"gen{i}_s"] = round(time.time() - t, 1)
    img.save(f"{OUT}/{which}_{mode}.png")
    m["extra"]["torch_peak_alloc_mib"] = round(torch.cuda.max_memory_allocated() / 2**20)
