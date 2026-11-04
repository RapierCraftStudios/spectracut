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

## Not in scope here

Real Claude Code and Codex text providers, the `waiting_for_ai` and
`ProviderExhausted` queue states and the Producer's estimates (#53), the
skill-pack content (#28) and Critic scoring (#30).
