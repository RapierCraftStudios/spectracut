### Approach
The model host's installed models are read from setup state (`model:<id>` component equal to `model_version(id)`), which costs nothing at run time. The result is carried on `ModelRuntime.installed`, so `default_explorer(runtime=runtime)` keeps its call shape. When the local image model is known to be missing, `default_explorer` returns None and logs a warning. `PreproductionRunner` then notes the skip on the style_frames and animatic nodes through a callback, and `studio run` turns that callback into a graph `RevisionNote` (author "studio", added once). An explorer `ProviderError`/`OSError` at render time is logged and noted, and the rest of that build renders without concepts. The node still succeeds.

### Changes
- src/spectracut/providers/local/installed.py: new `installed_models(home, host)`. It imports setup lazily and returns None when the state is unknown.
- src/spectracut/providers/local/runtime.py: `ModelRuntime(..., installed=None)` stores the set as `self.installed: frozenset[str] | None`.
- src/spectracut/studio/departments/art.py: new `concept_skip_reason`. `default_explorer` returns None with a warning when there is a reason. `PreproductionRunner` gains `concept_note`/`note`, and `_skip_concepts`/`_concept` degrade on explorer failure.
- src/spectracut/cli/studio.py: `model_runtime` passes `installed=installed_models(...)`. `pre()` passes `concept_note` and a `_note_once` closure that adds a graph RevisionNote.
- tests/studio/test_art_departments.py: regression tests for an absent model, an explorer failure, an installed model (and an unknown installed set), and ChatGPT images.
- tests/cli/test_studio_run.py: `studio run` default_runner with the image model absent (scene-built frames and boards, one note per node, no loads), and `model_runtime` reading setup state.
- tests/providers/test_local_runtime.py: tests for `installed_models` and `ModelRuntime.installed`. The stale `test_host_loader_requires_installed_factory` assertion is updated: since the built-in factory table (#296), a missing ezaudio install says "run `spectracut setup`" instead of naming the entry env var. This test failed on base too.

### Acceptance Criteria Status
- R1 ✅ src/spectracut/studio/departments/art.py:342 (default_explorer None + warning), art.py:505-524 (note once; explorer failure degrades), src/spectracut/cli/studio.py:183 (node note)
- R2 ✅ src/spectracut/providers/local/installed.py:15, src/spectracut/providers/local/runtime.py:74, src/spectracut/cli/studio.py:232
- R3 ✅ tests/studio/test_art_departments.py:320 (style frames render, concept None)
- R4 ✅ src/spectracut/cli/studio.py:423; tests/cli/test_studio_run.py:418
- R5 ✅ tests/studio/test_art_departments.py:363
- R6 ✅ V1–V5 pass (targeted suites, ruff, ruff format, mypy); the full suite is left to CI per host rules

### Verification
- V1 `uv run pytest -q tests/studio/test_art_departments.py tests/storyboard/test_preproduction.py tests/providers/test_local_runtime.py tests/providers/test_remote_runtime.py`: pass. It failed before the stale-test fix, and that failure was pre-existing on base.
- V2 `uv run pytest -q tests/cli/test_studio_run.py tests/cli/test_cli.py tests/studio/test_finishing_departments.py`: pass (48)
- V3 `uv run pytest -q tests/acceptance/test_m2_preproduction.py`: pass (1 skipped, same as base)
- V4 ruff check + ruff format --check: pass
- V5 mypy: pass
- `forgedock check`: PASS
- The repro test fails without the fix: `concept_skip_reason` and the `installed`/`concept_note`/`note` keywords do not exist on base, so it errors at import or construction.

### Notes
- Behaviour change: with setup state recording the model host but no `z-image-turbo-q4`, style frames and boards render without concept images, and the style_frames/animatic nodes carry a "concept images skipped" studio note. When the host is unrecorded (unknown), behaviour is unchanged.
- No new env vars.

## Stale-list follow-up
`installed` (providers/local/installed.py) is not a member of the lists at tests/audio/test_generate.py:80 (fake runtime helper) or tests/test_model_check.py:171 (model-check result records); recorded in result.json not_members. No code changes.
