### Approach
Two family modules built on `BaseSession`, following the measured precedents. I checked the EzAudio API at the pinned commit 86f1826 (`api/ezaudio.py`, `ckpts/ezaudio-xl.yml`) against upstream source:

- `EzAudio(model_name, ckpt_path, vae_path, device)` accepts explicit checkpoint and VAE paths.
- The model config is read relative to cwd from the source checkout.
- The T5 encoder name comes from that config.

So the ezaudio loader passes the installed DiT and VAE paths, changes into the source checkout only while the model is built, and swaps the config's text-encoder name for the local `flan-t5-xl` dir. MOSS follows the `nf4-offload` path: NF4 language model on GPU, audio tokenizer on CPU except during `decode`, loaded from the local snapshot with `local_files_only`. Both write PCM_16 wav (explicit format, because `out_path` has no suffix), trimmed or zero-padded to exactly `duration_s`.

### Changes
- `host/spectracut_host/loaders/ezaudio.py`: `load(spec)` → `EzAudioSession`.
  - Passes the installed `ckpts/s3/ezaudio_s3_xl.pt` and `ckpts/vae/1m.pt` paths; a missing file raises "run `spectracut setup`".
  - `generate_audio` gets `length = ceil(duration_s)` clamped to 1–10 s (EzAudio-XL's 500 latent frames), 50 steps, guidance 5, `random_seed=seed`.
  - `write_wav` is the shared fit-and-write helper.
  - `_unload` drops the model and the `sys.path` entry.
- `host/spectracut_host/loaders/moss_sfx.py`: `load(spec)` → `MossSfxSession`.
  - NF4 BitsAndBytesConfig with bf16 compute, sdpa attention, `device_map={"":0}`, cuDNN SDP off.
  - `_generate` seeds torch, generates with `max_new_tokens=1024`, moves the tokenizer to cuda only for decode, and moves it back to cpu in a `finally`.
  - sr comes from `model_config.sampling_rate` (default 24000).
- `tests/host/test_sfx_loaders.py`: 6 unit tests with fake torch, numpy, soundfile (writes a real wav via stdlib `wave`), `api.ezaudio` and transformers.
- `tests/host/test_sfx_loaders_e2e.py`: SPECTRACUT_E2E=1 smoke for each model on a CUDA host. It checks the wav reads back, has the exact length and is not silent, and that peak VRAM is <= 8 GiB.
- `.forge/post-deploy-checks.json`: `sfx-loaders-legion-e2e`.

### Acceptance Criteria Status
- R1 ✅ `host/spectracut_host/loaders/ezaudio.py:90`, `host/spectracut_host/loaders/moss_sfx.py:63`, wav writer `host/spectracut_host/loaders/ezaudio.py:39-54`
- R2 ✅ unit tests `tests/host/test_sfx_loaders.py:143-323`; legion smoke `tests/host/test_sfx_loaders_e2e.py:31`, which runs only on the model host via `.forge/post-deploy-checks.json:107` (out of band)

### Verification
- V1 `uv run pytest -q tests/host/test_sfx_loaders.py tests/host/test_model_loaders.py`: 20 passed.
- V2 `uv run pytest -q tests/host/test_sfx_loaders_e2e.py`: 2 skipped (no SPECTRACUT_E2E here, by design).
- V3 ruff check + format --check: pass.
- V4 mypy (strict) on the 4 files: no issues.
- V5 post-deploy JSON assert: pass.
- Without the fix: moving the two loader modules away makes `tests/host/test_sfx_loaders.py` fail at collection (1 error).
- Also ran `tests/test_layout.py`, `tests/host/test_host_wheel.py` and `tests/setup/test_model_assets.py`: 47 passed.
- `forgedock check`: PASS.
- The checks tool widened its test set to almost the whole suite (and a vitest run for `packages/capture`) because the JSON file is an unknown language. Per the host rules I ran the targeted files above instead.

### Notes
- No new env vars. Weights resolve under `SPECTRACUT_MODEL_ROOT` (existing).
- EzAudio cues longer than 10 s and MOSS cues longer than about 10 s (1024 tokens) are padded with silence to `duration_s`.
- The real GPU run, including the EzAudio T5 swap and the MOSS remote-code API, has not happened. It runs only through `sfx-loaders-legion-e2e` on legion.

### Duplicate export `load` (ezaudio.py, moss_sfx.py): intentional, no rename
`FACTORIES` in `host/spectracut_host/models.py:49` resolves every family to `<loaders>.<family>:load`, and the contract (D1, D2, P1) fixes `load(spec)` in each module. The two `load`s sit in separate modules, each reached through its own FACTORIES entry, so nothing collides or goes unprobed. Renaming would break the FACTORIES convention and need a `models.py` change, which P1 forbids. No code change.
