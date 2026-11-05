---
name: spectracut-demo
description: After a PR deploys to staging, capture a short clip of the changed flow with SpectraCut (live-UI capture plus an ffmpeg mux, no Blender) and attach it to the PR. Use when the PR, or an issue it closes, has a demo block.
---
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# spectracut-demo

Turns a PR's `demo` block into a short MP4 of the changed flow on staging and
attaches it to the PR. It is part of the optional SpectraCut ForgeDock pack.
SpectraCut never imports or reads ForgeDock.

## When to run

Run it once the PR's staging deploy is live, and again after each new staging
deploy of the same PR. A rerun replaces the clip and edits the same comment.
If neither the PR body nor any issue it closes has a demo block, the skill logs
`no demo block`, attaches nothing and exits 0.

## Command

```bash
node integrations/forgedock/skills/spectracut-demo/demo.mjs \
  --pr <N> --staging-url <https://staging.example.com> [--repo owner/repo] [--dry-run]
```

- `--repo` defaults to the current checkout's GitHub repo.
- `--dry-run` captures and muxes the clip, then stops before any GitHub write.
- Exit 0 means a clip was attached, or there was no demo block, or the run was
  a dry run. Exit 1 means an error, with the reason on stderr. Report it; don't retry blindly.

## Demo block

The PR body is checked first, then each issue the PR closes. The first match wins.

A fenced `demo` block holds JSON with an optional `path` (default `/`) and
`fps` (default 30, max 60), an optional `viewport` (default 1280x720) and a
non-empty `script` in the `@spectracut/capture` interaction DSL (`goto`,
`move`, `click`, `drag`, `type`, `scroll`, `wait`):

````markdown
```demo
{
  "path": "/pricing",
  "fps": 30,
  "script": [
    { "do": "move", "to": { "selector": "#start-trial" }, "durationMs": 600 },
    { "do": "click" },
    { "do": "wait", "ms": 800 }
  ]
}
```
````

A recorded interaction script can be used instead. Add a line
`demo: path/to/script.json` naming a repo-relative JSON file, either an
action array or a full spec as above. It is read from the PR's head commit.

`path` and every `goto` URL are staging paths. They are resolved against
`--staging-url`, and a script can't leave the staging origin. Scripted
time is capped at 30 s.

## What it does

1. `gh pr view` reads the PR body, head commit and closing issues. `gh issue view` reads each closing issue.
2. `@spectracut/capture` `launchAndCapture` records the flow in headless Chromium on a virtual clock.
3. `ffmpeg` muxes the frames into H.264 MP4 (`yuv420p`, `+faststart`), named `pr-<N>-<sha7>.mp4`.
4. `gh release upload --clobber` puts it on the `spectracut-demos` prerelease, which is created if missing.
5. It creates or edits the single PR comment marked `<!-- spectracut-demo -->` ("SpectraCut demo clip") that links the clip.

## Guardrails

- No Blender or bpy. Only capture and ffmpeg run, on the ForgeDock host.
- Secrets come only from env or secret stores, never from the demo block. Set `"secret": true` on any `type` action that types a credential so its keys are masked in events. Logged URLs drop credentials and query strings.
- Capture only the product under review. No real third-party brands, logos or customer data on screen unless the owner supplied and licensed them.
- Clips are review material for the PR only. Never publish them anywhere else.
