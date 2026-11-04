# Production graph

Every artifact the studio makes (brief, script, board, asset, shot, render, mix)
is a node in a production graph. The engine stores nodes with typed inputs and a
content hash, marks dependants dirty when something changes, runs each node's
gate, carries the render tier per node, and records the cost of every
department run in a ledger. This implements spike § The studio: a production
graph run by agent departments (`docs/engineering/technical-spike.md`).

Code: `src/spectracut/studio/` (`node.py`, `graph.py`, `gates.py`, `ledger.py`,
`profiles.py`, `profiles/*.json`). Records extend `StrictModel` and hash with
`content_hash` from `spectracut.spec._base` (see `schemas.md`). The engine is pure
Python: departments and the Critic are injected callables, and it never imports
Blender or calls a network service.

## Node

`NodeSpec` declares a node:

| Field | Type | Meaning |
|---|---|---|
| `id` | `Slug` | unique within the production |
| `kind` | `Slug` | artifact kind; matches a quality-profile stage (e.g. `brief`, `shots`, `render`) |
| `department` | `Slug` | the department (subagent) that makes it |
| `inputs` | tuple of node ids | upstream nodes whose outputs it reads; no duplicates, not itself |
| `params` | JSON object | the node's own declarative inputs (finite numbers only) |
| `tier` | `Tier` or null | explicit render tier; null takes it from the quality profile |

`Tier`: `preview`, `review`, `final`, `hero` (spike § Quality tiers). A node's
tier is `spec.tier`, else the profile's `tier[kind]`, else `preview`.

### Key and content hash

A node's **key** is `content_hash` (`sha256:<hex>` over canonical JSON) of:

```json
{"kind": ..., "department": ..., "params": ..., "tier": ..., "inputs": {"<input id>": "<input output hash>"}}
```

The node id is not in the key, so identical work in two nodes is done once.
A node's **output hash** is `content_hash` of the JSON output its department
returned. Keys chain through output hashes, so a node's key changes exactly when
something it depends on produces different content.

### Status

`NodeState` is kept by the graph (spec records stay frozen):
`status`, `tier`, `key`, `output_hash` (set only when clean), `attempts`, `error`.

| Status | Meaning | Dependants run? |
|---|---|---|
| `pending` | added, never built | no |
| `dirty` | it or an upstream node changed since it was built | no |
| `running` | its department is building it | no |
| `awaiting_approval` | built; a human gate is waiting for a person | no |
| `clean` | built, gate passed, output current | yes |
| `rejected` | the Critic (out of revisions) or a person rejected it | no |
| `failed` | the department raised an error (recorded in `error`) | no |

## Graph

`ProductionGraph(production_id, profile=None, gates=None, ledger=None)`.

- **Structure.** `add_nodes` takes nodes in any order. Duplicate ids, unknown
  inputs and cycles raise `ValueError` naming the node, and a rejected change
  leaves the graph unchanged. `order()` is a stable dependency order.
- **Dirty propagation.** `set_params(id, params)`, `update_node(spec)` and
  `invalidate(id)` mark the changed node and exactly its transitive dependants
  `dirty` and return that set. Nothing else changes state. An update that leaves
  the spec equal marks nothing.
- **Run.** `run(runner, critic=None)` visits nodes in dependency order and builds
  each `pending` or `dirty` node whose inputs are all `clean`. Others are
  reported as `blocked`. For each node it computes the key:
  - **Reuse.** If an output was already accepted under that key, the node becomes
    `clean` with that output. The department is not called and no ledger entry is
    written. Re-running a clean graph therefore does no work, and a change undone
    (or a forced rebuild with the same inputs) is served from the cache.
  - **Build.** Otherwise the runner is called with a `RunContext` (`spec`, `tier`,
    `key`, `inputs` as the accepted outputs of the inputs, `attempt`, `feedback`)
    and returns `RunResult(output, usage)`. The output then passes the node's gate.
- `RunReport` lists node ids as `executed`, `reused`, `awaiting_approval`,
  `rejected`, `failed` and `blocked`.

## Gate

`GateMode`: `auto`, `critic`, `human`. `GatePolicy` resolves a node's mode as:

1. `nodes[node id]`: per-node override for this production;
2. `production[kind]`: per-production override by node kind;
3. `defaults[kind]`: the quality profile's `gates`;
4. otherwise `auto`.

`QualityProfile.gate_policy(production=..., nodes=...)` builds a policy from a
profile plus overrides. Without an explicit policy, the graph uses its profile's.

| Mode | Behaviour |
|---|---|
| `auto` | accepted when built; the Critic is not consulted |
| `critic` | `critic(spec, output) -> CriticVerdict(passed, scores, comment)`. A fail re-runs the department with the verdict as `feedback`, up to the profile's `revision_budget[kind]` extra attempts, then the node is `rejected` |
| `human` | the Critic reviews first, as for `critic`, when one is passed; then the node is `awaiting_approval` and its dependants are blocked. `approve(id)` makes it `clean` and caches the output, and its dependants run on the next `run`. `reject(id)` makes it `rejected`; change it or `invalidate(id)` to rebuild |

With no Critic passed to `run`, `critic` gates accept as `auto`. Every gate
outcome is kept as a `GateDecision` (`mode`, `critic` verdict, `human`
decision, `comment`), readable through `decision(id)`. Only accepted outputs
(passed gates or approved) enter the reuse cache.

## Quality profiles

Profiles are data files: `src/spectracut/studio/profiles/<name>.json`, validated
by `QualityProfile` and loaded with `load_profile(name)`.

| Field | Type | Meaning |
|---|---|---|
| `name` | `Slug` | equals the file name |
| `description` | string | when to use it |
| `stages` | list of stage slugs | the stages (node kinds) this profile runs, in order |
| `revision_budget` | stage → int ≥ 0 | Critic-requested revisions per stage, after the first attempt |
| `tier` | stage → `Tier` | render tier per stage |
| `gates` | stage → `GateMode` | gate per stage; unlisted stages are `auto` |

Every stage needs a `revision_budget` and a `tier` entry. A table that names a
stage not in `stages` is rejected, with a field path.

| Profile | Adds | Top tier | Gates |
|---|---|---|---|
| `draft` | brief, script, template shots, VO, edit, render | preview | all auto |
| `standard` | + concept, style frames, storyboard, animatic, shots, sound design, mix | final | people approve brief, concept, style frames, animatic and delivery; Critic elsewhere |
| `premium` | + audience research, previs, custom assets, animation polish, FX, grade; larger budgets | final | as standard |
| `cinema` | + reference analysis, immersive mix; extra critique rounds | hero | as standard, plus a human animation-polish gate |

Each profile's stages include every stage of the cheaper ones.

## Ledger

Each department run (each attempt, including revisions and failures) appends one
`LedgerEntry`. Cache hits are not runs and write nothing.

| Field | Type | Meaning |
|---|---|---|
| `production_id`, `node_id`, `kind`, `department` | `Slug` | what ran and who ran it |
| `node_key` | `ContentHash` | key of the node build |
| `tier` | `Tier` | tier it ran at |
| `attempt` | int ≥ 1 | 1 for the first attempt, then one per revision |
| `started_at` | datetime (UTC) | start of the run |
| `duration_s` | float ≥ 0 | wall time in seconds |
| `gpu_minutes` | float ≥ 0 | GPU minutes reported by the department |
| `ai_usage` | metric slug → int ≥ 0 | AI usage, e.g. `claude_input_tokens`, `claude_requests` |
| `elevenlabs_characters` | int ≥ 0 | ElevenLabs characters billed |
| `outcome` | `accepted`, `revise`, `rejected`, `awaiting_approval`, `failed` | what the gate did with this attempt |

Departments report usage through `RunResult.usage` (`Usage`: `gpu_minutes`,
`ai_usage`, `elevenlabs_characters`). `Ledger.totals(node_id=None)` sums runs,
duration, GPU minutes, AI usage and ElevenLabs characters for the production or
for one node. `Ledger.to_jsonl()` exports canonical JSON lines.

The ledger is the base of the step ledger (spike § Learning which steps add
quality). Critic scores before and after are carried on the `GateDecision`.

## Not in scope here

Persistence, scheduling in parallel across GPU leases, and the Producer's
estimates are built on this contract in later issues. Secrets never appear in
params, outputs or ledger entries.
