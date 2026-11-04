# Local model selection (issue #49)

Measured 2026-11-04 on the owner's two render hosts. Every AI job except Claude, ChatGPT/Codex and ElevenLabs
(final VO) runs on these machines, so each model was run for real with the scripts in `scripts/model_check/`.
Raw numbers come from `out\results.jsonl` on each host (`scripts/model_check/collect_results.py` pulls and tabulates them).

## Status in one paragraph

Measured end to end: temp TTS, music, image, ASR, CLAP, omni critic (Qwen2.5-Omni-7B), and one sound-effects model.
Not measured (reasons at the bottom): Stable Audio Open (gated, owner action), video-to-audio foley, image-to-3D,
motion, and most second/third candidates. The acceptance goal of 2-4 measured candidates per job is **not met**; each
job below states how many candidates were actually measured. Two hard constraints shaped the run: both hosts are on
slow Wi-Fi (about 1-4 MB/s sustained, so a 17 GB model is 1-4 hours), and from mid-run the owner capped
`mrdubey-1` at **4 GB of system RAM** (see Constraints).

## Hosts and constraints

| Host | GPU | RAM | Notes |
|---|---|---|---|
| `mrdubey-1` | RTX 4070 Ti 12 GB (the owner's desktop already holds 1-4 GB of VRAM; a 3.6 GB baseline was seen) | 31 GB | `artac-remote` is not admin. Everything below works without admin. Peak VRAM is reported **net of that baseline**. |
| `legion` | RTX 3070 Laptop 8 GB | 34 GB | Elevated user. Models live in `C:\spectracut\models`. |

- **RAM rule on `mrdubey-1` (owner, mid-run): at most 4 GB of system RAM, GPU is fine, no CPU/RAM offload.**
  `scripts/model_check/common.py` records `peak_ram_mib` and `run.ps1`/`run_repo.ps1` set `SC_RAM_LIMIT_GB=3.8` on that host,
  which kills the test process when exceeded. RAM-heavy measurements were moved to `legion`.
  Re-running under the rule killed ACE-Step (DiT-only and +LM) and Z-Image-Turbo at the limit while loading
  (peak > 3.9 GB), so their `mrdubey-1` 12 GB numbers below were taken **before** the rule and are valid for VRAM and speed only.
  A bare torch+CUDA process is already about 2.2 GB (Kokoro measured 2.2 GB peak).
- **8 GB card emulation.** Where a download to `legion` was too slow, the 12 GB card was capped to 8 GiB
  (`SC_VRAM_CAP_GB=8` -> `torch.cuda.set_per_process_memory_fraction`). A run that completes under the cap fits an 8 GB card
  apart from about 0.3-0.5 GB of CUDA context. Rows marked "emulated" are this. Real 8 GB measurements are marked "real".
- Disk: `legion` was kept above 20 GB free; its Z-Image weights (16 GB) were deleted after measuring.

## Measured candidates per job

Speed is steady-state (second run) unless noted; the first run includes warm-up/compile. "VRAM" is peak above the
pre-run baseline.

### Temp TTS (job 4) - 1 candidate measured

| Model | Licence | Weights | Card | VRAM | Speed | Notes |
|---|---|---|---|---|---|---|
| Kokoro-82M, voice `af_heart` | Apache-2.0 | `hexgrad/Kokoro-82M` | 4070 Ti 12 GB (real) | 1.1-1.4 GB | 10.7 s of audio in 0.16-0.18 s (RTF 0.015) | RAM peak 2.2 GB. faster-whisper round-trip transcript was word-exact (25 words, 0.0-10.04 s) |
| Kokoro-82M | | | 3070 8 GB (real) | 1.06 GB | 0.53 s (RTF 0.05) | |

Not measured: Qwen3-TTS-0.6B, Chatterbox-Turbo (not needed to pick; Kokoro is far faster than real time).

### Music (job 3) - 1 candidate measured (two configurations)

30 s instrumental cue, caption "cinematic tech product reveal, warm analog synth pads ...", 8 steps, `acestep-v15-turbo`.

| Config | Card | VRAM | Time per 30 s | Notes |
|---|---|---|---|---|
| DiT only (no LM) | 12 GB (real, before RAM rule) | 7.2 GB | 2.1 s (first 6.9 s) | model load 53 s |
| DiT + 5Hz LM 0.6B (`thinking`) | 12 GB (real, before RAM rule) | 8.6 GB | 24.3 s (first 26 s) | load 61 s |
| DiT only, CPU offload, 8 GiB cap | 8 GB (emulated) | 5.9 GB | 4.7 s | |
| DiT + LM 0.6B, offload, 8 GiB cap | 8 GB (emulated) | 6.6 GB | 34.5 s | |

Output: 48 kHz stereo WAV, exactly 30.0 s, no clipping (peak 0.74-0.87). Spectral roll-off (95 % energy) was 440-660 Hz for
DiT-only and 600 Hz / 5.4 kHz for the two LM runs, so the DiT-only cue is bass-heavy; I could not listen, so
musical quality is **unverified by ear**. Windows route: `git clone ACE-Step-1.5` + `uv sync` (pins torch 2.7.1+cu128); the
0.6B LM is a separate download (`acestep-download --model acestep-5Hz-lm-0.6B`; the main repo ships the 1.7B LM).
Needs more than 4 GB RAM while loading, so it must run on `legion` (or another host) under the owner's rule.
Not measured: HeartMuLa-oss-3B.

### Image (job 6) - 1 candidate measured (two quantisations)

Prompt: product hero shot of a brushed-aluminium smartwatch on slate, UI text "72 bpm", 1024x1024, 9 steps, guidance 0,
`diffusers` `ZImagePipeline`, transformer from GGUF, bf16 Qwen3-4B text encoder, model CPU offload.

| Config | Card | VRAM | Time | Notes |
|---|---|---|---|---|
| Z-Image-Turbo GGUF Q8_0 (`jayn7/Z-Image-Turbo-GGUF`) | 12 GB (real, before RAM rule) | 8.4 GB (torch allocator peak 7.9 GB) | 21.8 s (first 69 s) | load 532 s on Wi-Fi |
| Z-Image-Turbo GGUF Q4_K_M | 8 GB (real, `legion`) | 8.0 GB (the whole card; torch peak 7.9 GB) | 37 s | fits but with no headroom; the text encoder is the peak |

Quality (looked at with the image viewer): sharp, well lit, correct materials, the on-screen text reads "72" and "bpm"
(one stray extra "bpm" label). Good enough for hero stills. The model reproduces a real-world product silhouette from a generic
prompt, so briefs must stay brand-neutral and QA must still check frames.
Under the 4 GB rule on `mrdubey-1` this pipeline (offload path) exceeds the limit; keeping it fully on the 12 GB GPU needs a
quantised text encoder, **not tested**. Run image jobs on `legion` or relax the cap.
Not measured: FLUX.2 klein 4B (22 GB download).

### Sound effects (job 1) - 1 candidate measured, the preferred one is gated

| Model | Licence | Status |
|---|---|---|
| Stable Audio Open 1.0 | Stability AI Community | **Gated** (`gated: auto`): needs HF token + licence acceptance (owner action). Not measured. |
| Stable Audio Open Small | Stability AI Community | Also gated, same action. Not measured. |
| MOSS-SoundEffect 8B (`OpenMOSS-Team/MOSS-SoundEffect`) | Apache-2.0 | Measured below. |

MOSS-SoundEffect, bitsandbytes NF4, `transformers==5.0.0` (5.22 breaks the remote-code processor), 1024 new tokens:

| Card | VRAM | Time per clip | Output |
|---|---|---|---|
| 4070 Ti 12 GB (real, before RAM rule) | 10.95 GB (does **not** fit 8 GB) | 98-107 s | 10.1 s, 24 kHz mono; clips are low level (RMS 0.03) |

Observed system RAM about 7 GB while loading, so it also breaks the `mrdubey-1` rule. Windows quirks: the processor does
`Path(repo_id)`, which turns `org/name` into `org\name`, so load from a local snapshot directory; `torchaudio.save` needs
torchcodec, so write with `soundfile`. Verdict: usable as a fallback on the 12 GB card only, slow, and quality was not
judged by ear. Not measured: any second candidate.

### Omni audio critic (job 5) - 1 candidate measured, 1 pending

llama.cpp `b12110` `llama-mtmd-cli`, CUDA 12.4 build (legion driver 581.80 is CUDA 13.0, so the 13.4 build finds no device).
Prompt: describe the clip in 2 sentences, rate quality 1-5, list defects.

| Model | Licence | Card | VRAM | Time | Answer quality |
|---|---|---|---|---|---|
| Qwen2.5-Omni-7B Q4_K_M + mmproj Q8_0 (`ggml-org/Qwen2.5-Omni-7B-GGUF`) | Apache-2.0 | 3070 8 GB (real) | 7.25 GB (all layers on GPU, ctx 8192) | 12 s per clip incl. load | Kokoro clip: "synthesised female voice ... high quality" (correct, but it did not hear the words); ACE clip: "melodic electronic tune with synth pattern and steady drum beat ... 4/5" (plausible). Coarse critic, cannot be trusted for defect spotting alone |
| Qwen3-Omni-30B-A3B Q4_K_M with expert offload | Apache-2.0 | | | | QWEN3_RESULT_PLACEHOLDER |

RAM peak 4.7 GB on `legion`. Needs more than the `mrdubey-1` 4 GB cap once the model is memory-mapped, so run it on `legion`.

### ASR / alignment and CLAP (one-liners)

| Model | Licence | Card | VRAM | Time | Result |
|---|---|---|---|---|---|
| faster-whisper `large-v3-turbo`, fp16, word timestamps | MIT | 4070 Ti (real) | 2.3 GB | 0.34 s for 10.7 s of audio (first call 58 s incl. CUDA init) | exact transcript, 25 word-level timestamps |
| `laion/larger_clap_general` | Apache-2.0 | 4070 Ti (real) | 0.9 GB | 0.8 s | zero-shot on a TTS clip with captions {speech, explosion, piano}: picked "piano music" at 0.96. Treat CLAP as a similarity score between candidate SFX and their prompt, not as a classifier of speech |

Install: `uv pip install faster-whisper nvidia-cublas-cu12 nvidia-cudnn-cu12 transformers librosa`; the NVIDIA DLL directories under
`site-packages\nvidia\*\bin` must be added with `os.add_dll_directory` (done in `asr_clap.py`). Not tested on the 8 GB card (the
models are small enough that it will fit).

## Picks

| Job | Pick | Fallback | VRAM | 8 GB? | 12 GB? | Speed | Install route | Licence |
|---|---|---|---|---|---|---|---|---|
| Temp TTS | Kokoro-82M | Qwen3-TTS-0.6B / Chatterbox-Turbo (not measured) | 1.1 GB | yes (real) | yes (real) | RTF 0.015-0.05 | uv venv, `kokoro` + spaCy `en_core_web_sm` wheel | Apache-2.0 |
| Music | ACE-Step 1.5 turbo (DiT, add LM 0.6B for structure) | HeartMuLa-oss-3B (not measured) | 7.2 GB DiT / 8.6 GB +LM; 5.9 / 6.6 GB with offload | yes with offload (emulated) | yes (real) | 2-7 s DiT, 24-35 s +LM per 30 s | repo clone + `uv sync`, Windows native | MIT |
| Image | Z-Image-Turbo, GGUF Q8_0 on 12 GB, Q4_K_M on 8 GB | FLUX.2 klein 4B (not measured) | 8.0-8.4 GB with offload | yes, no headroom (real) | yes (real) | 22 s (12 GB), 37 s (8 GB) per 1024^2 | `diffusers` + `gguf`, Windows native | Apache-2.0 |
| SFX | Stable Audio Open 1.0 (owner action: HF token + licence acceptance), not measured | MOSS-SoundEffect 8B NF4 (measured) | 11 GB (MOSS) | no (MOSS) | yes, barely (MOSS) | about 100 s per 10 s clip (MOSS) | `transformers==5.0.0` + bitsandbytes, local snapshot path | Stability AI Community / Apache-2.0 |
| Omni critic | Qwen2.5-Omni-7B Q4_K_M via llama.cpp | Qwen3-Omni-30B-A3B (see row above) | 7.25 GB | yes, tight (real) | yes | 12 s per clip | llama.cpp prebuilt CUDA 12.4 zip + two GGUF files | Apache-2.0 |
| ASR / alignment | faster-whisper large-v3-turbo | | 2.3 GB | expected | yes (real) | 0.34 s per 10 s | uv + CUDA DLL dirs | MIT |
| CLAP | laion/larger_clap_general | | 0.9 GB | expected | yes (real) | 0.8 s | transformers | Apache-2.0 |
| Video-to-audio foley | not measured | | | | | | | HunyuanVideo-Foley XL: Tencent community; ThinkSound: Apache-2.0 |
| Image-to-3D | not measured | | | | | | | Pixal3D: MIT; Hunyuan3D-2.1: Tencent community |
| Motion | not measured | | | | | | | Kimodo: Apache-2.0 code / NVIDIA Open Model weights |

## Owner actions

1. Stable Audio Open 1.0 and Small: log in to Hugging Face, accept the licence on both model pages and create a read token;
   put it in the environment of `legion` (never in a repo). Then run `scripts/model_check/` SFX measurements for it.
2. Decide the RAM policy for `mrdubey-1`. Under the 4 GB cap only the small models (Kokoro, Whisper, CLAP) run there;
   ACE-Step, Z-Image-Turbo, MOSS and any offload model were killed at the limit during loading. Either keep GPU jobs on
   `legion` (8 GB; Z-Image fits with no headroom) or allow a higher cap for render jobs.
3. `mrdubey-1` Windows quirks to remember when enrolling it: symlinks are untraversable for this user (HF cache is forced to
   copy mode in `common.py`; uv's Python junction is removed by `setup_env.ps1`), and `Get-CimInstance` is denied.
4. The 3 GB of VRAM the desktop already uses on `mrdubey-1` leaves about 8.5-9 GB for jobs there.

## Not measured, and why

| Item | Reason |
|---|---|
| Stable Audio Open 1.0 / Small | gated, owner action above |
| Video-to-audio foley (HunyuanVideo-Foley XL, ThinkSound) | 17 GB+ downloads at 1-4 MB/s plus an offload path that the 4 GB RAM rule forbids on `mrdubey-1`; ran out of time budget |
| Image-to-3D (Pixal3D, Hunyuan3D-2.1) | same: large downloads, then the RAM rule; lowest priority in the brief |
| Motion (Kimodo-SOMA-RP) | same; Windows support unverified |
| HeartMuLa, Chatterbox-Turbo, Qwen3-TTS-0.6B, FLUX.2 klein 4B, LFM3-Omni-2B | second candidates, skipped for time; the first pick for each job was measured first |
| Real 8 GB run of ACE-Step | 10 GB download to `legion` at about 1 MB/s; emulated with an 8 GiB cap instead |
| Real 12 GB re-runs under the 4 GB RAM rule | cap arrived after the runs; the re-run was killed at the limit, which is itself the finding |
| Listening tests | no audio output available; checked duration, level, clipping, spectrum, and an LLM/ASR round-trip only |

## Reproducing

```powershell
# on a host (SC_MODELS defaults to the parent of scripts\model_check; copy the folder there)
powershell -ExecutionPolicy Bypass -File model_check\setup_env.ps1 -Name tts -Packages kokoro,soundfile
powershell -ExecutionPolicy Bypass -File model_check\pipinstall.ps1 tts https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl
powershell -ExecutionPolicy Bypass -File model_check\run.ps1 tts tts_kokoro.py
```

From the dev box: `scripts/model_check/push.sh <host>` copies the scripts, `collect_results.py` pulls and tabulates the
JSONL. Scripts: `common.py` (VRAM/RAM sampling, 4 GB watchdog, 8 GiB cap), `setup_env.ps1`, `setup_ace.ps1`,
`setup_llamacpp.ps1`, `run.ps1`, `run_repo.ps1`, `tts_kokoro.py`, `music_ace.py`, `image_diffusers.py`, `sfx_moss.py`,
`asr_clap.py`, `omni_llama.ps1`, `hf_get.py`, `audio_stats.py`.
