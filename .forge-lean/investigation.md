# Investigation: #326 style frames hard-require the optional image model

**Verdict**: CONFIRMED
**Confidence**: high
**Severity**: P0 (blocks the M2 AlterLab launch film: style_frames fails on any model host set up with `--skip-models` or the essential set)
**Task Type**: bug

### What Was Claimed
On a model host without `z-image-turbo-q4` (the optional image model; #322's essential set excludes it), `studio run` fails `style_frames` with `ProviderError: local: model host 'legion': z-image-turbo-q4: models-serve exited`. The reason is that `default_explorer` builds the concept-image explorer whenever a ModelRuntime exists. It never checks whether the image model is installed. Style frames and boards should render through the scene builder without a concept image, with a note on the node.

### What We Found
Verified against base `origin/milestone/m2-first-film-alterlab-launch-film` @ 5626cfa:
- `src/spectracut/studio/departments/art.py:318-331` `default_explorer`: mock gives the mock provider. Otherwise `runtime is None` gives None, and any runtime at all gives `choose_exploration_provider(config, local=get_provider("image", runtime))`. Nothing checks whether the model is installed.
- `src/spectracut/cli/studio.py:382` wires `explorer=art_depts.default_explorer(runtime=runtime)` for every preproduction run. The runtime comes from `model_runtime()` at `src/spectracut/cli/studio.py:174-221`, which never reads setup state.
- `src/spectracut/studio/departments/art.py:465-467` (`_style_frames`) and `:541-542` (`_boards`, used by the animatic) call `explore(self.explorer, ...)` unguarded. Any `ProviderError` from the explorer fails the whole node before the scene-builder renderer runs.
- `src/spectracut/providers/local/remote.py:111-114` raises `models-serve exited (...)` when the host refuses to load a model whose weights or loader are missing. This is the live error.
- `src/spectracut/providers/local/registry.py:82,114`: `z-image-turbo-q4` is the default `image` model. `src/spectracut/setup/model_assets.py:257-264` `ESSENTIAL_MODELS` excludes it.
- A cheap query for installed models already exists on the control plane: setup records each installed model in `<home>/setup-state.json` as `hosts[<host>].components["model:<id>"] = model_version(<id>)`. See `src/spectracut/setup/plan.py:189-224` (`SetupState`, `load_state`), `:728` (`MODEL_PREFIX`), `:1052` (setup's own "missing" test) and `src/spectracut/setup/model_assets.py:241` (`model_version`).
- `src/spectracut/providers/local/runtime.py:64-82` `ModelRuntime` has no notion of which models its host has installed.
- Node notes: `src/spectracut/studio/graph.py:420-434` `ProductionGraph.add_note` / `notes` (RevisionNote, `graph.py:62-76`, `author` field). The graph snapshot persists notes (`graph.py:239`). `studio run` has no FactsStore (`FactsStore(` is never constructed in `src/`), so `RunResult.facts` would not record anything. The `pre()` closure at `src/spectracut/cli/studio.py:374-400` has `graph` in scope.

### Root Cause
`default_explorer` (`art.py:328-331`) uses "a ModelRuntime exists" to mean "the image model can run". After #322 the image model is optional, so that assumption no longer holds. On top of that, `PreproductionRunner` treats any explorer error as fatal (`art.py:467`, `:542`), although the concept image is only an optional input to the scene-builder render.

### Affected Files
- `src/spectracut/providers/local/runtime.py`: `ModelRuntime` gains an optional `installed` set (None means unknown).
- `src/spectracut/providers/local/installed.py` (new): `installed_models(home, host)` reads setup state.
- `src/spectracut/studio/departments/art.py`: `concept_skip_reason` (new), the guard in `default_explorer`, and `PreproductionRunner` degradation plus the note hook.
- `src/spectracut/cli/studio.py`: `model_runtime()` passes `installed`, and `pre()` passes the skip reason and a note hook to `PreproductionRunner`.
- Tests: `tests/storyboard/test_preproduction.py`, `tests/studio/test_art_departments.py`, `tests/cli/test_studio_run.py`, `tests/providers/test_local_runtime.py`.
- Enumerating tests named by the engine. None of them should need a list change, because no new file is added under `studio/departments` or `cli` and the existing exports are unchanged. Re-run them anyway:
  - `tests/cli/test_studio_run.py:233` / `:254`. Also note `tests/cli/test_studio_run.py:762-795`: its fake `default_explorer(config=None, *, runtime=None)` and fake `PreproductionRunner(*a, **kw)` constrain the call signatures. The call site must keep `default_explorer(runtime=runtime)`.
  - `tests/studio/test_finishing_departments.py:164`
  - `tests/cli/test_cli.py:92`, `tests/cli/test_cli.py:2`
  - `tests/acceptance/recorded/__init__.py:9`, which drives `default_runner` in the M2 acceptance run (`tests/acceptance/test_m2_preproduction.py`).

### Sibling Sweep
- `default_voice` (`art.py:340-351`) uses the same "runtime exists" pattern for `kokoro-82m`. Kokoro is in `ESSENTIAL_MODELS` (`model_assets.py:260`), and a missing voice is already an explicit error (`art.py:527-528`). Out of scope.
- Sound design (`src/spectracut/studio/departments/sound*.py`) loads `ezaudio-xl`, `ace-step-1.5` and `hunyuan-foley-xl`. The first two are essential. `hunyuan-foley-xl` is optional, so foley has the same defect class. Recommend a follow-up issue rather than widening this one (requirement R4 is not in the issue). Noted here for the reviewer.
- `critic` (`qwen2.5-omni-7b-q4`) is optional too. Critic paths should be checked in the same follow-up.

### History
- `default_explorer` was introduced in 3ceb093 (forge checkpoint, #270/#271 line of work). #273 wired `PreproductionRunner` into `studio run` (`cli/studio.py:374-400`).
- #281 (7560df7, merged in 5626cfa) made style frames render through the scene builder. That fallback without a concept now exists but is never reached.
- #322 (fe3b73a) made the image model optional (`ESSENTIAL_MODELS`, `--skip-models`) and exposed the defect.
- No prior fix.

### Recommendation
1. `ModelRuntime(..., installed: Collection[str] | None = None)` stores `installed: frozenset[str] | None`. None means unknown (for example a host installed by hand) and keeps today's behaviour.
2. New `providers/local/installed.py`: `installed_models(home, host) -> frozenset[str] | None`. It returns the ids whose `model:<id>` component equals `model_version(id)` in `setup-state.json`. It returns None when the state has no record of `host` or cannot be read. `spectracut.setup` is imported lazily inside the function to avoid an import cycle (`setup/plan.py:90` imports `studio.store`).
3. `cli/studio.py:model_runtime()` passes `installed=installed_models(home, slot.name)` to both `ModelRuntime(...)` constructions (`:217`, `:218`).
4. `art.py`: new `concept_skip_reason(config=None, *, runtime=None) -> str | None`. It returns a reason when the local image model (`resolve_model("image", ...)`) is not in `runtime.installed`. It returns None in mock mode, when `runtime` is None, when `installed` is None or absent (read with `getattr`), or when `image_provider` is chatgpt. `default_explorer` returns None (and logs a warning) whenever there is a reason. Its signature is unchanged.
5. `PreproductionRunner(..., concept_note: str | None = None, note: Callable[[str, str], None] | None = None)`:
   - With no explorer and a `concept_note`, each `style_frames` / `animatic` build calls `note(node_id, concept_note)` once.
   - An explorer `ProviderError`/`OSError` during a build logs a warning and leaves that frame or board without a concept. It also stops exploring for the rest of that build, notes the node once with the error, and does not count toward `image_exploration_requests`. The node never fails because of the explorer.
6. `cli/studio.py:pre()` computes `art_depts.concept_skip_reason(runtime=runtime)` and passes `note=` as a closure. The closure calls `graph.add_note(node_id, RevisionNote(node_id=..., text=..., author="studio", created_at=now))` and skips text already noted on that node.

### Related Issues
#322 (optional image model), #281 (scene-built style frames), #271/#273 (PreproductionRunner wiring), #289 (sound_design needed a ModelRuntime; same family).
