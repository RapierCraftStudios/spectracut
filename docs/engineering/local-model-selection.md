# Local model selection (issue #49)

This doc picks one local model per AI job, plus a fallback. It covers sound effects, video-to-audio foley,
music, temp TTS, the omni audio critic and image diffusion. Claude, ChatGPT/Codex and ElevenLabs (final VO)
are the only external services (see [technical-spike.md](technical-spike.md) § Cost and external dependencies).
Every other job runs on the model host. Each model was run with the scripts in `scripts/model_check/`. Raw
numbers are rows in `C:\spectracut\models\out\results.jsonl` on `legion`; `scripts/model_check/collect_results.py`
fetches and tabulates them.

Only models whose output can be used commercially can be a pick or a fallback. Non-commercial weights
(CC-BY-NC and similar) and gated models that need an owner action are listed separately with the reason.

## Hosts

| Host | GPU | RAM | Role |
|---|---|---|---|
| `legion` | RTX 3070 Laptop, 8 GB (8192 MiB) | about 28 GB usable | **The only model host** (owner decision on #49, 2026-11-04). Models, venvs and weights live in `C:\spectracut\models`. |
| `mrdubey-1` | RTX 4070 Ti, 12 GB | capped at 4 GB for tests | Not a model host. Nothing is run there. Its 12 GB numbers come from the first pass (PR #120) and are **informational only**. |

- "Peak VRAM" is the peak of whole-card `memory.used` (sampled every 100 ms by `common.py`), minus the baseline before the run.
  `legion`'s baseline is 0 MiB.
- "Seconds per output" is the steady-state time (second run) for one output, without model load.
- Card column: `legion 8 GB` is a real measurement on `legion`. `12 GB (info)` is the first pass on `mrdubey-1`.
  `8 GB emulated` is a 12 GB card capped to 8 GiB with `SC_VRAM_CAP_GB=8`. `not measured` gives the blocker in the notes.
- `legion`'s internet link measured 0.5-1 MB/s on 2026-11-05, so a 10 GB checkpoint takes 3-6 hours. That limits
  how many candidates were measured; each job below says which runs are still open.

## Measured candidates per job

### Sound effects

| Model | Licence | Weights source | Card | Peak VRAM | Seconds per output | Quality notes |
|---|---|---|---|---|---|---|
| MOSS-SoundEffect 8B, NF4 | Apache-2.0 | `OpenMOSS-Team/MOSS-SoundEffect` | 12 GB (info) | 10.95 GB | 98-107 s per 10 s clip | 24 kHz mono, low level (RMS 0.03). Does not fit 8 GB as is; `sfx_moss.py nf4-offload` keeps the audio tokenizer on the CPU for the 8 GB path |
| MOSS-SoundEffect 8B, NF4 + tokenizer offload | Apache-2.0 | `OpenMOSS-Team/MOSS-SoundEffect` | not measured | - | - | 16 GB checkpoint not yet on `legion`; run `sfx_moss.py nf4-offload` |
| EzAudio-XL (s3_xl) + flan-t5-xl | MIT (weights), Apache-2.0 (T5) | `OpenSound/EzAudio`, `google/flan-t5-xl` | not measured | - | - | About 1 GB DiT plus a 3 GB fp16 T5 encoder, so it is expected to fit 8 GB; about 7 GB to download; run `sfx_ezaudio.py s3_xl` |

Rejected or blocked:

| Model | Licence | Reason |
|---|---|---|
| Stable Audio Open Small / 1.0 | Stability AI Community | Gated on Hugging Face. No token is available (`HF_TOKEN` is unset on `legion`). Owner action below; `sfx_stable_audio.py` is ready. Not a pick until measured |
| AudioLDM 2 | CC-BY-NC-SA 4.0 (weights) | Non-commercial |
| Tango 2 | CC-BY-NC-SA 4.0 (weights) | Non-commercial |

### Video-to-audio foley

| Model | Licence | Weights source | Card | Peak VRAM | Seconds per output | Quality notes |
|---|---|---|---|---|---|---|
| HunyuanVideo-Foley XL, `--enable_offload` | Tencent Hunyuan Community | `tencent/HunyuanVideo-Foley` | not measured | 8 GB (vendor figure with offload) | - | Venv `foley`, repo `repos\foley` and test clip `out\ball.mp4` are ready on `legion`; the XL checkpoint, VAE and Synchformer (about 8 GB) are not downloaded yet. Run through `run_cli.py foley ... -- python infer.py ...` |
| Text-to-audio SFX pick, layered and cut to picture | see Sound effects | see Sound effects | not measured | - | - | The no-video fallback: the timeline places SFX by hand at the cut points from the shot list. It needs no extra model |

Rejected or blocked:

| Model | Licence | Reason |
|---|---|---|
| MMAudio | CC-BY-NC 4.0 (weights) | Non-commercial |
| ThinkSound (`liuhuadai/ThinkSound`) | Apache-2.0 code, but the README says "Commercial use is NOT permitted" | Non-commercial |
| HunyuanVideo-Foley XXL | Tencent Hunyuan Community | Needs 12 GB even with offload (vendor figure), so it does not fit `legion` |

### Music

| Model | Licence | Weights source | Card | Peak VRAM | Seconds per output | Quality notes |
|---|---|---|---|---|---|---|
| ACE-Step 1.5 turbo, DiT only | MIT | `ACE-Step/Ace-Step1.5` | 12 GB (info) | 7.2 GB | 2.1 s per 30 s cue | 48 kHz stereo, exactly 30.0 s, peak 0.74-0.87, bass-heavy (95 % roll-off 440-660 Hz) |
| ACE-Step 1.5 turbo, DiT + LM 0.6B, CPU offload | MIT | `ACE-Step/Ace-Step1.5`, `ACE-Step/acestep-5Hz-lm-0.6B` | 8 GB emulated | 6.6 GB | 34.5 s per 30 s cue | The LM adds structure (roll-off up to 5.4 kHz). No real `legion` run yet: about 7 GB of checkpoints (`dl_ace.py`) |
| HeartMuLa-oss-3B, `--lazy_load true` | Apache-2.0 | `HeartMuLa/HeartMuLa-oss-3B-happy-new-year`, `HeartMuLa/HeartCodec-oss-20260123` | not measured | - | - | About 7 GB to download; run through `run_cli.py music ...` |

Rejected: MusicGen / AudioGen (CC-BY-NC 4.0 weights, non-commercial). Stable Audio Open (gated, see Sound effects).

### Temp TTS

| Model | Licence | Weights source | Card | Peak VRAM | Seconds per output | Quality notes |
|---|---|---|---|---|---|---|
| Kokoro-82M, voice `af_heart` | Apache-2.0 | `hexgrad/Kokoro-82M` | legion 8 GB | 1055 MiB | 0.37 s for 10.7 s of audio (RTF 0.034) | faster-whisper round trip gives a word-exact transcript (25 words). Measured again by `legion_smoke.sh` |
| Kokoro-82M | Apache-2.0 | `hexgrad/Kokoro-82M` | 12 GB (info) | 1.1-1.4 GB | 0.16-0.18 s (RTF 0.015) | Process RAM peak 2.2 GB |
| Qwen3-TTS-12Hz-0.6B-Base, voice clone, bf16 sdpa | Apache-2.0 | `Qwen/Qwen3-TTS-12Hz-0.6B-Base` | legion 8 GB | 6820 MiB | 98.6 s for 9.9 s of audio (RTF 9.9) | Clones the Kokoro reference voice; 2.6 GB process RAM. About 270 times slower than Kokoro on `legion` (sdpa, no flash-attn on Windows), so it is only a fallback for voice cloning |

Not measured: Chatterbox-Turbo (MIT); a third candidate is not needed, since Kokoro runs about 30 times faster than real time.

### Omni audio critic

Prompt: "describe the clip in 2 sentences, rate its technical quality 1-5, list defects", on the Kokoro clip and an
ACE-Step cue.

| Model | Licence | Weights source | Card | Peak VRAM | Seconds per output | Quality notes |
|---|---|---|---|---|---|---|
| Qwen2.5-Omni-7B Q4_K_M + mmproj Q8_0, llama.cpp `b12110` (CUDA 12.4 build) | Apache-2.0 | `ggml-org/Qwen2.5-Omni-7B-GGUF` | legion 8 GB | 7253 MiB | 11.9 s per clip including load | Heard "a synthesized female voice ... no defects" (correct, but it did not report the words) and "melodic electronic tune ... steady drum beat, 4/5" for the cue. A coarse critic: it has to be paired with the numeric QA gate, not trusted alone |
| LFM3-Omni-2B, bf16, text out | LFM Open License v1.0 (commercial use allowed below the licence's revenue threshold) | `LiquidAI/LFM3-Omni-2B` | LFM_CARD | LFM_VRAM | LFM_SECS | LFM_NOTES |

Not measured: Qwen3-Omni-30B-A3B Q4_K_M with `--n-cpu-moe` (Apache-2.0). The 18.5 GB download is too slow on `legion`'s link.

### Image

Prompt: product hero shot of a brushed-aluminium smartwatch on slate, with "72 bpm" on its screen, 1024x1024.

| Model | Licence | Weights source | Card | Peak VRAM | Seconds per output | Quality notes |
|---|---|---|---|---|---|---|
| Z-Image-Turbo, GGUF Q4_K_M transformer, bf16 text encoder, model CPU offload, 9 steps | Apache-2.0 | `Tongyi-MAI/Z-Image-Turbo`, `jayn7/Z-Image-Turbo-GGUF` | legion 8 GB | 8011 MiB (whole card; torch peak 7933 MiB) | 37.2 s per image | Sharp, correct materials, readable "72 bpm" (one stray extra "bpm" label). Fits with no headroom; the text encoder sets the peak |
| Z-Image-Turbo, GGUF Q8_0 | Apache-2.0 | `jayn7/Z-Image-Turbo-GGUF` | 12 GB (info) | 8.4 GB | 21.8 s per image | Same look |
| FLUX.2 klein 4B, GGUF Q8_0 transformer, model CPU offload, 4 steps | Apache-2.0 | `black-forest-labs/FLUX.2-klein-4B`, `unsloth/FLUX.2-klein-4B-GGUF` | not measured | - | - | About 12 GB to download (Qwen3-4B text encoder plus the GGUF); run `image_diffusers.py flux2klein gguf-Q8_0` |

Rejected: FLUX.1 [dev] and FLUX.2 [dev] (FLUX Non-Commercial licence).

### ASR / alignment and CLAP (supporting models)

| Model | Licence | Weights source | Card | Peak VRAM | Seconds per output | Result |
|---|---|---|---|---|---|---|
| faster-whisper `large-v3-turbo`, fp16, word timestamps | MIT | `Systran/faster-whisper-large-v3-turbo` | ASR_CARD | ASR_VRAM | ASR_SECS | ASR_NOTES |
| faster-whisper `large-v3-turbo` | MIT | same | 12 GB (info) | 2.3 GB | 0.34 s for 10.7 s of audio | Exact transcript, 25 word timestamps |
| `laion/larger_clap_general` | Apache-2.0 | `laion/larger_clap_general` | CLAP_CARD | CLAP_VRAM | CLAP_SECS | CLAP_NOTES |

## Picks

The pick and the fallback are commercial-safe for every job. Where the 8 GB fit has not been measured on `legion` yet,
the VRAM cell says so.

| Job | Pick | Fallback | Licence | Weights source | VRAM (legion) | Seconds per output | Reason it won |
|---|---|---|---|---|---|---|---|
| Sound effects | EzAudio-XL (provisional) | MOSS-SoundEffect 8B NF4 + tokenizer offload | MIT / Apache-2.0 | `OpenSound/EzAudio` / `OpenMOSS-Team/MOSS-SoundEffect` | not measured (MOSS: 10.95 GB on 12 GB without offload) | MOSS: about 100 s per 10 s clip (12 GB info) | The only commercial, non-gated text-to-audio models found. EzAudio is small enough for 8 GB; MOSS is the only one measured so far, but it is slow and needs offload to fit. Stable Audio Open replaces EzAudio if the owner unlocks it and it measures well |
| Video-to-audio foley | HunyuanVideo-Foley XL with offload (provisional) | Sound-effects pick, placed and cut to picture | Tencent Hunyuan Community (commercial use allowed; excludes the EU, UK and South Korea) / see Sound effects | `tencent/HunyuanVideo-Foley` / see Sound effects | not measured (vendor: 8 GB with offload) | not measured | The only commercial video-to-audio model with an 8 GB path. MMAudio and ThinkSound are non-commercial |
| Music | ACE-Step 1.5 turbo (DiT, + LM 0.6B for structure, CPU offload) | HeartMuLa-oss-3B | MIT / Apache-2.0 | `ACE-Step/Ace-Step1.5` / `HeartMuLa/HeartMuLa-oss-3B-happy-new-year` | 6.6 GB (8 GB emulated) | 4.7 s DiT only, 34.5 s with LM per 30 s cue | Fastest measured music model with a permissive licence, exact cue length and 48 kHz stereo; fits 8 GB with offload |
| Temp TTS | Kokoro-82M | Qwen3-TTS-12Hz-0.6B-Base | Apache-2.0 / Apache-2.0 | `hexgrad/Kokoro-82M` / `Qwen/Qwen3-TTS-12Hz-0.6B-Base` | 1055 MiB / 6820 MiB | 0.37 s per 10.7 s line / 98.6 s per 9.9 s line | About 30 times faster than real time at 1 GB, with a word-exact ASR round trip. Qwen3-TTS adds voice cloning |
| Omni audio critic | Qwen2.5-Omni-7B Q4_K_M (llama.cpp) | LFM3-Omni-2B | Apache-2.0 / LFM Open License v1.0 | `ggml-org/Qwen2.5-Omni-7B-GGUF` / `LiquidAI/LFM3-Omni-2B` | 7253 MiB / LFM_VRAM | 11.9 s / LFM_SECS per clip | Heard both test clips plausibly and fits the 8 GB card fully on GPU. The smaller LFM leaves room for another model |
| Image | Z-Image-Turbo GGUF Q4_K_M | FLUX.2 klein 4B GGUF | Apache-2.0 / Apache-2.0 | `jayn7/Z-Image-Turbo-GGUF` / `unsloth/FLUX.2-klein-4B-GGUF` | 8011 MiB | 37.2 s per 1024x1024 image | Best measured stills (sharp, readable on-screen text) under a permissive licence, and it fits 8 GB |
| ASR / alignment | faster-whisper large-v3-turbo | - | MIT | `Systran/faster-whisper-large-v3-turbo` | ASR_VRAM | ASR_SECS | Exact transcript with word timestamps in well under a second |
| CLAP | laion/larger_clap_general | - | Apache-2.0 | `laion/larger_clap_general` | CLAP_VRAM | CLAP_SECS | Small zero-shot audio-text similarity for SFX search and checks |

## Image-to-3D and motion

Both jobs were moved to #125 (M3) by the owner decision on #49, so they are not measured here. The candidates
carried over are Pixal3D (MIT) and Hunyuan3D-2.1 (Tencent Hunyuan Community) for image-to-3D, and Kimodo
(Apache-2.0 code, NVIDIA Open Model weights) for motion.

## Still to measure on legion

Each of these runs is ready to go; only the download is missing. Run `push.sh legion`, then on `legion`:

| Job | Run | Download |
|---|---|---|
| Sound effects | `run.ps1 tts dl_queue.py EzAudio flan-t5-xl`, then `run_repo.ps1 ezaudio ezaudio sfx_ezaudio.py s3_xl` | about 7 GB |
| Sound effects | `run.ps1 tts dl_queue.py MOSS-SoundEffect`, then `run.ps1 sfx sfx_moss.py nf4-offload` | about 16 GB |
| Foley | `run.ps1 tts dl_queue.py HunyuanVideo-Foley`, then `run_cli.py foley ...` | about 8 GB plus encoders |
| Music | `setup_ace.ps1`, `run.ps1 tts dl_ace.py`, then `run_repo.ps1 ace ace music_ace.py lm offload` | about 7 GB plus the venv |
| Image | `run.ps1 tts dl_queue.py FLUX.2-klein`, then `run.ps1 img image_diffusers.py flux2klein gguf-Q8_0` | about 12 GB |

## Owner actions

1. **Stable Audio Open Small and 1.0** (Stability AI Community licence): log in to Hugging Face, accept the licence on
   both model pages and create a read token. Set it as `HF_TOKEN` in `legion`'s user environment, never in a file in
   the repo. Then run `run.ps1 sfx sfx_stable_audio.py small` and `... 1.0`. The script reads the token only from the
   environment, and `common.build_record` refuses to write a row that contains it.

## Reproducing

From the dev box, `scripts/model_check/push.sh legion` copies the scripts to `C:\spectracut\models\model_check`.
`bash scripts/model_check/legion_smoke.sh` runs one real Kokoro measurement on `legion` and checks the results row.
`python scripts/model_check/collect_results.py --job tts` prints the rows as a markdown table.

On `legion` (PowerShell, `-ExecutionPolicy Bypass`):

```powershell
model_check\setup_env.ps1 -Name tts -Packages kokoro,soundfile   # one venv per model family
model_check\run.ps1 tts tts_kokoro.py
```

| Script | Purpose |
|---|---|
| `common.py` | VRAM/RAM sampling, results rows, JSONL append, markdown table (pure helpers unit-tested in `tests/test_model_check.py`) |
| `legion_smoke.sh`, `push.sh`, `collect_results.py` | dev-box side: e2e smoke, copy scripts, fetch results |
| `setup_env.ps1`, `setup_ace.ps1`, `setup_repo.ps1`, `setup_llamacpp.ps1`, `pipinstall.ps1` | venvs, repos and llama.cpp binaries on `legion` |
| `run.ps1`, `run_repo.ps1`, `run_cli.py` | run a model script (or a repo CLI) inside a venv and record one row |
| `dl_queue.py`, `dl_ace.py`, `hf_get.py` | sequential weight downloads into the HF cache |
| `tts_kokoro.py`, `tts_qwen3.py` | temp TTS |
| `sfx_moss.py`, `sfx_ezaudio.py`, `sfx_stable_audio.py` | sound effects |
| `music_ace.py` | music |
| `omni_llama.ps1`, `omni_lfm.py` | omni critic |
| `image_diffusers.py` | image |
| `asr_clap.py`, `audio_stats.py` | ASR round trip, CLAP, clip stats |
