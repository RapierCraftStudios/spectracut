**Verdict**: CONFIRMED
**Confidence**: high
**Severity**: medium (feature gap; blocks the M2 launch film from starting from a one-line idea)
**Task Type**: feature

### What Was Claimed
A production needs a hand-written brief JSON. Nothing turns a vague idea plus sources into answers, defaults with rationale, doubts and an assumptions page. The issue asks for `studio new --idea ... --source ... --headless`.

### What We Found
- `src/spectracut/cli/studio.py:166-225`: `studio new` takes a required BRIEF file via `_load_brief` (`:134`). There is no `--idea`, `--source` or `--headless`.
- `src/spectracut/studio/departments/discovery.py:112-129`: `discovery_nodes(answers, pages)` requires caller-supplied answers and pages. Nothing generates them. The Intake department only reshapes `answers`.
- `src/spectracut/studio/departments/discovery.py:8-11`: departments never fetch URLs (contract `docs/engineering/contracts/departments.md`). Source fetching must therefore happen outside departments, in `intake.py` or the CLI.
- `src/spectracut/spec/brief.py:63-82`: `CreativeBrief` has no assumptions record. `StrictModel` is strict, so the field must be added explicitly.
- `src/spectracut/spec/snapshots/creative_brief.schema.json` is a schema snapshot. Adding a field requires `--update`, and the change must be additive and optional to stay compatible (`tests/spec/test_schema_compat.py`).
- `src/spectracut/studio/store.py:89-120`: `ProductionStore.create` writes brief.json, production.json and graph.json. There is no hook for an `assumptions.md` artifact. Write it with `write_atomic` into `store.path(pid)` after create.
- `src/spectracut/writer/copy_gate.py:55-66`: a whole-word, case-insensitive forbidden-word matcher already exists. `spec/brand.py:121` holds `BrandKit.forbidden_words`.
- No `intake.py` and no `tests/studio/test_intake.py` exist yet.

### Root Cause
Missing feature. There is no question bank, no defaults-with-rationale and no self-sourcing of claims, so the brief must be hand-authored.

### Affected Files
- `src/spectracut/studio/intake.py` (new): question bank per film type, a default and rationale per aspect, doubt batching (max 4, multiple-choice with a recommended option), headless resolution, source loading (local path or URL), proof points derived only from fetched source text, forbidden-word filtering, and rendering of `assumptions.md`.
- `src/spectracut/spec/brief.py`: optional `assumptions: list[Assumption]` on `CreativeBrief`, with `Assumption{aspect, value, rationale}` and `dropped_claims`.
- `src/spectracut/spec/snapshots/creative_brief.schema.json`: regenerated.
- `src/spectracut/studio/departments/discovery.py`: helper that produces answers and pages from the idea and sources, so `discovery_nodes` no longer needs them supplied.
- `src/spectracut/cli/studio.py`: `studio new --idea/--source/--headless`. BRIEF becomes optional, and exactly one of BRIEF or `--idea` is required.
- `tests/studio/test_intake.py` (new).
- `tests/cli/test_cli.py`: new CLI test.
- Enumerating tests that must stay green: `tests/spec/test_schemas.py:38` (VALID creative_brief fixture and sibling schema names), `tests/cli/test_cli.py:84` (studio new output), `tests/spec/test_schema_compat.py`.

### Sibling Sweep
- `Brief` (`brief.py:49`) duplicates the proof-point source check. Add assumptions only to `CreativeBrief`, which is the output of Discovery.
- Forbidden words are enforced at `copy_gate.py` and `templates/base.py:196`. The intake path needs its own check on brief text, reusing the `copy_gate` matcher.
- Mock/no-network tests are the pattern in `tests/studio/test_discovery.py`. Fetching must take an injectable fetcher so tests use no network.

### History
`discovery.py` was last touched in 470ad37 (WIP checkpoint). No earlier attempt at intake autofill was found in this tree.

### Recommendation
Implement deterministically and offline-testable, with no LLM needed for the headless path:
1. `intake.py` defines `Aspect` (id, question, options with one recommended, default, rationale) per film type. `resolve(idea, sources, headless, ask)` fills aspects the idea or sources answer. Remaining aspects are batched into groups of at most 4 doubts. Headless takes the recommended option. All defaults go into assumptions.
2. Source loading takes an injectable fetcher. Local paths are read from disk, and URLs go through the fetcher. Each source gets an id, and proof points are sentences from source text that cite that id. Candidate claims without a source go to `dropped_claims` and are never invented.
3. Build a `CreativeBrief` and check it against the brand's forbidden words. Offending claims are dropped and listed.
4. The CLI calls `store.create` and then writes `assumptions.md`.

### Related Issues
#216 (deferred items from #49), #44 (department contract), #63 (AlterLab launch film acceptance).
