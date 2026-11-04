"""ACE-Step 1.5 instrumental cue. Run with the repo venv: run.ps1 ace music_ace.py [lm|nolm] [offload]
Needs cwd = repo (run via run_ace.ps1)."""
import os, sys, time, json
for _a in sys.argv:  # cap8 = emulate an 8 GB card on the 12 GB one
    if _a.startswith("cap"): os.environ["SC_VRAM_CAP_GB"] = _a[3:]
from common import measure, OUT
repo = os.path.join(os.environ["SC_MODELS"], "repos", "ace")
sys.path.insert(0, repo)
from acestep.handler import AceStepHandler
from acestep.llm_inference import LLMHandler
from acestep.inference import GenerationParams, GenerationConfig, generate_music
use_lm = "nolm" not in sys.argv
offload = "offload" in sys.argv
CAPTION = "Cinematic tech product reveal, warm analog synth pads, soft pulsing bass, light percussion, building confident and optimistic, instrumental"
DUR = 30
with measure("music", "ACE-Step-1.5 turbo" + (" +LM0.6B" if use_lm else " DiT-only"),
             f"dur={DUR}s steps=8 offload={offload} lm={use_lm} cap={os.environ.get('SC_VRAM_CAP_GB')}") as m:
    t = time.time()
    dit = AceStepHandler()
    st, ok = dit.initialize_service(project_root=repo, config_path="acestep-v15-turbo", device="auto", offload_to_cpu=offload)
    assert ok, st
    llm = LLMHandler()
    if use_lm:
        st, ok = llm.initialize(checkpoint_dir=os.path.join(repo, "checkpoints"), lm_model_path="acestep-5Hz-lm-0.6B",
                                backend="pt", device="auto", offload_to_cpu=offload, dtype=None)
        assert ok, st
    m["extra"]["load_s"] = round(time.time() - t, 1)
    for i in range(2):  # 1st = warm-up, 2nd = steady state
        p = GenerationParams(task_type="text2music", thinking=use_lm, caption=CAPTION, lyrics="[Instrumental]",
                             duration=DUR, inference_steps=8, guidance_scale=1.0, seed=7 + i, vocal_language="unknown")
        t = time.time()
        r = generate_music(dit, llm, params=p, config=GenerationConfig(batch_size=1, audio_format="wav"), save_dir=OUT)
        m["extra"][f"gen{i}_s"] = round(time.time() - t, 1)
        m["extra"][f"gen{i}_ok"] = bool(r.success)
        if r.success: m["extra"][f"gen{i}_path"] = r.audios[0].get("path")
        else: m["extra"][f"gen{i}_err"] = str(r.status_message)[:200]
