# Technical Spike: SpectraCut

> Written by /bootstrap on 2026-11-02. Every decision is followed by an ADR-style `**Rationale**:` line (the decision, why, and the rejected alternative).

SpectraCut is an agent-native video and ad engine. AI agents direct headless Blender on GPU render hosts, capture any live web product frame by frame, and assemble tutorials, explainers, ads in every aspect ratio, and cinematic trailers. Every render passes an automated QA gate and a vision critique loop before a human reviews it. It is product-agnostic ("it can market even a spelling checker") and ships as self-hosted open source (AGPL-3.0, dual-licensed commercially), plus a hosted cloud version.

**Vision and quality bar.** SpectraCut is an extremely high-quality media producer. Its primary job is to be the marketing engine for the products RapierCraft builds. It is also a product in its own right, released as open source. The target output is what a top product studio makes:
- photoreal 3D device mockups with the real, live UI on screen;
- full launch films with motion typography, editing, sound design, CGI effects and finishing (grade, lens, post);
- the same quality cut down into ads for every platform.

**First use and long-term aim:**
- **First:** all of AlterLab's marketing. That covers SaaS launch films, product demos and walkthroughs, feature announcements and ads.
- **Long-term:** an original cinematic universe of episodes and films sharing characters and worlds. Canon therefore spans productions, not just one film.
- **The bar for both:** the output has to be that good, or none of the rest matters.

Nothing in the architecture caps it at short ads. Long-form and film-length work is a matter of scale (more shots, longer timelines), not a different system. Every quality decision is judged against "would this pass in a launch film from a top studio?"

It grows out of the AlterLab launch-trailer pipeline (RapierCraftStudios/AlterLab#38096), which proved each piece by hand:
- deterministic live-UI capture;
- a Blender crystal and devices on a single RTX 3070;
- ElevenLabs VO;
- Codex storyboards;
- a director's grammar for shot variety;
- a list of failure modes (haze, dim screens, static UI, real-brand leaks, the same push-in on every shot).

## Assumptions

- **Users:** marketers and founders who want product videos without a studio; developers and agents that generate video through the MCP server; operators of render hosts. Admins run the hosted cloud.
- **Core jobs:**
  - brief → storyboard → animatic → approved renders;
  - capture a live product;
  - generate an ad matrix (hook × visual × CTA × aspect);
  - review and approve;
  - export to platforms;
  - learn from performance.
- **Scope:** the full product in one plan. AlterLab's launch film comes first (owner decision, 2026-11-04), with ads and demos as cut-downs of it; then premium and cinema depth; then long-form, review, distribution and cloud.
- **Platforms:**
  - a CLI;
  - an MCP server;
  - a local web review UI;
  - a hosted web app for the cloud;
  - render hosts on Windows or Linux with NVIDIA GPUs.
- **Stack:** Python core (closest to the "Python API" preset) plus TypeScript for capture and the web UI; see Stack.
- **Auth:**
  - local mode needs none; it binds to localhost and keeps a token for the MCP and HTTP API;
  - cloud mode uses email magic link plus Google (Auth.js) and API keys for agents.
- **Data and tenancy:**
  - local: SQLite plus the filesystem;
  - cloud: Postgres with row-level security per workspace, and S3-compatible object storage for media.
- **Integrations:**
  - ElevenLabs (VO, SFX);
  - AI through the user's own subscriptions: ChatGPT through the official Codex CLI sign-in (text, image generation) and Claude through the official Claude Code sign-in (text, vision, agents). OAuth means no API keys, with keys as an optional fallback;
  - Playwright;
  - Blender 4.5 LTS;
  - ffmpeg;
  - platform exports (YouTube, Meta, TikTok, X, LinkedIn, Reddit);
  - Stripe for the cloud;
  - optional, off by default: tokenbaton (AI capacity across several Claude accounts), computeshare (jobs on spare tailnet machines, including GPU slices) and ForgeDock (skills and failure issues). See Optional integrations.

  Every credentialed integration has a mock provider.
- **Payments:** the cloud uses Stripe usage-based billing (render minutes plus generations) behind a mock in dev. The OSS edition is free; commercial licences are sold off-platform.
- **i18n:** English UI with i18n-ready strings. Captions and VO are multi-language capable later.
- **Compliance:**
  - privacy by default; no end-user PII beyond accounts;
  - uploaded brand assets are customer-owned and deletable;
  - every generated media asset carries licence metadata (voice, music, imagery).
- **Design:** polished brand. The review UI uses shadcn/ui with a dark-first token theme. The marketing site hero routes to `/design`.
- **Deployment:**
  - OSS: PyPI and npm packages plus Docker images on GHCR;
  - cloud: Docker on a VPS for the control plane, with GPU workers (registered hosts or rented GPUs);
  - a staging → main flow.
- **Scale (year one):** tens of self-hosted installs and early cloud workspaces. Rendering is the bottleneck, not the control plane.
- **Budget:** free or hobby tiers until the cloud launch. GPU rendering on owner hardware (legion) for development.
- **Team:** solo maintainer with ForgeDock agents.
- **Timeline:** no hard date; milestones in build order. AlterLab is the first customer (its ads).

## Guardrails

- **Rendering runs only on registered render hosts.** The control plane never imports `bpy` or launches Blender locally. A host guard refuses unregistered execution.
- **No real third-party brands, logos, customer names or OEM marks** (e.g. a laptop maker's logo) in any generated frame unless the brief's owner explicitly supplies and licenses them. The QA gate enforces this.
- **Copy safety:** every on-screen claim and number traces to a source in the brief. Per-brand forbidden words are enforced. No competitor disparagement beyond sourced comparisons.
- **Clarity over atmosphere** by default:
  - legible screens exposed for the UI;
  - no haze or veiling glow;
  - minimal grain;
  - one readable subject per shot.

  A look (see Production pipeline) may add bloom, grain, lens effects or atmosphere, but only within that look's QA thresholds, and UI legibility always wins over the look.
- **Commercial licensing:** media is marked "commercial-safe" only when every voice, music, SFX and image source carries a commercial licence. Exports for paid ads require commercial-safe.
- **Secrets** live only in environment or secret stores; never in specs, recipes, logs or renders.
- **AGPL-3.0-or-later** for all code, with a CLA on every contribution so dual licensing stays possible. Brand kits and customer assets are not part of the codebase.

## Stack

**Core (`spectracut` Python package):**
- Python 3.12 with uv, Typer (CLI), Pydantic v2 (spec and recipe schemas), FastAPI (local and cloud API), SQLAlchemy plus Alembic (SQLite locally, Postgres in the cloud).
- The official MCP Python SDK for the agent harness. structlog for logging.

**Render host agent (`spectracut-host`):**
- A small Python service installed next to Blender 4.5 LTS on each GPU host.
- It runs Blender headless (`blender -b --factory-startup -P`) with SpectraCut's bpy runtime, and pulls jobs over an authenticated channel: SSH for self-hosted, HTTPS long-poll in the cloud.

**Other components:**
- **Capture (`@spectracut/capture`):** TypeScript and Playwright (Chromium or Edge) with a virtual clock (`page.clock`), producing PNG sequences and DOM-layer exports.
- **Web (`apps/web`):** Next.js App Router, shadcn/ui plus Tailwind, Auth.js (cloud), the review UI and cloud dashboard, Stripe (cloud).
- **Media:** ffmpeg for encode and mux, pyloudnorm for loudness, OpenCV and Pillow for QA metrics, and Tesseract or a vision model for OCR.

**Rationale**:
- Python is where Blender (bpy), the MCP SDK and the media tooling all live. Playwright and the web UI are strongest in TypeScript.
- Rejected: an all-TypeScript core, which would need a Python bridge for every Blender call; and an Electron app, which is heavy and blocks agent and CLI use.

## Repo layout

```
spectracut/
  pyproject.toml                 # uv workspace (core + host)
  src/spectracut/
    cli/                         # Typer commands: plan, board, render, review, serve, hosts, export
    spec/                        # Pydantic schemas: Spec, BrandKit, Brief, Recipe, Shot, Variant
    harness/                     # MCP server + typed tool layer (scene, camera, light, material, screen, inspect, preview)
    recipes/                     # replayable scene recipes, (de)serialisation, hashing
    studio/                      # production graph, departments (subagents), Producer, gates, quality profiles, step ledger
    canon/                       # story bible, universe canon, continuity checks
    edit/                        # Edit timeline model, OpenTimelineIO import/export, conform
    finishing/                   # color management, Looks, compositing graphs, motion typography
    queue/                       # job queue, executors (host pool, computeshare), cache, host registry
    templates/                   # shot template library (Python definitions + assets manifest)
    skills/                      # cinematography + Blender skill pack (markdown prompts, recipes, rubrics, failure corpus)
    storyboard/                  # treatment, boards (image providers), blocking animatic, spec bridge
    critique/                    # vision judge, rubric scoring, revise loop
    writer/                      # brief -> hooks/scripts/VO/shot lists, copy gate
    audio/                       # AudioRecipe, Ardour session writer, SuperCollider cues, music editing, mix, audio critic, captions
    formats/                     # tutorial, explainer, ad, trailer assemblers + edit-rule linter
    qa/                          # automated QA checks + report
    distribution/                # export presets, UTMs, metrics ingest, variant ranking
    providers/                   # pluggable providers (+ mocks): tts, sfx, image, vision, storage
    api/                         # FastAPI app (local + cloud)
    cloud/                       # tenancy, billing (Stripe), workers, quotas
  host/                          # spectracut-host (render host agent) + bpy runtime (runs inside Blender only)
    spectracut_host/
    bpy_runtime/
  packages/capture/              # @spectracut/capture (TypeScript, Playwright)
  integrations/forgedock/        # optional ForgeDock skills (demo, release) and failure-issue writer
  apps/web/                      # Next.js review UI + cloud dashboard + marketing site
  brands/examples/spellwise/     # fictional demo brand kit + demo app for acceptance
  docs/engineering/              # this spike, ADRs
  docs/setup/                    # credentials / host setup guides
  tests/                         # pytest (unit, integration with mock providers)
  .github/workflows/             # ci.yml, release.yml, deploy-staging.yml
```

**Rationale**: one repo holds the engine, the host agent, capture and the UI, so a spec change ships atomically across them. The bpy runtime is isolated under `host/`, so the control plane cannot import Blender (a guardrail). Rejected: separate repos per component, whose coordination cost is too high for a solo maintainer.

## Runtime topology

**Self-hosted (day one):**
- `spectracut serve` on a workstation runs the API, the MCP server, the queue (SQLite) and the review UI on localhost.
- One or more render hosts run `spectracut-host`, registered by SSH key, and pull jobs one GPU job at a time.
- Media lives on local disk, with a content-addressed frame cache keyed by recipe hash.

**Cloud, next step:**
- A control plane (API, web, queue on Postgres, Redis for job leases) on a VPS.
- GPU workers: customers' registered hosts, or a managed pool of rented GPU instances.
- Media in S3-compatible storage (Cloudflare R2), served by a CDN.

**At scale:**
- Worker autoscaling on queue depth.
- Per-workspace render quotas.
- Regional storage buckets.

**Rationale**:
- Rendering dominates cost and latency, so the design centres on a pull-based GPU worker pool that works the same with one home GPU or a fleet. The control plane stays small.
- Rejected:
  - push-based SSH-only execution, which can't cross NAT for customer hosts in the cloud;
  - serverless GPU, which has cold starts too slow for interactive preview loops.

## Data model

| Entity | What it is |
|---|---|
| **Workspace** | cloud tenant; local mode has one implicit workspace |
| **User**, **Membership** | cloud only |
| **ApiKey** | agents and CLI |
| **BrandKit** | palette, type, hero object mesh, logo, tone, forbidden words, claim sources, voice choice |
| **Brief** | product, audience, proof points with sources, offer, CTA |
| **Project** | one campaign or film |
| **Treatment** | concept, acts, logline |
| **Shot** | template id, slots, camera, duration in beats, notes |
| **Board** | image per shot, provider, status: concept, approved, real render |
| **Spec** | format, length, aspects, shots, VO, captions, music, CTA, variant_id |
| **Recipe** | replayable tool-call log for a scene; content hash |
| **Job** | kind: capture, preview, final, mix, export; host; status; timings; cost |
| **Render** | frames, video, sheet; QA report; critique scores |
| **Variant** | spec plus renders plus export ids/UTMs |
| **MetricSample** | platform, variant, views, retention, CTR, cost, signups |
| **Comment** | frame-accurate, on a render or board |
| **LicenseRecord** | per asset: source, licence, commercial-safe |
| **Production** | one film or campaign run through the studio; quality profile, budget, gate settings |
| **CreativeBrief** | the approved output of Discovery: message, audience, proof points, tone, success measure |
| **AudienceProfile** | personas, prior knowledge, objections, platform expectations |
| **Node** | one artifact in the production graph: department, inputs, content hash, status, tier |
| **Gate** | review on a node: Critic scores, human decision, comments |
| **Asset** | modeled object, material, rig, Look, sound or effect recipe; versioned and reusable across productions |
| **StepRecord** | step ledger entry: department, step, cost, duration, scores before and after, accepted |

Relations:
- Workspace 1–n everything.
- Project 1–n Spec; Spec 1–n Shot; Shot 1–n Recipe version.
- Render n–1 Job; Variant 1–n MetricSample.

Retention: media is kept until the project is deleted; caches are evicted LRU by quota.

**Rationale**: specs and recipes are the source of truth and renders are derived and cacheable, so anything approved can be re-rendered exactly. Rejected: storing `.blend` files as the source, which can't be diffed or replayed and drifts from intent.

## Security and privacy

- Local mode binds to 127.0.0.1 with a generated token. The cloud uses Auth.js sessions plus scoped API keys with per-key render quotas.
- Render hosts authenticate with per-host keys. Jobs are signed. Hosts execute only SpectraCut's bpy runtime with recipe data, never arbitrary Python from the network, and run with no network access during renders.
- **Postgres RLS by workspace** (cloud). Media URLs are signed and short-lived.
- Secrets come from env or a secret manager only. Captures mask configured selectors (API keys, emails).
- Brand assets are customer-owned, deletable with the project, and never used to train anything.
- No customer data leaves for third-party providers beyond what each provider call needs (VO text, board prompts). This is disclosed in the docs.

**Rationale**: the dangerous surface is remote code execution on GPU hosts and leaking customer brand material, so execution is limited to a fixed runtime fed declarative recipes. Rejected: letting agents send raw bpy code to hosts, which is flexible but unsafe and non-replayable.

## Design system and quality gates

- **Web UI:**
  - shadcn/ui plus Tailwind with a dark-first token theme (the media review reads best on dark);
  - WCAG 2.2 AA;
  - reduced-motion support;
  - frame-accurate players.
- **Video output quality** is a first-class gate: the QA checks (legibility, brand safety, copy, haze, grain, flicker, loudness, safe areas, shot repetition) and the critique rubric.
- **CI:**
  - Python: ruff, mypy, pytest with mock providers;
  - TypeScript: tsc, ESLint, Vitest;
  - a Playwright smoke test of the review UI;
  - a recipe-replay determinism test (headless Blender in CI on CPU at tiny resolution, using the Eevee/Workbench fallback);
  - a schema compatibility check.

**Rationale**: the product's value is the quality of the output, so output quality gets automated gates, not just code quality. Rejected: relying on human review alone, which the AlterLab trailer showed catches problems late and costs many render rounds.

## Route/API map

| Route or endpoint | Purpose | Auth |
|-------------------|---------|------|
| MCP `spectracut` server: `scene.*`, `camera.*`, `light.*`, `material.*`, `screen.*`, `inspect.*`, `preview.*`, `queue.*`, `board.*`, `critique.*` | Agent harness tools | local token / API key |
| `POST /v1/briefs`, `GET /v1/briefs/{id}` | Briefs | session / API key |
| `POST /v1/projects/{id}/plan` | Writer → treatment, specs | session / API key |
| `POST /v1/projects/{id}/boards` | Storyboard frames | session / API key |
| `POST /v1/specs/{id}/render` (preview/final) | Queue renders | session / API key |
| `GET /v1/jobs/{id}`, `GET /v1/jobs?status=` | Queue status | session / API key |
| `POST /v1/captures` | Live-UI capture job | session / API key |
| `GET /v1/renders/{id}` (+ `/qa`, `/critique`) | Render, QA report, critique | session / API key |
| `POST /v1/renders/{id}/comments`, `POST /v1/variants/{id}/approve` | Review | session |
| `POST /v1/variants/{id}/export` | Platform exports | session / API key |
| `POST /v1/metrics/ingest` | Performance data | API key |
| `POST /v1/hosts/register`, `POST /v1/hosts/lease`, `POST /v1/hosts/jobs/{id}/result` | Render host protocol | host key |
| `/` (marketing), `/app/*` (review UI, cloud dashboard), `/settings/billing` | Web | public / session |
| `POST /v1/billing/webhook` | Stripe (cloud) | Stripe signature |

**Rationale**:
- The MCP tools are the primary interface for agents, and the HTTP API mirrors them for the CLI and UI, so every action is scriptable.
- The host protocol is pull-based so it works behind NAT.
- Rejected: GraphQL, which is more complexity than one client set needs.

## Credentials

| Credential or account | Env var(s) | needs-human issue | Mock in dev/CI |
|-----------------------|------------|-------------------|----------------|
| ElevenLabs API (paid plan, commercial licence) | `ELEVENLABS_API_KEY` | ElevenLabs paid plan and API key | `providers/mock_tts.py` (silent WAV with timed markers) |
| AI providers: ChatGPT (images, text) and Claude (vision, text, agents) | none by default: the user's subscription sign-in via the official Codex CLI and Claude Code (OAuth); optional `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` fallback | none (the AI provider layer issue) | `providers/mock_image.py`, `providers/mock_vision.py` |
| Music licensing source | `MUSIC_LIBRARY_API_KEY` | Music licensing source | `providers/mock_music.py` (generated click track) |
| Domain + DNS (spectracut.com, .dev, .io) | `CLOUDFLARE_API_TOKEN` | Domains and DNS | n/a (localhost) |
| Cloud VPS + GPU worker account | `DEPLOY_SSH_KEY`, `GPU_PROVIDER_API_KEY` | Cloud hosting and GPU account | local host agent |
| Object storage (R2) | `S3_ENDPOINT`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY` | Object storage bucket | local filesystem storage |
| Stripe (cloud billing) | `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET` | Stripe account | mock billing provider |
| Google OAuth (cloud sign-in) | `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | Google OAuth client | magic-link dev mailer |
| Package publishing (PyPI, npm trusted publishing) | `PYPI_TRUSTED_PUBLISHING`, `NPM_TOKEN` | Package registry publishing | `--dry-run` builds |
| CLA bot + trademark clearance | `CLA_ASSISTANT_GIST_URL` | CLA and trademark | n/a |
| Ad platform accounts (YouTube, Meta, TikTok, X, LinkedIn, Reddit) | `YOUTUBE_API_KEY`, `META_ACCESS_TOKEN` | Ad and analytics platform accounts | `providers/mock_metrics.py` |

**Rationale**: AI calls ride the user's existing ChatGPT and Claude subscriptions through the official CLIs' OAuth sign-in, so a new user needs no API keys and SpectraCut never stores AI tokens. For the hosted cloud, provider terms are checked before subscription sign-in is offered; until then the cloud uses each workspace's own keys. Each remaining external input is something agents cannot create (accounts, payments, legal). Mocks keep every code path testable in dev and CI, so no build issue blocks on a human.

## The studio: a production graph run by agent departments

SpectraCut works the way a CGI studio makes a film entirely in Blender, from understanding what the client wants through to final delivery. Every role is a specialised subagent instead of a person, and independent work runs in parallel. A person can step in at any stage but doesn't have to.

**Positioning: process-driven, not outcome-driven.**
- **Video generation** iterates on outcomes. Each attempt regenerates a whole clip and hopes it comes out right, and fixing one detail usually means regenerating everything.
- **SpectraCut** iterates on the process:
  - direction is settled at the cheap stages (brief, script, boards, animatic, previs) before expensive rendering;
  - every artifact is kept and reused (assets, looks, rigs, sounds);
  - a late change re-runs only what depends on it: re-grade one shot, swap one line of VO, re-render one asset's shots.

  This should make a finished film far cheaper and far more controllable. The claim is measured, not assumed: every production records its GPU minutes, AI usage and revisions, and the benchmark films compare total cost against a video-generation baseline.

**Stages** (what a studio does; each produces reviewable artifacts):

1. **Discovery: what are we making, and for whom?**
   - **Intake:** interview the user about goal, product, offer, deadline and where the video will run.
   - **Product understanding:** read the product's site and docs, and capture the live product.
   - **Audience research:** who exactly is watching (personas), what they already know, what they care about, what they distrust (e.g. developers distrust hype), and what each platform's viewers expect (pace, length, sound-off viewing, hook in the first seconds).
   - **Reference analysis:** break down reference videos the user likes, or category examples, into structure, pacing, shot types and sound. This is used for the craft only; references are never copied, and real brands never appear.
   - **Output: the creative brief.** One core message, proof points with sources, tone, audience, success measure, constraints. Approved before anything else.
2. **Development: the idea.**
   - Several concepts and treatments written in parallel, each with a logline, a structure and why it fits this audience.
   - The chosen concept becomes a script: VO, on-screen text, beats.
3. **Pre-production: how it will look.**
   - **Mood board and art direction:** palette, lighting mood, materials, typography and the Look.
   - **Style frames:** a few finished-quality key stills (rendered in Blender, with image-generation concepts as a starting point) that fix the look before any animation. These are the "scene mockups."
   - **Storyboard:** a frame per shot, with camera, action and notes.
   - **Animatic:** boards cut to temp VO and music on the edit timeline, to lock timing.
   - **Previs and layout:** grey-box 3D blocking with real cameras and lenses at the locked timing.
   - **Breakdown:** the asset list and shot list. This is the production plan the Producer schedules.
4. **Production: building and shooting it in Blender.**
   - **Assets:** modeling (procedural, geometry nodes, kitbash libraries, device models), materials and look development, rigs.
   - **Shots:** layout, animation (blocking → spline → polish), lighting, CGI effects and simulations.
   - **Live UI:** captures for every screen.
   - **Render:** at the tier the stage needs.
5. **Post-production:** edit, compositing, grade, motion typography, sound design and mix, finishing (see Production pipeline and Sound design and mix).
6. **Delivery and learning:** masters and cut-downs for every platform and aspect, review, distribution, then performance data fed back into Discovery for the next video.

**Departments are subagents.** Each department is an agent with its own skill pack (craft knowledge, recipes, rubric, failure corpus):

| Group | Departments |
|---|---|
| Strategy | Strategist, Audience researcher |
| Story | Writer, Director |
| Art | Art director, Storyboard artist, Editor |
| Production | Previs/layout, Modeler, Look-dev artist, Animator, Lighter, FX artist |
| Post | Compositor, Colorist, Motion designer, Sound designer, Mixer |
| Running the show | Producer, Critic/QA |

- **The Producer** turns the breakdown into a production graph, schedules it, and keeps it within budget.
- **The Director** keeps every department consistent with the brief and the chosen concept.
- **The Critic** reviews every artifact against its rubric before it moves on.

**The production graph works like a build system.**
- Every artifact (brief, script, board, asset, shot, render, mix) is a node with typed inputs, a content hash, and the department that makes it.
- **Parallelism comes from the graph:** independent assets, shots and departments run at once, limited only by GPU leases (computeshare) and AI capacity (tokenbaton).
- **Changes propagate like a build:** changing an asset marks only the shots that use it as dirty; changing the grade re-runs only compositing. Nothing clean is redone.
- **Each stage has a gate:** Critic review, plus optional human approval. By default people approve at the cheap, high-leverage points (brief, concept, style frames, animatic) and the final. Any gate can be set to automatic, or to require a person.

**Quality profiles choose the steps.** How many stages run, how many review loops each gets, and at what render tier depend on the quality wanted:

| Profile | Use | What it adds |
|---|---|---|
| `draft` | quick social posts, internal | brief, script, template shots, preview tier, auto gates |
| `standard` | ads, product updates | + storyboard, animatic, style frames, final tier, sound design |
| `premium` | launch videos | + audience research, multiple concepts, previs, custom assets, animation polish, CGI effects, full grade and mix |
| `cinema` | flagship launch films, long-form | + reference analysis, extra critique rounds per department, hero tier, immersive mix |

Every production shows an estimate before it starts (GPU hours, AI usage, wall time, gates waiting on people), and the user can add or drop individual steps.

**Learning which steps add quality.**
- **Step ledger:** every step records its cost, time, the Critic's rubric scores before and after, and whether people accepted or rejected the result.
- **Over many productions** this shows which steps actually raise quality for which kind of video, and which only add cost.
- **Using it:** the Producer uses the ledger to suggest steps for a quality target and budget ("for launch videos, previs plus a second lighting pass raised scores most per GPU hour").
- **Safety:** profiles start hand-written from studio practice. Learned changes are proposed, checked against the benchmark films, and only then adopted. A change that lowers a benchmark score is rejected.

**Rationale**: studio pipelines exist because fixing a problem gets more expensive at every later stage. Encoding that pipeline as a content-addressed graph gives agents the same advantage: cheap decisions early, local changes late, and reuse everywhere. Making departments separate subagents with their own skills and rubrics keeps each one expert and lets them work in parallel. Rejected:
- one general agent doing everything in sequence: slow, shallow per craft, and a mistake late on forces a full redo;
- end-to-end video generation as the core: outcome-only iteration, weak control over brand, UI legibility and exact text, and no reusable assets.

## Long-form work and the quality strategy

Output quality is the only thing that makes SpectraCut worth using. Everything else (cost, speed, automation) matters only once the result is something a top studio would be proud of. This section sets how SpectraCut gets there, for a 30 s ad and for a 13-minute episode alike.

### From one request to compartmentalised units and back

A request such as "a 13-minute episode, and these things must happen in the story" is broken down top-down and consolidated bottom-up:

```
Production ─ canon (story bible) ─ Episode
   └─ Acts ─ Sequences ─ Scenes ─ Shots ─ department tasks (layout, anim, lighting, FX, comp, sound…)
consolidation:  tasks → shot renders → scene edits → sequence conform → episode master + stems
```

- **Breakdown:**
  - the Writer and Director turn the request into a beat sheet, then a script with scene headings;
  - the Producer breaks scenes into shots, and shots into department tasks, each a node in the production graph with an estimate.
  - A 13-minute episode is roughly 150–300 shots.
- **Canon keeps a long film, and a whole universe, consistent.** Canon can belong to one production or to a universe shared by many productions (characters, worlds, timeline). Each production pins the canon version it uses, so later episodes can't silently change earlier ones. A shared story bible holds everything that must stay the same everywhere: character sheets (model, rig, wardrobe states, voice), environments, props, the Look, the music themes, and per-scene continuity (time of day, damage, who holds what). Every task reads canon instead of re-describing it, and the Critic checks shots against it. Continuity breaks are the main failure in long agent-made films, so canon is a first-class artifact, versioned like code.
- **Consolidation:**
  - shot renders conform into scene edits on the edit timeline, scenes into sequences, sequences into the episode;
  - sound is mixed per scene, then re-recorded for the episode (stems, M&E, loudness for the delivery spec);
  - every level is a node, so a fix in one shot flows up to the master without redoing anything else.

### How quality is reached

1. **Explore wide, commit narrow.** Generate many options where they are cheap (concepts, beats, boards, style frames, previs camera options), score them, keep the best, and only then spend GPU time. The stages a person reviews are the ones where a choice changes the whole film.
2. **Quality comes mostly from assets, not generation.**
   - Agents direct, assemble, light, animate and finish; they don't sculpt hero characters from nothing.
   - SpectraCut keeps a growing asset library: open, commercially usable libraries (e.g. Poly Haven, CC0; Blender Studio open-movie assets, CC-BY), licensed kits, and assets built for earlier productions.
   - New assets are made by look-dev and modeling departments, using procedural modeling and image-to-3D models where their licences allow, and are reviewed before they go into the library.
   - Every asset carries its `LicenseRecord`.
3. **Craft knowledge is encoded, not improvised.** Each department's skill pack holds studio practice: shot grammar, lighting setups, animation principles, editing rules, mix practice. Each also holds a failure corpus of what went wrong before, and reference examples of the target quality (taste anchors).
4. **The Critic is the most important agent.**
   - It reviews frames, contact sheets and short frame sequences (for motion problems) against the department's rubric and the taste anchors, and it judges against the canon and the brief.
   - Its scores are regularly checked against human ratings. Where the two disagree, the rubric is fixed, because a critic that can't tell good from great caps the whole system.
5. **Benchmarks are the definition of quality.** Each capability has reference pieces and a target score, rated by the Critic and by people. Nothing counts as done until it beats its benchmark, and no change ships that lowers one.

### The capability ladder: what is hard

Not all film is equally hard for agents. SpectraCut climbs this ladder, reaching benchmark quality on each rung before relying on the next:

| Rung | Kind of film | Main difficulty | Status of the tools |
|---|---|---|---|
| 1 | Product and device films, UI, typography, abstract CGI | lighting, materials, camera, finishing | achievable at top quality now; the primary use case |
| 2 | Environments, vehicles, machines, space, effects-heavy sequences | asset quality, simulation, scale | achievable with good assets and simulation |
| 3 | Creatures and armoured figures in action, little facial acting | body animation | needs motion libraries and AI motion models plus heavy critique |
| 4 | Character drama with dialogue, faces and acting | facial performance, lip sync, acting over long scenes | the hardest problem; tools are improving fast but are not yet at film quality without people |

A 13-minute narrative episode lives mostly on rungs 3–4. The architecture (graph, canon, departments, consolidation) supports it from the start. Its quality depends on character animation, which is the area to invest in and benchmark hardest:
- motion libraries and AI motion generation;
- facial animation driven from VO;
- a human-in-the-loop animation polish gate for `cinema` productions until the benchmarks show agents can match it.

**Render budget for long-form.** 13 minutes at 24 fps is 18,720 frames.
- At about 2 GPU-minutes per final Cycles frame that's about 620 GPU-hours, or roughly 13 days on two cards.
- With Eevee-based looks at about 10 s per frame it's about 50 GPU-hours.

So long-form productions:
- choose looks partly by render cost;
- render previews and the animatic on Eevee;
- spend Cycles time only on approved shots;
- can rent extra GPUs through the same executor interface when a deadline needs it.

**Third-party IP.** The brand guardrail applies to stories as well as logos. Franchise characters, factions and settings (e.g. Warhammer) are used only when the brief's owner holds the rights. For testing long-form, SpectraCut uses original settings and characters in the same genre.

**Rationale**: long films fail on consistency and character performance more than on single-frame beauty. A versioned canon, hierarchical consolidation and a calibrated Critic target the first. Assets plus motion tools plus a human polish gate target the second until the tools close the gap. Climbing the ladder from product films keeps SpectraCut useful and ground-breaking on the work RapierCraft needs now, while building toward narrative film. Rejected:
- promising narrative-film quality before the benchmarks show it;
- generating characters and acting end to end with video models, which loses continuity, control and reuse across a 13-minute episode.

## Production pipeline: from shots to a finished film

The harness, templates and capture make shots. A launch film also needs the stages a studio runs after that. Each stage is declarative (part of the spec or recipe), so a finished film can be re-rendered exactly and revised by an agent or a person.

```
brief → script/VO → storyboard → animatic (edit timeline)
      → shots: 3D (Blender) + live UI (capture) + motion type + CGI effects
      → render passes (beauty, AOVs, cryptomatte, depth, motion vectors)
      → comp + finishing (lens, grade, grain)  → sound design + mix
      → conform to timeline → masters (per aspect, HDR/SDR) → QA + critique → review
```

- **Edit timeline as a first-class model.** The edit is an `Edit` object: tracks of clips with in/out points, transitions, speed ramps, J/L audio cuts and markers. It starts in the animatic and stays the single timeline through finishing. It imports and exports **OpenTimelineIO**, so a cut can round-trip to DaVinci Resolve or Premiere for human finishing and come back. Editing rules (cut on motion and beat, shot-scale variety, pacing curves per format) live in the edit-rule linter and the skill pack.
- **Look development and color.**
  - Scene-referred color end to end: Blender renders linear EXR, and the view transform is AgX, or ACES 2 where a project needs it.
  - A **Look** is a versioned preset: grade (a CDL or LUT per look), lens (chromatic aberration, vignette, distortion, halation), bloom, grain, motion blur and depth of field rules. Brand kits pick looks.
  - Masters are SDR Rec.709 by default, with optional HDR (Rec.2100 PQ) for platforms that take it.
  - QA reads each look's thresholds (grain, haze, bloom) instead of one global limit.
- **Render passes and compositing.** Final renders write multilayer EXR with AOVs (diffuse, glossy, emission), cryptomatte, depth and motion vectors. Compositing is a declarative node graph per shot: screen glow kept separate from the scene, UI sharpening, glints, light wraps, relighting tweaks without re-rendering. It runs in Blender's compositor on the render host. Changing the grade or comp re-runs only compositing, not the render.
- **Motion typography.** A typography engine for titles, kinetic type, lower thirds, UI callouts and end cards:
  - type in 3D (extruded, depth-of-field aware) and 2D (crisp, pixel-aligned overlays);
  - animated by named presets (tracking reveals, mask wipes, per-glyph staggers, match cuts from UI text to titles) with easing curves;
  - brand fonts from the brand kit, with licence checks;
  - every on-screen word still passes the copy gate.
- **CGI effects.** A library of effect recipes: particles and dust, light streaks and sweeps, glass and refraction, liquid and simulations with baked caches, UI elements dissolving into particles, holographic or volumetric beams. Each is a parameterised recipe with a render-cost estimate and a QA profile. Simulations are baked once and cached by recipe hash, so finals replay them identically.
- **Sound design and mix:** a full post-production sound department built on open-source tools. See Sound design and mix.
- **Quality tiers.** Each render runs at a tier:
  - `preview`: Eevee/Workbench, half resolution, warm GPU session, seconds per frame;
  - `review`: Eevee or low-sample Cycles at full resolution;
  - `final`: Cycles with denoising, full passes, comp and finishing;
  - `hero`: higher samples, 4K masters, ProRes 4444 or EXR deliverables.

  Approval at one tier unlocks the next, so final and hero GPU time is spent only on approved shots.
- **Benchmark films.** Quality is tracked against a set of reference pieces: a device launch, an app walkthrough, a 30 s ad, a title sequence. Each has a rubric scored by the critique loop and by a human. A change that lowers a benchmark score is a regression, like a failing test.

**Rationale**: the gap between "a correct render" and "a launch film" is finishing: edit, color, comp, type, effects and sound. Building these as declarative, cached stages keeps every film reproducible, lets agents revise one stage without redoing the others, and lets a person take over in Resolve through OpenTimelineIO at any point. Rejected:
- baking the look into the render, which forces a re-render for every grade change;
- a closed internal timeline format, which locks out human editors;
- one global "clarity" threshold for every style, which would forbid deliberate cinematic looks.

## Sound design and mix

Target: film-grade sound, built the way a feature-film post sound team works, on open-source tools that SpectraCut drives from code. Sound is declarative like picture: an `AudioRecipe` attached to the edit timeline describes every cue, layer, bus and automation curve, renders deterministically, and can be opened by a human sound designer in a real DAW.

**The departments SpectraCut covers:**
- **Dialogue/VO:** clean-up (noise, de-ess, de-click, room tone), level riding, timing to picture, and alternate-language VO.
- **Hard effects and designed sounds:** hits, impacts, whooshes, risers, drops, braams, glitches, UI sounds, built in layers (sub, body, transient, air/tail).
- **Foley-style detail:** clicks, taps, typing, scrolls, device handling, glass and metal touches, synced to the exact frame.
- **Backgrounds and ambiences:** room tones, abstract beds and textures under scenes.
- **Music editing:** the music cut to picture (re-edited at bar and beat boundaries, time-stretched where needed, stingers and endings landed on the end card), working from stems where available.
- **Re-recording mix:** dialogue, music and effects buses, ducking and sidechain, EQ to stop music masking VO, reverbs that match each shot's space, automation following picture.
- **Deliverables:** stereo master, stems (DX/MX/FX), an M&E track (music and effects with no dialogue, for other languages), and a 5.1 and object-based immersive (7.1.4) master for film work.

**Where cues come from (the advantage over a normal sound team):** SpectraCut knows exactly what happens on screen, so most cue placement needs no guessing:
- the edit timeline gives cuts, transitions and speed ramps;
- recipes give camera moves (speed, distance), object motion, impacts, and effects such as particle bursts;
- live-UI capture gives every click, keystroke, scroll and page change, with frame times;
- motion typography gives each title's reveal and hit.

A sound-design agent (with a sound-design skill pack and rubric) turns those events into cues: which sound family, how big, what layers, what pitch and timing offsets. Event parameters drive the sound: a faster camera move gives a longer, brighter whoosh; a bigger title gives a heavier hit.

**Open-source stack:**

| Job | Tool | Why |
|---|---|---|
| Mix engine and human hand-off | **Ardour** (GPL) | A full DAW with buses, automation, LV2/VST3 plugins and Lua scripting. SpectraCut writes an Ardour session from the `AudioRecipe` and renders it headless, so a person can open the exact mix and keep working. |
| Procedural sound design | **SuperCollider** (GPL), non-real-time rendering | Synthesises whooshes, risers, impacts, drones and UI tones from parameters, deterministically and offline, with no sample library needed. |
| Clip processing in Python | **Pedalboard** (GPL) | Applies EQ, compression, reverb, pitch and time effects and hosts VST3 plugins from Python, for per-clip prep and quick previews. |
| Pro processing plugins | **LSP Plugins**, **x42**, **Airwindows**, **Dragonfly Reverb** | Mastering-grade EQs, compressors, limiters, convolution and algorithmic reverbs, true-peak limiting and loudness meters. |
| Synths for designed sounds | **Surge XT** (GPL) | Presets for risers, pads, impacts and textures, driven by the cue parameters. |
| Time-stretch and pitch | **Rubber Band** (GPL) | High-quality stretching so music and effects fit picture exactly. |
| Music analysis and stems | **librosa**, **Essentia** (AGPL), **Demucs** (MIT) | Beats, bars, downbeats and sections for cutting music to picture; stem separation when the licensed track has no stems. |
| Dialogue clean-up | **DeepFilterNet**, **RNNoise** | Noise removal on recorded or synthetic VO. |
| Immersive and surround | **EBU ADM Renderer / EAR Production Suite**, **IEM Plugin Suite** | Object-based mixes and ADM BWF deliverables; ambisonic beds and spatial reverbs. |
| Loudness and QA metrics | **pyloudnorm**, ffmpeg `ebur128`, x42 meters | Integrated and short-term loudness, true peak, loudness range. |
| Sound library search | **CLAP** audio-text embeddings | Search the local library by description ("glassy short whoosh, rising"). |

The exact versions, and whether each tool's headless mode works as needed (especially Ardour's headless render from its Lua tool), are confirmed in the first audio issue before the design depends on them. Every tool runs as a separate program or library call. None of them is copied into SpectraCut's code, so their licences stay compatible with AGPL distribution.

**Sound sources and licensing:**
- **Procedural** (SuperCollider, Surge XT): owned outright, commercial-safe by default.
- **Libraries the user has licensed** (e.g. the free Sonniss GDC bundles, or paid libraries): indexed locally, never redistributed in the repo.
- **Freesound:** CC0 only for commercial work.
- **Generative models** (ElevenLabs SFX, open audio models): used only where their licence allows commercial use, and recorded per asset.
- **Not commercial-safe:** libraries such as BBC Sound Effects, whose licence is non-commercial, are allowed for previews but never in a commercial-safe master. Every sound carries a `LicenseRecord`; one non-commercial sound marks the whole master non-commercial-safe.

**Delivery specs** (per-platform loudness presets):
- streaming and social: −14 LUFS integrated, ≤ −1 dBTP;
- broadcast: EBU R128 −23 LUFS, or ATSC A/85 −24 LKFS;
- film and streaming-service masters: dialogue-gated targets (e.g. −27 LKFS) with the delivery stems.

**Audio QA:**
- **Metric checks:** loudness and true peak per delivery spec; no clipping; stereo phase correlation and mono compatibility; VO intelligibility (music and effects kept below VO in the speech band); sync (every cue within one frame of its event); no unintended silence or abrupt cut-offs; and repetition, meaning the same sample not reused too close together.
- **Critique: an audio critic that actually listens.** Text-and-image models can't hear, so the audio critic is a stack of models that do. Each layer covers what the one below can't:
  1. **Exact metrics** (above): loudness, peaks, phase, sync, masking, repetition. Cheap, deterministic, run on everything.
  2. **Learned quality predictors** score each stem and the mix without a prompt:
     - Meta's **Audiobox Aesthetics** (CC-BY 4.0) scores production quality, production complexity, enjoyment and usefulness for speech, music and sound effects;
     - speech MOS predictors such as **UTMOS** and **DNSMOS** score VO.
     These catch thin, muddy or badly produced audio. Known biases (an independent review found it can rate low-bitrate audio too highly, and it favours some vocal styles) mean they flag problems rather than decide on their own.
  3. **An omni model that listens:** an audio-native model hears the mix, or the video with its sound, and answers the rubric questions: does every major cut land on a transition sound, does the riser peak on the title, does the music's energy match the edit, is the VO always clear, does anything sound cheap or out of place.
     - Local by default: an open omni model on a render-host GPU (e.g. Qwen3-Omni; its Q4 quantisation needs about 19 GB of VRAM, which is more than the 4070 Ti's 12 GB, so it runs split across cards, on a rented GPU, or as a smaller variant). Licence checked before use.
     - Optional providers: a hosted audio-native model through the user's own API key.
  4. **Calibration against people:** the stack's scores are checked against your ratings of the same mixes, and weighted by how well each layer agrees with you.

  Human approval of `final` audio stays on until calibration shows the critic agrees with people on the benchmark mixes; then it becomes a setting like every other gate. Commercial music generators (e.g. Suno) don't publish their internal quality models, so SpectraCut doesn't depend on them.

**Rationale**: a film-grade result needs a full mix environment, not a chain of ffmpeg filters. Generating an Ardour session keeps the mix inspectable and lets a human sound designer take over at any point, just as OpenTimelineIO does for the picture edit. Procedural design in SuperCollider gives a large range of owned, commercial-safe sounds from parameters, and suits cues driven by exact on-screen events. Rejected:
- ffmpeg filter graphs as the mix engine: no plugin hosting, buses or automation, and no human hand-off;
- a closed DAW such as Reaper (scriptable but not open source) as the required engine; it can be added later as an optional export target;
- relying only on generative audio models: hard to control, licences vary, and they can't hit frame-exact events.

## Optional integrations: tokenbaton, computeshare, ForgeDock

SpectraCut works fully without these three. Each is off by default, turned on by one config block, and detected at startup; `spectracut doctor` reports which are on and whether they're healthy. None of them changes a spec, a recipe or a render: they only change where work runs, which account pays for AI calls, and who gets told about failures.

```
                  ┌──────────── ForgeDock ────────────┐
                  │ skill: spectracut demo / release  │◄── issues for repeated QA/critique failures
                  └───────────────┬───────────────────┘
                                  │ CLI / MCP
                                  ▼
  AI providers ◄── provider layer ── spectracut (control plane: API, MCP, queue)
  (claude via                          │ executors
   tokenbaton-exec)        ┌───────────┴─────────────┐
                           ▼                         ▼
                  spectracut-host (pull)     computeshare router (push)
                  registered GPU boxes        └─ sandbox: spectracut-host --once
                                                 (CPU slice or GPU slice)
```

### tokenbaton: more AI capacity across several Claude accounts

- **Command:** the Claude provider runs `ai.claude_bin` (default `claude`). Locally it points at `tokenbaton-exec`, which picks the account with the most room on every call. The interactive `claude-go` / `tokenbaton` launcher is not used for headless calls.
- **Capacity check:** before starting a job that makes many AI calls (critique loop, ad matrix writing), the queue asks the provider for capacity. With tokenbaton present that's `tokenbaton --pick`; without it, it's the last rate-limit response seen. When no account has room, the job waits in a `waiting_for_ai` state and resumes when the reset time passes, instead of failing halfway through a revise loop.
- **Rate-limit errors** from any Claude or Codex call are parsed into a typed `ProviderExhausted(reset_at)` error, so the same waiting logic works for single-account users.
- **Isolation:** provider calls run with a minimal config (no user plugins, MCP servers or hooks), which matters more when tokenbaton spreads the overhead across accounts.

**Rationale**: the bottleneck for agent-heavy formats is subscription usage, not GPUs. Making "out of AI capacity" a normal queue state helps every user; tokenbaton is then just a better capacity source. Rejected: building account rotation into SpectraCut, which duplicates tokenbaton and doesn't belong in a distributed product.

### computeshare: run jobs on spare machines on the tailnet

computeshare already places Docker sandboxes on enrolled hosts by reserved CPU and memory, renews leases, and cleans up. SpectraCut uses it as a second **executor** next to directly registered `spectracut-host` machines.

- **Executor interface:** the queue hands a job to an executor. `HostPoolExecutor` (default) leaves it for registered hosts to pull. `ComputeshareExecutor` calls the router's `POST /sandboxes` with a SpectraCut image and the job id, keeps the lease alive by polling `GET /sandboxes/:id`, and deletes the sandbox when the job ends or is cancelled.
- **Same protocol inside the sandbox:** the sandbox runs `spectracut-host --once --job <id>`, which pulls that one job over the normal host protocol, then uploads results to the control plane's storage. So signing, the execution guard and the recipe-only runtime are identical on both paths, and large frame sequences never pass through the router.
- **Job routing:**
  - CPU jobs (capture, ffmpeg mux, QA checks, audio mix) can go to CPU slices today.
  - Blender jobs need GPU slices (below), or fall back to registered hosts.
  - Each job kind declares `cpus`, `mem_gb`, `gpus` and `vram_gb`; the executor only offers it to a backend that can meet them.
- **Images:** computeshare today pre-pulls one lane image. It gains a per-client allow-list of images (`spectracut-worker:cpu`, plus GPU images if the Docker path is kept), pre-pulled at enrollment, never pulled during a launch.
- **Auth:** SpectraCut gets its own router client token, separate from ForgeDock's, with its own concurrency cap, so renders can't starve ForgeDock lanes or the reverse.

**Rationale**: computeshare already solves placement, capacity reservation and cleanup on a mixed Windows/Linux fleet. Reusing it means one pool of spare machines serves both ForgeDock lanes and SpectraCut renders. Running `spectracut-host` for every job keeps one execution path to secure and test. Rejected: SpectraCut talking to computeshare agents directly (duplicates the router), and passing frames through the router's result endpoint (it's built for small JSON results, not gigabytes of PNGs).

### computeshare GPUs: whole cards for renders, previews and other GPU work

The target fleet today: `archlinux` (control plane and computeshare router, no GPU), `legion` (RTX 3070 Laptop, 8 GB) and `mrdubey-1` (RTX 4070 Ti, 12 GB), both Windows. The goal is that SpectraCut can use both cards fully, through computeshare, for final renders, fast previews and mockups, and other GPU work.

- **Whole-GPU leases.** A GPU is reserved as a unit, never sliced: Blender, NVENC and local models all expect the whole card. Agents advertise each card (model, VRAM, driver, capabilities: `cuda`, `optix`, `opengl`, `vulkan`, `nvenc`), detected with `nvidia-smi` on the Windows side. The router places a GPU job only on a free card that meets its `vram_gb` and capabilities, and prefers the bigger card for heavy jobs.
- **Native Windows GPU runner (expected), decided by measurement.** computeshare lanes run in Docker inside WSL2. CUDA and OptiX (Cycles) work there; Eevee and Workbench need full OpenGL/Vulkan on the NVIDIA driver, which WSL2 doesn't reliably give. Eevee and Workbench are what make previews and mockups fast. So the first task is a benchmark on `legion`: the same shot in Cycles, Eevee and Workbench plus an NVENC encode, in WSL Docker and natively on Windows. Expected result, and the plan unless the numbers say otherwise:
  - computeshare's Windows installer adds a small native **GPU runner** service next to the WSL distro;
  - it runs as a dedicated low-privilege local user, and starts only allow-listed executables at fixed paths (Blender 4.5 LTS, `spectracut-host`, ffmpeg), never arbitrary commands;
  - a Windows Firewall rule blocks outbound network for Blender, matching the "no network during renders" guardrail;
  - the router talks to it the same way it talks to a WSL agent, so SpectraCut sees one kind of GPU sandbox either way.
- **Warm GPU sessions for speed.** Start-up dominates short preview renders: launching Blender and loading the scene often takes longer than rendering a preview frame. SpectraCut can hold a **session lease** on a card. Blender stays running with the scene loaded, and each preview or mockup only applies recipe changes and renders. The lease ends after an idle timeout (default 10 min) or when the session ends. Final renders use fresh one-shot jobs so they stay reproducible.
- **Using both cards at once:**
  - finals are split **by shot** across cards, so every frame of one shot comes from one machine;
  - previews may be split **by frame** across cards for speed;
  - the frame cache key includes the render profile (Blender version, GPU model, driver), so frames from different machines are never mixed in a final shot.
- **Other GPU work ("whatever"):** the same lease runs allow-listed GPU job kinds besides Blender:
  - NVENC encodes for review proxies and fast exports;
  - local image models (e.g. a ComfyUI/Flux worker) as a board and mockup provider, alongside ChatGPT;
  - local speech models (e.g. Whisper) for caption timing.

  Each is a provider with a mock, like every other integration.
- **Your machines stay yours.** Each GPU host has a lending mode, set per host and switchable from the tray or `computeshare` CLI:
  - `dedicated`: always lend the card;
  - `when-idle`: stop taking new GPU jobs while you're at the machine or a full-screen app or another heavy GPU process is running; a running job finishes its current frame, then the rest of the shot moves to another card;
  - `off`.

  Laptops (`legion`) only take GPU jobs on AC power, and hosts report GPU temperature so the router can back off a hot card.

**Rationale**: renders and fast previews are where SpectraCut's time goes, and two consumer cards on the tailnet are enough for real speed if both are used fully and Blender stays warm between previews. A native runner is expected because Eevee and Workbench need full OpenGL/Vulkan, which WSL2 doesn't reliably provide; measuring first avoids building it if WSL turns out good enough. Rejected:
- fractional GPU slices, which Blender can't use well;
- splitting final shots by frame across different GPUs, which risks visible differences between machines;
- a cloud GPU for development, since the owner's cards are free and on the tailnet.

### ForgeDock: SpectraCut as a tool in the dev pipeline

- **Skills shipped from this repo** (`integrations/forgedock/`), installed only if ForgeDock is present:
  - `spectracut-demo`: after a PR deploys to staging, capture a short clip of the changed flow (live-UI capture plus mux, no Blender) and attach it to the PR. The flow comes from a `demo:` block in the issue or a recorded interaction script.
  - `spectracut-release`: turn the release notes ForgeDock already writes into a 30–60 s release video (and 9:16 cut), queued for human review, never auto-published.
  - `/design` hand-off: ForgeDock's render-critique loop can call SpectraCut's critique tools (`critique.*` over MCP) to score a rendered page against a rubric.
- **Failure issues:** when a shot fails QA or the critique loop after `max_revisions`, SpectraCut can file a GitHub issue in ForgeDock's issue format: the failing check, the template and recipe hash, the worst frames attached, and the guardrail involved. Off by default; rate-limited to one issue per template and failure kind; deduplicated against open issues.
- **No dependency the other way:** SpectraCut never imports ForgeDock or reads its state; the integration is a skill pack calling SpectraCut's CLI and MCP, plus issues written through `gh`.

**Rationale**: SpectraCut's CLI and MCP server already make it scriptable, so ForgeDock support is mostly packaging: skills plus an issue writer. PR demo clips need only capture and mux, so they're the earliest useful piece. Rejected: running SpectraCut's agent work as ForgeDock lanes; lanes are built for long repo-changing jobs, while SpectraCut makes many short AI calls that its own queue handles better.

## Milestone plan

Each milestone ends with a real AlterLab deliverable judged against its benchmark, so quality is proven on real work, not on demos.

1. **M1 Foundation: the studio's skeleton.**
   - repo scaffold, CI, release and staging slice;
   - schemas: brief, brand kit, canon, spec, recipe, `Edit` timeline (OpenTimelineIO import and export);
   - the production graph engine: nodes, content hashes, dirty propagation, gates, quality tiers;
   - the render host agent, job queue and frame cache, behind the executor interface;
   - the harness core (MCP: scene, camera, light, material, preview);
   - the AI provider layer (subscription sign-in, configurable `ai.claude_bin`, `waiting_for_ai`);
   - the CLI.

   **Deliverable:** a single device shot of AlterLab rendered from a recipe, replayed identically.
2. **M2 First film: AlterLab's SaaS launch film at the `standard` profile.**
   - Discovery (intake, product understanding, creative brief) and Development (concepts, script);
   - storyboard, style frames, animatic;
   - live-UI capture and brand-kit device, hero object and typography templates;
   - color management and Looks, render passes and compositing, the motion typography engine;
   - sound: tool check, `AudioRecipe` and Ardour session writer, event-driven cues, procedural design, music cut to picture, stereo mix with stems, audio QA with metrics and learned predictors;
   - QA gate and Critic v1, the Producer and departments v1;
   - ads and demos as cut-downs of the same production;
   - computeshare GPU work (benchmark, whole-GPU leases, native runner, `mrdubey-1`) so both cards render;
   - the ForgeDock `spectracut-demo` skill (PR demo clips).

   **Deliverable:** the AlterLab launch film, plus its 15 s and 30 s ads in every aspect, beating the first benchmark films.
3. **M3 Premium and cinema: depth across every department.**
   - audience research and reference analysis, multiple concepts, previs and layout;
   - asset library and look-dev, animation (motion libraries, AI motion), lighting and FX departments;
   - CGI effects library;
   - the sound-design skill pack and agent, sound library search, dialogue clean-up, M&E;
   - the listening audio critic (omni model) and Critic calibration against human ratings;
   - the step ledger and step suggestions;
   - tutorial, explainer and demo-walkthrough formats;
   - ForgeDock failure issues and `/design` critique hand-off.

   **Deliverable:** an AlterLab product walkthrough and a `premium` launch piece, scored above the `standard` film on the same rubric.
4. **M4 Long-form, review, distribution and cloud.**
   - long-form: hierarchical breakdown and consolidation, universe canon, continuity checks, character animation track (facial animation from VO, human polish gate);
   - review UI, distribution exports and the performance loop;
   - HDR, `hero` deliverables, 5.1 and immersive mixes;
   - cloud (tenancy, auth, billing, GPU pool, storage), marketing site, `spectracut-release` skill;
   - the Spellwise product-agnostic acceptance.

   **Deliverable:** a short original narrative piece (rung 2–3 of the capability ladder) in an original universe, plus the open-source release.

**Rationale**:
- The production graph sits in M1 because every stage, department and gate is a node in it; building formats first and the graph later would mean rewriting them.
- A whole film in M2, not ads first: AlterLab's launch film is the primary use, and ads at this quality are cut-downs of a well-made film, not a separate product.
- Depth (M3) before breadth (M4 long-form, cloud) because output quality is the only thing that makes SpectraCut useful.
- Long-form narrative comes last and climbs the capability ladder; its hardest part (character performance) gets the most benchmarking.
- Every milestone ships a real deliverable and the release slice, so the project is always usable.
