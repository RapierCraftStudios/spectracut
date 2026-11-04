# Core schemas

The studio's records are Pydantic v2 models. Every lane builds against them, so
they change only under the compatibility policy below. This implements spike §
Data model (`docs/engineering/technical-spike.md`). The baseline JSON Schema for
each record is committed in `src/spectracut/spec/snapshots/<name>.schema.json`.

## Common rules

All records extend `StrictModel` (`src/spectracut/spec/_base.py`):

- **Closed.** Unknown fields are rejected (`extra="forbid"`), so a typo fails
  instead of being dropped.
- **Frozen.** Records are immutable once validated.
- **Errors carry a field path.** `ValidationError.errors()[i]["loc"]` gives a
  path such as `("shots", 0, "duration_beats")`. A rule that spans several items
  of a list (unique ids, references between fields) reports the list field
  (e.g. `("proof_points",)`) and names the index in the message
  (`proof_points[1].source_id 'x' is not a declared source`).
- **Finite numbers only.** NaN and infinity are rejected.
- **Strings are trimmed.** Leading and trailing whitespace is stripped.

Shared types:

| Type | Rule |
|---|---|
| `Slug` (ids) | `^[a-z0-9][a-z0-9_-]*$`, at most 128 characters |
| `HexColor` | `#RRGGBB` |
| `ContentHash` | `sha256:` followed by 64 lowercase hex digits |
| `Version` | integer ≥ 1 |
| `UnitFloat` | float in [0, 1] |

## Records

| Name (registry key) | Model | Module |
|---|---|---|
| `audience_profile` | `AudienceProfile` | `spectracut.spec.brief` |
| `brief` | `Brief` | `spectracut.spec.brief` |
| `creative_brief` | `CreativeBrief` | `spectracut.spec.brief` |
| `look` | `Look` | `spectracut.spec.brand` |
| `brand_kit` | `BrandKit` | `spectracut.spec.brand` |
| `shot` | `Shot` | `spectracut.spec.spec` |
| `spec` | `Spec` | `spectracut.spec.spec` |
| `recipe` | `Recipe` | `spectracut.spec.recipe` |
| `license_record` | `LicenseRecord` | `spectracut.spec.asset` |
| `asset` | `Asset` | `spectracut.spec.asset` |
| `canon` | `Canon` | `spectracut.canon.schema` |

`spectracut.spec.registry.SCHEMAS` lists all of them, and `export_json_schemas()`
returns their JSON Schemas.

### AudienceProfile

| Field | Type |
|---|---|
| `personas` | list of `Persona {name, description}`, at least one |
| `prior_knowledge` | string |
| `objections` | list of non-empty strings |
| `platform_expectations` | map from platform slug to expectation |

### Brief and CreativeBrief

**Brief** has these fields:

| Field | Type |
|---|---|
| `id` | slug |
| `product` | non-empty string |
| `audience` | `AudienceProfile` |
| `sources` | list of `ClaimSource {id, title, ref}`, at least one, ids unique |
| `proof_points` | list of `ProofPoint {claim, source_id}`, at least one |
| `offer` | string |
| `cta` | non-empty string |

**CreativeBrief** is the approved output of Discovery. It has the same audience,
sources, proof points, offer and CTA, plus `message`, `tone` and
`success_measure`.

Rule: every `proof_points[i].source_id` must name a declared source. This is
the guardrail that every on-screen claim traces to a source in the brief.

### Look

| Field | Type |
|---|---|
| `id` | slug |
| `version` | `Version` |
| `grade` | `Grade`: `{cdl: CDL{slope, offset, power, saturation} \| null, lut \| null}`, at most one of the two |
| `lens` | `{chromatic_aberration, vignette, halation}` in [0, 1] and `distortion` in [-1, 1] |
| `bloom`, `grain` | `UnitFloat` |
| `motion_blur`, `depth_of_field` | bool |
| `qa_thresholds` | `{grain_max, haze_max, bloom_max}` |

Rule: `grain` and `bloom` must not exceed the look's own QA thresholds. A look
may add effects, but only within its thresholds.

### BrandKit

| Field | Type |
|---|---|
| `id` | slug |
| `name` | non-empty string |
| `palette` | `{swatches: {role: HexColor}}`, at least one swatch |
| `typography` | `{families: {role: family}, weights: [100..900]}` |
| `hero_object_mesh`, `logo` | asset id or null |
| `tone` | non-empty string |
| `looks` | list of `Look`, at least one, ids unique |
| `default_look_id` | slug |
| `voice` | `VoiceChoice {provider, voice_id, style}` or null |
| `forbidden_words` | list of strings |
| `claim_sources` | list of `ClaimSource`, ids unique |

Rules:

- `default_look_id` must be one of `looks`.
- `forbidden_words` are lowercased and de-duplicated, keeping their order.

### Shot

| Field | Type |
|---|---|
| `id` | slug |
| `template_id` | slug |
| `slots` | map from slot slug to value |
| `camera` | `{lens_mm > 0, move}` |
| `duration_beats` | > 0 |
| `notes` | string |
| `recipe_ids` | list of slugs (a Shot has 1–n recipe versions) |
| `dirty` | bool |

`content_hash` is a property, so it is not serialised.

### Spec

| Field | Type |
|---|---|
| `id` | slug |
| `format` | `tutorial`, `explainer`, `ad` or `trailer` |
| `length_s` | > 0 |
| `aspects` | unique values from `16:9`, `9:16`, `1:1`, `4:5`; at least one |
| `shots` | list of `Shot`, at least one, ids unique |
| `vo` | string |
| `captions` | bool |
| `music` | string |
| `cta` | string |
| `variant_id` | slug or null |

### Recipe

| Field | Type |
|---|---|
| `id` | slug |
| `shot_id` | slug |
| `version` | `Version` |
| `blender_version` | `major.minor[.patch]` |
| `steps` | list of `RecipeStep {index, tool, args}` |
| `parent_hash` | `ContentHash` or null |

Rules:

- **Contiguous steps.** `steps[i].index == i`.
- **Tool names are dotted identifiers** (`camera.set_lens`).
- **Arguments are JSON values.** `args` holds JSON values only.

There is no field that can carry code. Hosts run declarative recipes, never
network-supplied Python.

### LicenseRecord and Asset

**LicenseRecord** records provenance:

| Field | Type |
|---|---|
| `source` | non-empty string |
| `licence` | non-empty string |
| `url` | string or null |
| `attribution` | string |
| `non_commercial` | bool |
| `commercial_safe` | bool |

Rule: a non-commercial licence can never be `commercial_safe`, so such material
never reaches a published master.

**Asset** is a modeled object, material, rig, Look, sound or effect:

| Field | Type |
|---|---|
| `id` | slug |
| `kind` | `model`, `material`, `rig`, `look`, `sound` or `effect` |
| `version` | `Version` |
| `name` | non-empty string |
| `recipe_ref` / `file_ref` | exactly one of the two |
| `license` | `LicenseRecord`, required |
| `dirty` | bool |

Assets also expose `content_hash`.

### Canon

`Canon` (`src/spectracut/canon/schema.py`) is the story bible:

| Field | Type |
|---|---|
| `id` | slug |
| `scope` | `production` or `universe` |
| `version` | `Version` |
| `characters` | list of `CharacterSheet {id, name, model_asset_id, rig_asset_id, wardrobe_states, voice}` |
| `environments` | list of `Environment {id, name, asset_ids}` |
| `props` | list of `Prop {id, name, asset_id}` |
| `look_id` | slug or null |
| `music_themes` | list of strings |
| `continuity` | list of `ContinuityState {scene_id, time_of_day, damage, holdings}` |

Rules:

- Ids are unique within each collection.
- `holdings` maps a declared character to declared props.

A production pins the version it uses with `CanonPin {canon_id, version}`, so a
later episode can't silently change an earlier one.

## Content hashes and dirty state

`content_hash(data)` returns `sha256:` plus the SHA-256 hex digest of the record's
canonical JSON. Canonical JSON is
`json.dumps(model_dump(mode="json"), sort_keys=True, separators=(",", ":"),
ensure_ascii=False, allow_nan=False)` encoded as UTF-8. Two consequences:

- The hash does not depend on dict key order or on `PYTHONHASHSEED`.
- A golden-value test catches drift in serialisation.

Which fields are hashed:

- **`Recipe.content_hash`** covers the replay content only: `shot_id`,
  `blender_version`, `steps` and `parent_hash`. `id` and `version` do not change
  it, so two identical recipes share a frame-cache entry.
- **`Shot` and `Asset`** (`HashedModel`) hash every field except `dirty`.

`dirty` marks a record whose inputs changed since it was last built. Content
hashes and `dirty` are SpectraCut additions to the Kitsu model, and they drive
build-style change propagation in the production graph.

## Compatibility policy

`scripts/check_schema_compat.py` runs in CI. It exports every registered schema
and compares it with the committed baseline.

**Breaking (CI fails):**

- a schema removed, or a `$defs` entry removed
- a property removed
- a property newly required
- a `type`, `$ref`, `const`, `format` or `pattern` changed or added
- an enum value removed, or an enum added
- an `anyOf` branch removed
- a minimum or maximum tightened
- `additionalProperties` newly forbidden

**Compatible (CI passes with a note):**

- a new optional property
- a new schema or definition
- a new enum value
- a loosened bound
- a description change

After a compatible change, run
`uv run python scripts/check_schema_compat.py --update` and commit the refreshed
baselines. Pytest also fails while baselines are stale.

A breaking change needs a new record version and a migration for stored
records. After that, refresh the baselines with `--update` in the same PR and
call it out in the PR description.

## Kitsu mapping

SpectraCut mirrors the Kitsu/Zou data model in its own tables, so no Kitsu
server is needed. An optional Kitsu sync uses these one-to-one maps from
`src/spectracut/spec/kitsu.py`. `tests/spec/test_kitsu_mapping.py` checks that
they are bijective and that the tables below match the code.

### Entities

| SpectraCut | Kitsu/Zou |
|---|---|
| `production` | `Project` |
| `episode` | `Episode` |
| `sequence` | `Sequence` |
| `shot` | `Shot` |
| `asset` | `Asset` |
| `asset_type` | `AssetType` |
| `task` | `Task` |
| `task_type` | `TaskType` |
| `task_status` | `TaskStatus` |
| `casting` | `EntityLink` |
| `version` | `PreviewFile` |
| `comment` | `Comment` |

`EntityLink` is Zou's casting link (an asset cast in a shot). `PreviewFile` is a
reviewable version of a task's output.

### Task statuses

| SpectraCut | Kitsu/Zou |
|---|---|
| `todo` | `todo` |
| `wip` | `wip` |
| `wfa` | `wfa` |
| `retake` | `retake` |
| `done` | `done` |

These are Kitsu's default task status short names: Todo, Work In Progress,
Waiting For Approval, Retake and Done.

### Review states

A gate's review state is expressed in Kitsu as a task status:

| SpectraCut | Kitsu/Zou |
|---|---|
| `pending` | `wfa` |
| `changes_requested` | `retake` |
| `approved` | `done` |
