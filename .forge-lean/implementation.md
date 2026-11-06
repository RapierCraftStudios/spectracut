### Approach
A frozen `FilmContract` (rows R1..Rn) is built from the brief node when it is approved. An injected conformance reviewer judges each row at `edit` and `delivery`. Code computes the verdict from the rows and ignores the reviewer's summary. The graph folds a non-passed verdict into a confirmed HIGH finding, so the existing reject path blocks the node and everything downstream of it.

### Changes
- src/spectracut/studio/contract.py: new. `ContractRow`, `FilmContract`, `freeze_contract` (same brief hash returns the prior contract; a changed brief returns version+1).
- src/spectracut/studio/conformance.py: new. `Evidence`, `RowJudgement`, `ConformanceReview`, `conformance_verdict`, `blockers`.
- src/spectracut/studio/graph.py: `ProductionGraph.contract`, optional `run(conformance=...)`, freeze on brief approval or acceptance, `_conform` folded into the verdict.
- src/spectracut/studio/store.py: write-once `contracts/v<N>.json`, `save_contract`, `contract()`; `save` and `load` carry the contract.
- tests/studio/test_contract.py: new, 11 tests.

### Acceptance Criteria Status
- R1 ✅ contract.py:50-82, store.py save_contract, graph.py `_freeze_contract`; test_contract.py immutability, versioning and store tests
- R2 ✅ conformance.py (Evidence, RowJudgement), graph.py `_conform`
- R3 ✅ conformance.py `conformance_verdict`; test_failed_or_incomplete_conformance_blocks_delivery
- R4 ✅ conformance.py `conformance_verdict` (the `claimed` and `summary` fields are never read); test_model_summary_does_not_decide_the_verdict
- R5 ✅ V2 and acceptance tests pass

### Verification
- V1 `uv run pytest -q tests/studio/test_contract.py`: 11 passed. The new tests fail without the fix, since the modules do not exist (ImportError).
- V2 existing graph, producer, supervisor, intake and department tests: 73 passed.
- Also run: tests/acceptance/test_m2_launch_film.py, test_m2_discovery.py, tests/test_layout.py, tests/test_spdx.py, tests/cli/test_cli.py (63 passed, 2 skipped). `mypy src/spectracut/studio` and `ruff check` pass; `ruff format` was applied.
- I did not run the wide scoped list the checks tool printed. `forgedock check` passes. The `--publish` run reported "no local cache".

### Notes
- With no conformance callable or no frozen contract, behaviour is unchanged (P1).
- Without a callable, `delivery` is not blocked even when a contract exists, to keep the unchanged-behaviour guarantee. The caller must pass `conformance` to enforce it.
- Brand-rule rows are built only when the caller passes `brand_rules` to `freeze_contract`. The graph's approve hook does not have the BrandKit, so it freezes without them.
- The test list at tests/studio/test_producer.py:46 holds no module names, so there is nothing to add.

## CI follow-up
tests/studio/test_producer.py:46 holds no list of studio modules (it is the Recorder helper; imports are only what the test uses), so conformance and contract are recorded as not_members in result.json.
