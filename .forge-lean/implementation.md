### Approach
Deterministic, offline-testable intake in `studio/intake.py`. The idea answers product and platforms. Remaining aspects come from a per-film-type question bank. Doubts are batched at most 4 at a time, and headless takes the recommended option. Proof points are sentences from fetched sources that cite the source id. Unsourced or forbidden-word claims are dropped and listed.

### Changes
- src/spectracut/studio/intake.py: new module (bank, resolve, batches, load_sources, build_brief, assumptions_markdown).
- src/spectracut/spec/brief.py: `Assumption`, plus `CreativeBrief.assumptions` and `dropped_claims`, both optional.
- src/spectracut/spec/snapshots/creative_brief.schema.json: regenerated.
- src/spectracut/studio/departments/discovery.py: `intake_inputs` helper.
- src/spectracut/cli/studio.py: `studio new --idea/--source/--headless`; writes `assumptions.md`.
- tests/studio/test_intake.py (new) and tests/cli/test_cli.py: regression tests.

### Acceptance Criteria Status
- R1 ✅ cli/studio.py `new`, tests/cli/test_cli.py idea test
- R2 ✅ intake.py `resolve`/`assumptions_markdown`
- R3 ✅ intake.py `batches`/`resolve`, test_doubts_are_batched_by_four_and_answers_are_used
- R4 ✅ intake.py `build_brief`, test_unsourced_claims_are_dropped_and_listed
- R5 ✅ intake.py `_forbidden_in`, test_forbidden_words_enforced
- R6 ✅ V1 and V2 pass

### Verification
- V1: 63 passed.
- V2: 101 passed.
- `forgedock check`: PASS.
- The new CLI test fails without the cli change.
- ruff and mypy are clean on the changed files.

### Notes
- The default source fetcher uses urllib for http(s) URLs only.
- Forbidden words come from `--brand-kit` when given.
- `Assumption` is not exported from `spectracut.spec`.
- `studio new` now accepts either BRIEF or `--idea`, never both.

## Follow-up
tests/studio/test_producer.py:46 holds no module list (it is the Recorder fake); intake recorded in result.json not_members.
