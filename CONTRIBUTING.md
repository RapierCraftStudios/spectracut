# Contributing to SpectraCut

Thanks for helping build SpectraCut. The design lives in [`docs/engineering/technical-spike.md`](docs/engineering/technical-spike.md); the build plan is in the milestones and issues.

## Licence and CLA

SpectraCut is AGPL-3.0-or-later and dual-licensed. Every contribution must be covered by the Contributor Licence Agreement so dual licensing stays possible; a CLA check runs on pull requests (see `CLA.md`, added by the governance work).

Every source file (`.py`, `.ts`, `.tsx`, `.js`, `.jsx`, `.mjs`, `.cjs`) starts with an SPDX header in its first five lines:

```
# SPDX-License-Identifier: AGPL-3.0-or-later      (Python)
// SPDX-License-Identifier: AGPL-3.0-or-later     (TypeScript / JavaScript)
```

CI enforces it with `scripts/check_spdx.py`.

## Setup

Requirements: [uv](https://docs.astral.sh/uv/) (Python 3.12+) and Node.js 22+.

```sh
uv sync      # Python workspace: spectracut (core) + host/ (spectracut-host)
npm ci       # TypeScript workspaces: packages/*, apps/*
```

## Checks

These run in CI on every pull request; run them before you push.

```sh
uv run python scripts/check_spdx.py
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest -q
npx tsc -p packages/capture/tsconfig.json --noEmit
npx eslint .
npx vitest run
```

## Ground rules

- Blender and `bpy` run only on render hosts. Code under `src/spectracut` must never import `bpy` or `host/bpy_runtime`.
- Hosts execute only declarative recipes, never arbitrary network-supplied Python.
- Secrets live in env or secret stores, never in specs, recipes, logs, renders or commits.
- Keep pull requests focused on one issue and include tests.
