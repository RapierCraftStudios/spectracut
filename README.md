# SpectraCut

**An agent-run CGI studio.** SpectraCut takes a request, from a 15 s ad to a full launch film or an episode, and runs it through a studio pipeline: discovery and audience research, script, storyboards, style frames, animatic, previs, Blender production on GPU render hosts, compositing, grade, motion typography, sound design and mix. Every role is a specialised AI agent, independent work runs in parallel, and every stage is reviewed by a critic before a person sees it.

It's process-driven rather than outcome-driven: direction is settled at the cheap stages, every asset is reused, and a late change re-runs only what depends on it. The bar is output quality a top studio would be proud of.

> Status: bootstrapping. The design is in [`docs/engineering/technical-spike.md`](docs/engineering/technical-spike.md); the build plan is in the milestones and issues.

## What it does
- **Agent harness:** an MCP server with typed tools for scenes, cameras, lights, materials, device screens and previews. Every action is logged into a replayable scene recipe.
- **Skill pack:** cinematography and Blender know-how for agents: shot grammar, lighting recipes, a failure-mode corpus and rubrics.
- **Storyboard → animatic → render:** image-generation boards, grey-box blocking cut to the voiceover, then approved shots become render specs.
- **Live-UI capture:** any web product, driven frame by frame on a virtual clock, with real interactions and no mockups.
- **Formats:** tutorials, explainers, 6/15/30 s ads (16:9, 9:16, 1:1, 4:5) and trailers, with voice, music, SFX, the mix and captions.
- **Quality:** automated QA (legibility, brand safety, copy, haze, flicker, loudness, repetition) plus a render → critique → revise loop.
- **Review and distribution:** frame-accurate review and platform exports, with a performance loop that learns which variants work.
- **Self-hosted or cloud:** run it on your own GPU box, or use the hosted version.

## License
SpectraCut is dual-licensed:
- **open source** under [AGPL-3.0-or-later](LICENSE);
- **commercial**, for organisations that can't comply with the AGPL. See `COMMERCIAL.md` (coming with the governance issue).

Contributions require signing the CLA, so dual licensing stays possible.

A [RapierCraft Studios](https://github.com/RapierCraftStudios) project.
