# Skill packs

A skill pack is a department's craft knowledge: its system prompt, the rubric
the Critic scores it against, a corpus of past failures and taste anchors. Each
department has one folder:

```
skills/<department-name>/
  prompt.md        required: the system prompt and craft knowledge
  rubric.md        required: what the Critic scores the output against
  failures/*.md    optional: failure corpus (what went wrong before, and why)
  anchors/*.md     optional: taste anchors (references the output should match)
```

`prompt.md` and `rubric.md` must exist and must not be empty.
`SkillPack.load(path)` raises `ValueError` naming the path when the folder or
either file is missing.

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

The Discovery and Development departments (`spectracut.studio.departments`)
keep their packs together under `skills/story/<department>/`: `intake`,
`product-understanding`, `strategist`, `concept-writer` and `script-writer`.
Each of those classes sets `skill_pack` to its folder.
