# Skill packs

A skill pack is a department's craft knowledge: its system prompt, the rubric
the Critic scores it against, a corpus of past failures and taste anchors. Each
department has one folder:

```
skills/<department-name>/
  prompt.md        required: the system prompt and craft knowledge
  rubric.md        required: what the Critic scores the output against
  rubric.json      the same criteria as weighted JSON, scored by the benchmark runner
  failures/*.md    optional: failure corpus (what went wrong before, and why)
  anchors/*.md     optional: taste anchors (references the output should match)
```

`prompt.md` and `rubric.md` must exist and must not be empty.
`SkillPack.load(path)` raises `ValueError` naming the path when the folder or
either file is missing.

## M2 departments

`spectracut.skills.M2_DEPARTMENTS` lists the departments that ship a pack:
`writer`, `director`, `art-director`, `storyboard`, `editor`, `lighter`,
`scene-builder`, `compositor`, `motion-designer`, `sound-designer`, `mixer` and `critic`.
`spectracut.skills.pack_dir(name)` is a pack's folder.

## Rubrics and the benchmark runner

`rubric.json` uses the benchmark rubric schema
(`spectracut.critique.benchmarks.Rubric`):
`{"id": "<department>", "title": "...", "criteria": [{"id", "prompt", "weight"}]}`.
Its `id` is the department name, and every criterion id is also a heading in
`rubric.md`. When a benchmark piece names a rubric id that is not in
`benchmarks/rubrics/`, the runner loads the department's `rubric.json`. The
`departments` benchmark set (`benchmarks/sets/departments/`) has one piece per
department, so `spectracut bench check departments` scores every pack rubric.

## Failure corpus

Each failure file says what went wrong, how to detect it and the fix. The
AlterLab trailer's failures are in the pack of the department that owns them,
and all of them are in the Critic's pack:

| failure | owner |
|---|---|
| `haze` | `lighter` |
| `dim-screens` | `lighter` |
| `static-ui` | `motion-designer` |
| `real-brand-leaks` | `art-director` |
| `repeated-push-ins` | `director` |

## How a department points at its pack

A `Department` subclass (`spectracut.studio.department`) sets `name`, and its
pack is `spectracut/skills/<name>/` by default. Set the class variable
`skill_pack` to a folder path to use another location (tests do this with
fixture packs). `Department.pack()` loads and caches the pack,
`Department.rubric()` returns the rubric text, and the department runner sends
`prompt.md` as the system prompt of every task. See
`docs/engineering/contracts/departments.md`.

## Rules

- Packs are markdown only. They hold no executable code, and nothing in them is
  run or imported.
- Packs never contain secrets: no API keys, tokens, account names or private
  URLs. They are shipped with the package and sent to AI providers as prompts.

## Story departments

The Discovery and Development departments (`spectracut.studio.departments.discovery` and `.writer`)
keep their packs together under `skills/story/<department>/`: `intake`,
`product-understanding`, `strategist`, `concept-writer` and `script-writer`.
Each of those classes sets `skill_pack` to its folder.
