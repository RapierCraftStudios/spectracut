# Benchmark films

Benchmarks are the definition of quality (spike § Long-form work and the quality
strategy). Each capability has reference pieces and a target score, rated by the
Critic and by people. Nothing counts as done until it beats its benchmark, and no
change ships that lowers one.

Code: `src/spectracut/critique/benchmarks.py`; CLI: `spectracut bench`.

## Layout

```
benchmarks/
  rubrics/<rubric>.json        weighted criteria the Critic (and people) score
  sets/<set>/set.json          the pieces of a benchmark set
  sets/<set>/baseline.json     accepted scores per piece
  sets/<set>/references/       reference pieces
  sets/<set>/outputs/<piece>/  rendered outputs; the newest file is scored
  sets/<set>/results/          report.json, report.md, scores.jsonl, ratings.jsonl
  harness/<task>.json          headless harness tasks with hidden ground truth
```

The directory defaults to `./benchmarks`; override it with `--benchmarks-dir` or
`SPECTRACUT_BENCHMARKS_DIR`.

### Rubric

```json
{"id": "ad-30s", "title": "30 second ad",
 "criteria": [{"id": "message", "prompt": "Does the frame carry one clear message?", "weight": 2.0}]}
```

Each criterion is scored in [0, 1] by the Critic (`VisionJudge.score(frame, "<title>: <prompt>")`);
the piece score is the weight-averaged criterion score.

### Set

```json
{"id": "fixtures", "title": "...",
 "pieces": [{"id": "ad-30s", "capability": "short-ad", "rubric": "ad-30s",
             "outputs": "outputs/ad-30s", "pattern": "*.png",
             "reference": "references/ad-30s.png", "fixture": true,
             "target": 0.5, "tolerance": 0.05}]}
```

Paths are relative to the set directory. Files are validated strictly; an invalid
file fails with its path and the offending field.

## Commands

- `spectracut bench run <set> [--out DIR] [--write-baseline]` scores each piece's
  latest output (newest mtime, ties by name) per its rubric and writes `report.json`
  and `report.md`. Every Critic score is also appended to `scores.jsonl`.
- `spectracut bench rate <set> <piece> <score> [--rater ID] [--output FILE]` records a
  human rating in [0, 1] for an output (default: the latest) in `ratings.jsonl`, keyed
  by the output's content hash, next to the Critic's score for the same output. The
  next report shows both.
- `spectracut bench check <set>` scores the set and exits 1 when any piece drops
  below its baseline by more than its `tolerance` (or was not scored).
  `run --write-baseline` accepts the current scores as the new baseline.
- `spectracut bench agreement <set>` reports Critic-vs-human agreement.
- `spectracut bench harness [--solutions DIR] [--width W --height H]` runs the ported
  harness tasks headless at tiny resolution.

The Critic is selected like every other vision judge: `SPECTRACUT_PROVIDERS=mock`
uses the deterministic mock (CI), otherwise Claude sign-in or `ANTHROPIC_API_KEY`.

## Fixture references (#47)

The `fixtures` set covers the four benchmark films from the spike (device launch,
app walkthrough, 30 s ad, title sequence) with tiny generated stand-in frames. Every
piece is marked `"fixture": true` until the owner's reference videos exist (#47);
then a real set replaces it. Its `baseline.json` was recorded with the mock judge,
so `SPECTRACUT_PROVIDERS=mock spectracut bench check fixtures` passes in CI. Its
`results/` directory is git-ignored.

## Critic-vs-human agreement (BlenderGym protocol)

Following BlenderGym's verifier-agreement protocol, every pair of outputs that has
both a Critic score and a human rating (mean of ratings per output) is compared:
the Critic agrees when it prefers the same output people prefer. Pairs people rate
equal are excluded (`human_ties`); pairs the Critic scores equal count as
disagreement (`critic_ties`). `agreement_rate = agreements / pairs`, reported with
Spearman's rho and Kendall's tau-b. Where the Critic and people disagree, fix the
rubric. The agreement is in every `report.json` and from `bench agreement`.

## Harness tasks

Harness tasks are seeded from the task shapes of SceneActBench, 3DCodeBench and
BlenderWorld (MIT/Apache-licensed harnesses: headless Blender tasks scored against
hidden ground truth). They are re-authored here as declarative scenes, not vendored
code:

```json
{"id": "...", "source": "sceneactbench|3dcodebench|blenderworld",
 "instruction": "...", "resolution": [64, 36], "threshold": 0.95,
 "start": {"background": [r,g,b], "light": 1.0, "camera": {"pan": [0,0], "zoom": 1},
           "objects": [{"id": "...", "shape": "rect|circle", "center": [x,y], "size": s, "color": [r,g,b]}]},
 "ground_truth": { ...same scene shape, hidden from the solver... }}
```

A solver sees only the start scene and instruction (`HarnessTask.public()`) and
returns a scene. The scorer renders candidate and ground truth with a tiny
pure-Python rasteriser (no Blender: bpy never runs in the control plane) and
scores `0.5 * pixel similarity + 0.5 * parameter similarity`. Without
`--solutions`, `bench harness` is a self-check that every start scene fails and
every ground truth passes; it runs in CI through the pytest suite. Running the same
declarative tasks in Blender belongs on the render hosts.
