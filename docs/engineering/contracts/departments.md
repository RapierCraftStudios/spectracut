# Departments

The studio's departments are its subagents: one per craft (writer, storyboard,
renderer, and so on). Each department is a typed Python class plus a skill-pack
folder, and the department runner executes it through the AI provider layer for
the production graph (`production-graph.md`). Every department added in later
issues implements this contract. This implements spike § The studio: a
production graph run by agent departments (`docs/engineering/technical-spike.md`).

Code: `src/spectracut/studio/department.py`, `src/spectracut/studio/runner.py`,
`src/spectracut/providers/base.py` (`TextProvider`, `Completion`) and
`src/spectracut/providers/mock_text.py`. Like the graph engine, departments and
the runner are pure Python: they never import Blender or call a network service
themselves. Only a provider talks to an AI service.

## Department

A department subclasses `Department` and declares class variables:

| Class variable | Type | Meaning |
|---|---|---|
| `name` | slug | department slug; matches `NodeSpec.department` |
| `produces` | non-empty `frozenset[str]` | node kinds this department makes |
| `inputs` | `Mapping[str, type[BaseModel]]` | input node kind → model that input's output must validate against |
| `output` | `type[BaseModel]` | model the department's output must validate against |
| `skill_pack` | `Path` or `None` | skill-pack folder; `None` means `spectracut/skills/<name>/` |
| `cost` | `CostEstimate` | expected cost of one run |
| `uses_gpu` | bool, default `False` | runs hold a GPU lease |

The declaration is checked when the class is defined: a name that is not a
slug, an empty `produces`, a bad node kind, an `output` or input model that is
not a pydantic model class, or a `cost` that is not a `CostEstimate` raises
`TypeError`. A shared base that only carries declarations sets
`abstract = True` in its own body and is not checked.

Class methods: `pack()` (the loaded `SkillPack`, cached per class), `rubric()`
(the pack's rubric text), `output_schema()` (`output.model_json_schema()`),
`validate_output(node_id, value)` (validates and returns
`model_dump(mode="json")`) and `validate_inputs(node_id, inputs, kinds)` (each
input's kind must be declared and its output must validate against its model).
Both raise `DepartmentOutputError(department, node_id, detail)`, a `ValueError`
whose detail summarises the pydantic errors.

### Skill pack

`SkillPack.load(path)` reads `prompt.md` (the system prompt) and `rubric.md`
(required, non-empty) and lists `failures/*.md` and `anchors/*.md`. A missing
folder or file raises `ValueError` naming the path. Layout and rules:
[`src/spectracut/skills/README.md`](../../../src/spectracut/skills/README.md).

### Cost estimate

`CostEstimate`: `ai_requests` (int ≥ 0), `ai_input_tokens` (int ≥ 0),
`gpu_minutes` (float ≥ 0) and `elevenlabs_characters` (int ≥ 0). It is what the
Producer adds up for a production's estimate; the actual cost of each run is in
the ledger.

## Text provider

`TextProvider` (a runtime-checkable protocol in `providers/base.py`) has a
`name` and `complete(system, prompt, *, schema) -> Completion`: answer `prompt`
under the system prompt `system` with JSON that conforms to the JSON schema
`schema`. `Completion` is `output` (JSON) and `usage` (metric slug → int).

`MockTextProvider` (`mock-text`) is the dev and CI provider: deterministic, no
network or CLI. It returns a minimal instance of the schema (objects with their
required properties, scalars, arrays, `enum`/`const`, the first `anyOf`/`oneOf`
branch and local `$ref` into `$defs`), with values derived from a hash of the
system prompt and prompt. An optional `respond(system, prompt, schema)` callable
overrides the output. Usage is `mock_text_requests` and `mock_text_input_chars`.

## Department runner

`DepartmentRunner(departments, provider, *, canon=None, brief=None,
limits=CapacityLimits(), graph=None)` is a graph `Runner`. Two departments with
the same name, two departments producing the same kind, or a GPU department
with `gpu_leases = 0` raise `ValueError`, and every skill pack is loaded up
front. For each node it:

1. **Resolves** the department by `spec.department` (unknown: `KeyError` naming
   it) and checks it produces `spec.kind` (else `ValueError`).
2. **Validates inputs** against the department's input models. Input kinds come
   from `graph` when given; otherwise an input's id is its kind.
3. **Assembles the context.** `assemble_context` builds a `TaskContext` with
   exactly `canon`, `brief`, `inputs` (the accepted outputs of the node's
   declared inputs only), `params` (the node's params) and `feedback` (the
   Critic's verdict on the previous attempt, or null). Nothing comes from the
   environment, other nodes or global state. The prompt is a fixed instruction
   plus the canonical JSON of that context; the skill pack's `prompt.md` is the
   system prompt.
4. **Holds capacity.** A GPU department first takes a GPU lease, then every
   task takes an AI slot around `provider.complete(...)`. Both are
   `threading.BoundedSemaphore`s sized by `CapacityLimits(ai ≥ 1, gpu_leases ≥ 0)`.
5. **Validates the output** with `validate_output`. A failure raises
   `DepartmentOutputError`, so the graph records the node `failed` with the
   error and a `failed` ledger entry, and accepts no output. There is no
   automatic retry: revisions are for Critic verdicts.
6. **Reports usage**: `RunResult(output, Usage(ai_usage=...))` with the
   provider's usage plus `prompt_context_bytes`, the UTF-8 byte length of the
   assembled context. The graph copies it into the run's ledger entry
   (`LedgerEntry.ai_usage`), so the ledger schema is unchanged.

### Critic hook

`runner.critic(judge)` returns a graph `Critic`. For each output it resolves the
node's department and calls `judge(department, rubric, output)`, which returns
a `CriticVerdict`. The graph still decides when to call it, per the node's gate
mode (`production-graph.md` § Gate).

## Parallel runs

`ProductionGraph.run(runner, critic=None, max_workers=1)` builds independent
nodes at the same time when `max_workers > 1` (`production-graph.md` § Graph).
The graph bounds how many nodes run at once; the runner's semaphores bound how
many of them use the AI provider or a GPU at once. Reports stay in dependency
order.

## Implementing a department

1. Add the skill pack under `src/spectracut/skills/<name>/`.
2. Define the output model (and input models) as `StrictModel`s.
3. Subclass `Department` with `name`, `produces`, `inputs`, `output`, `cost`
   and `uses_gpu`.
4. Register it with the `DepartmentRunner`, and test it end to end with
   `MockTextProvider`.

## Discovery and Development

Code: `src/spectracut/studio/departments/` (import from `discovery` and `writer`),
packs under `src/spectracut/skills/story/<department>/` (each class sets
`skill_pack`), and the copy gate in `src/spectracut/writer/copy_gate.py`.

| Department | Node kind | Inputs | Output |
| --- | --- | --- | --- |
| `intake` | `intake` | (none; node param `answers`) | `IntakeNotes`: goal, product, offer, deadline, platforms |
| `product-understanding` | `product` | `intake` | `ProductNotes`: summary, sources (`ClaimSource`), features citing a source |
| `strategist` | `brief` | `intake`, `product` | `CreativeBrief` (`spec/brief.py`) |
| `concept-writer` | `concept` | `brief` (node param `angle`) | `Concept`: title, logline, structure, fit |
| `script-writer` | `script` | `brief`, chosen `concept` | `Script`: lines of beat, VO, on-screen text and claims |

- `discovery_nodes(answers, pages)` declares `intake` -> `product` -> `brief`.
  `pages` are `{url, text}` excerpts of the site and docs; product
  understanding never fetches a URL (live capture is `packages/capture`).
- `development_nodes(brief_id="brief", n=3)` declares `concept-1`..`concept-n`,
  each with its own `angle` and reading only the brief, so
  `run(max_workers>=n)` writes them at the same time. Give `concept` a human
  gate; the concepts then wait at `awaiting_approval`.
- `pick_concept(graph, chosen_id)` approves the chosen concept, rejects every
  other concept still awaiting, and adds (or rewires) the `script` node to read
  the brief and the chosen concept. It raises `ValueError` for a node that is
  not a concept or not awaiting approval.

### Copy gate

`check_script(script, brief, forbidden_words)` returns a `Violation` for each
claim with no source id or one not among the brief's sources, and for each
forbidden word (case-insensitive, whole word or phrase) in VO, on-screen text
or claim text. `check_brief(brief, product)` flags brief sources the product
notes did not declare. `guarded(runner, graph=..., forbidden_words=...)` wraps a
graph runner: script and brief outputs are checked after the inner run, and a
violation raises `ScriptCopyGateError` (a `DepartmentOutputError`), so the node is
recorded `failed` whatever its gate mode, `auto` included.

### Claude Code text provider

`ClaudeCodeTextProvider` (`providers/claude_code.py`, name `claude-code-text`)
implements `TextProvider` with headless `claude -p --output-format json
--json-schema <schema>`, the judge's isolation flags and no tools, in an empty
scratch directory with API keys removed so only the subscription sign-in is
used. Usage is reported as `claude_requests`, `claude_input_tokens` and
`claude_output_tokens`. The real-model acceptance test is
`tests/acceptance/test_m2_discovery.py` (`SPECTRACUT_E2E=1`, post-deploy check
`m2-discovery-concepts-claude-e2e`).

## Not in scope here

A Codex text provider, the `waiting_for_ai` and `ProviderExhausted` queue
states and the Producer's estimates (#53), the skill-pack content of other
departments (#28) and Critic scoring (#30).

## Pre-production

`src/spectracut/studio/departments/art.py` declares four departments and
`preproduction_nodes()` wires them: `art-director` (kind `art_direction`: mood
board, palette, Look), `style-frame-artist` (`style_frames`), `storyboard`
(`storyboard`: a board per shot with camera and action) and `animatic-editor`
(`animatic`: an `Edit`). `PreproductionRunner` wraps the department runner:

- **Style frames** build at the final tier (the runner refuses a lower one).
  Each frame is explored on an image provider, then rendered by an injected
  renderer (Blender on the farm; the control plane never runs Blender). The node
  waits at its human gate (`PREPRODUCTION_GATES`).
- **Image exploration** uses a local model unless `image_provider = "chatgpt"` is
  set in the `[ai]` config (`storyboard.choose_exploration_provider`); opting in without
  a ChatGPT provider is an error, not a fallback.
- **Animatic** is built in code (`storyboard.build_animatic`): boards as picture
  clips, temp VO from the local TTS and optional music on audio tracks, each board
  held for the longer of its plan and its VO. `check_locked_timing` must pass.
