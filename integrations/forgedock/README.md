# ForgeDock integration (optional)

SpectraCut as a tool in the ForgeDock dev pipeline: a skill pack that ForgeDock
agents run, which calls SpectraCut's capture and CLI and writes to GitHub through `gh`.

SpectraCut never depends on ForgeDock. Nothing here is imported by the
`spectracut` package, shipped in its wheel, or part of the npm workspaces.
Without ForgeDock this directory is inert. See
`docs/engineering/technical-spike.md` § ForgeDock: SpectraCut as a tool in the
dev pipeline.

## Install

Only install the pack if ForgeDock is present (`spectracut doctor` reports
`forgedock: on`). Copy or symlink each skill folder into the agent's skills
directory, for example:

```bash
ln -s "$PWD/integrations/forgedock/skills/spectracut-demo" ~/.claude/skills/spectracut-demo
```

## Skills

### spectracut-demo

After a PR deploys to staging, it captures a short clip of the changed flow
(live-UI capture plus an ffmpeg mux, no Blender) and attaches it to the PR. The
flow comes from a fenced `demo` JSON block in the PR or an issue it closes,
or from a `demo: <path>.json` line naming a recorded interaction script. See
[`skills/spectracut-demo/SKILL.md`](skills/spectracut-demo/SKILL.md) for the
block format and the guardrails.

Prerequisites on the host that runs it:

- ForgeDock, and `gh` authenticated for the target repo (it needs release upload and PR comment).
- Node 20+ with `@spectracut/capture` resolvable: build it in this repo (`npm ci && npm run build -w @spectracut/capture`) or install it from npm.
- A Chromium that `playwright-core` can launch.
- `ffmpeg` on `PATH`.

Usage:

```bash
node integrations/forgedock/skills/spectracut-demo/demo.mjs --pr 123 --staging-url https://staging.example.com
```

The clip goes onto the repo's `spectracut-demos` prerelease as
`pr-<N>-<sha7>.mp4`. The PR gets one comment marked `<!-- spectracut-demo -->`,
which each rerun edits.
