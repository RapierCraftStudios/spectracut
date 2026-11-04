"""MOSS-SoundEffect (8B, Apache-2.0) with bitsandbytes 4-bit. Usage: run.ps1 sfx sfx_moss.py [nf4|bf16]"""
import sys, time, faulthandler, torch, soundfile as sf
faulthandler.enable()
from common import measure, OUT
from transformers import AutoModel, AutoProcessor, BitsAndBytesConfig
torch.backends.cuda.enable_cudnn_sdp(False)
mode = sys.argv[1] if len(sys.argv) > 1 else "nf4"
PROMPTS = ["Heavy wooden door creaking open slowly in a quiet stone hallway, then a soft thud.",
           "Short sci-fi UI whoosh with a bright digital chirp."]
from huggingface_hub import snapshot_download
# the custom processor does Path(repo_id), which turns "org/name" into "org\\name" on Windows -> use a local dir
repo = snapshot_download("OpenMOSS-Team/MOSS-SoundEffect")
with measure("sfx", "MOSS-SoundEffect 8B", f"mode={mode} max_new_tokens=1024") as m:
    t = time.time()
    proc = AutoProcessor.from_pretrained(repo, trust_remote_code=True)
    kw = dict(trust_remote_code=True, attn_implementation="sdpa")
    if mode == "nf4":
        kw["quantization_config"] = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_compute_dtype=torch.bfloat16)
        kw["device_map"] = {"": 0}
    else:
        kw["torch_dtype"] = torch.bfloat16
    model = AutoModel.from_pretrained(repo, **kw)
    if mode != "nf4": model = model.to("cuda")
    proc.audio_tokenizer = proc.audio_tokenizer.to("cuda")  # after the LM so the 4-bit load has the VRAM
    m["extra"]["vram_after_load_mib"] = torch.cuda.memory_allocated() // 2**20
    model.eval(); m["extra"]["load_s"] = round(time.time() - t, 1)
    for i, p in enumerate(PROMPTS):
        batch = proc([[proc.build_user_message(ambient_sound=p)]], mode="generation")
        t = time.time()
        with torch.no_grad():
            out = model.generate(input_ids=batch["input_ids"].to("cuda"), attention_mask=batch["attention_mask"].to("cuda"), max_new_tokens=1024)
        for msg in proc.decode(out):
            a = msg.audio_codes_list[0]
            sr = getattr(proc.model_config, "sampling_rate", 24000)
            sf.write(f"{OUT}/moss_sfx_{mode}_{i}.wav", a.float().cpu().numpy().T if a.dim() > 1 else a.float().cpu().numpy(), sr)
            m["extra"][f"gen{i}_audio_s"] = round(a.shape[-1] / sr, 2)
        m["extra"][f"gen{i}_s"] = round(time.time() - t, 1)
