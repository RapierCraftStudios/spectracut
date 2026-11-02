# Technical Spike: SpectraCut

> Written by /bootstrap on 2026-11-02. Every decision is followed by an ADR-style `**Rationale**:` line (the decision, why, and the rejected alternative).

SpectraCut is an agent-native video and ad engine. AI agents direct headless Blender on GPU render hosts, capture any live web product frame by frame, and assemble tutorials, explainers, ads in every aspect ratio, and cinematic trailers. Every render passes an automated QA gate and a vision critique loop before a human reviews it. It is product-agnostic ("it can market even a spelling checker") and ships as self-hosted open source (AGPL-3.0, dual-licensed commercially), plus a hosted cloud version.

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
- **Scope:** the full product in one plan, ads first (owner decision). Ad generation lands before the deeper agent-direction features, then trailers and tutorials, then cloud.
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
  - Stripe for the cloud.

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
    queue/                       # job queue, scheduler, cache, host registry
    templates/                   # shot template library (Python definitions + assets manifest)
    skills/                      # cinematography + Blender skill pack (markdown prompts, recipes, rubrics, failure corpus)
    storyboard/                  # treatment, boards (image providers), blocking animatic, spec bridge
    critique/                    # vision judge, rubric scoring, revise loop
    writer/                      # brief -> hooks/scripts/VO/shot lists, copy gate
    audio/                       # VO (ElevenLabs), SFX, music beds, mix, captions
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

## Milestone plan

1. **Foundation:** repo scaffold, CI, spec schemas, CLI, render host agent and registry, job queue with cache, the harness core (MCP server with scene, camera, light, material, preview tools), and a release and staging slice (packages built, a staging control plane deploy).
2. **Ads engine (ads first):**
   - live-UI capture;
   - brand-kit-driven shot templates (devices, hero object, end card, typography);
   - writer and copy gate;
   - voice, music, SFX and mix with captions;
   - the ad format generator (6/15/30 s, all aspects, variant matrix);
   - the QA gate.
3. **Agent direction:**
   - the skill pack;
   - storyboard → animatic → spec;
   - the critique loop;
   - advanced harness tools;
   - the extended template library (Earth, abstract scenes, exploded UI);
   - tutorial, explainer and trailer formats.
4. **Review, distribution, cloud and launch:**
   - the review UI;
   - distribution exports and the feedback loop;
   - the cloud (tenancy, auth, billing, GPU worker pool, storage);
   - the marketing site;
   - the Spellwise product-agnostic acceptance.

**Rationale**:
- Foundation first because everything renders through the host agent, queue and harness.
- Ads second because the owner wants ads soonest and AlterLab is the first customer.
- Agent direction third because it deepens quality across all formats.
- Cloud and launch last because they monetise a product that already works self-hosted.
- The release and staging slice sits in M1 so every milestone ships.
