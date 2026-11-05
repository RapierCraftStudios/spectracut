# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``device-shot`` profile's runner: one product device shot rendered on the farm.

Stages (``profiles/device-shot.json``):

``shot``
    :func:`build_device_recipe` turns the brief and brand kit into a
    deterministic declarative recipe: device body, screen, plinth, the brand's
    UI screen through ``material.apply_screen`` (pinned by sha256), camera and
    key light, coloured from the brand palette.
``render_preview`` / ``render_final``
    :func:`tier_steps` appends the tier's render steps (preview: Eevee at 50%;
    final: Cycles, fixed seed, OIDN denoise). The job is signed to shared
    storage (:func:`sign_to_shared`) and runs through
    ``JobQueue.as_runner`` + :class:`FlamencoExecutor`. The output records the
    scene-graph hash and every frame's sha256. The preview frame is scored once
    by the vision judge and counted as ``vision_qa_requests`` AI usage.

:func:`replay` re-renders a recorded render node with a fresh job id into a
fresh output dir, bypassing the FrameCache and the graph cache, and compares
the scene-graph hash and per-frame hashes.

The control plane never imports ``bpy`` or the bpy runtime; hosts only run the
declarative steps.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import JsonValue

from spectracut.queue.executors.flamenco import FlamencoApi, FlamencoExecutor
from spectracut.queue.queue import Job, JobQueue
from spectracut.queue.signing import JobEnvelope, sign_job
from spectracut.spec.brand import BrandKit
from spectracut.spec.brief import Brief, CreativeBrief
from spectracut.spec.recipe import Recipe, RecipeStep
from spectracut.studio.estimate import BRIEF_PARAM
from spectracut.studio.graph import ProductionGraph, RunContext, Runner, RunResult
from spectracut.studio.node import Tier

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from spectracut.providers.base import VisionJudge

PROFILE = "device-shot"
SHOT_STAGE = "shot"
RENDER_STAGES = frozenset({"render_preview", "render_final"})

BRAND_KIT_PARAM = "brand_kit"
SCREEN_IMAGE_PARAM = "screen_image"
"""The screen image path relative to the assets dir, as the host resolves it."""
SCREEN_PATH_PARAM = "screen_path"
SCREEN_SHA256_PARAM = "screen_sha256"

BLENDER_VERSION = "4.5"
WIDTH, HEIGHT = 1920, 1200
PREVIEW_PERCENTAGE = 50
FINAL_SEED = 1153
"""Fixed Cycles seed so final renders are reproducible."""
FINAL_SAMPLES = 256

VISION_QA_METRIC = "vision_qa_requests"
VISION_RUBRIC = (
    "Product device shot. The device screen must be legible and unobstructed, the "
    "framing centred on the device, and no third-party logos or marks visible."
)
JOB_TTL = timedelta(hours=12)


def _rgb(hex_colour: str) -> list[JsonValue]:
    h = hex_colour.lstrip("#")
    return [round(int(h[i : i + 2], 16) / 255.0, 4) for i in (0, 2, 4)]


def _swatch(kit: BrandKit, name: str, fallback: int) -> list[JsonValue]:
    swatches = kit.palette.swatches
    if name in swatches:
        return _rgb(swatches[name])
    ordered = [swatches[k] for k in sorted(swatches)]
    return _rgb(ordered[min(fallback, len(ordered) - 1)])


def _steps(calls: list[tuple[str, dict[str, JsonValue]]], start: int = 0) -> list[RecipeStep]:
    return [RecipeStep(index=start + i, tool=t, args=a) for i, (t, a) in enumerate(calls)]


def build_device_recipe(
    brief: Brief | CreativeBrief,
    brand_kit: BrandKit,
    *,
    screen_image: str,
    screen_sha256: str,
) -> Recipe:
    """The deterministic device recipe: same brief, kit and image give the same hash.

    A tablet lies on a plinth with its screen facing an elevated camera, so the
    UI reads upright. The view transform is ``Standard`` so UI colours stay true
    (UI legibility wins over a filmic look).
    """
    body = _swatch(brand_kit, "ink", 0)
    plinth = _swatch(brand_kit, "surface", 1)
    calls: list[tuple[str, dict[str, JsonValue]]] = [
        ("scene.clear", {}),
        (
            "object.add_primitive",
            {
                "kind": "cylinder",
                "name": "plinth",
                "location": [0, 0, 0.05],
                "scale": [1.4, 1.4, 0.05],
            },
        ),
        ("material.apply_preset", {"object": "plinth", "preset": "matte_plastic", "color": plinth}),
        (
            "object.add_primitive",
            {
                "kind": "cube",
                "name": "device_body",
                "location": [0, 0, 0.13],
                "scale": [0.86, 0.56, 0.03],
            },
        ),
        (
            "material.apply_preset",
            {"object": "device_body", "preset": "glossy_plastic", "color": body},
        ),
        (
            "object.add_primitive",
            {
                "kind": "plane",
                "name": "device_screen",
                "location": [0, 0, 0.161],
                "scale": [0.8, 0.5, 1],
            },
        ),
        (
            "material.apply_screen",
            {
                "object": "device_screen",
                "image": screen_image,
                "image_sha256": screen_sha256,
                "strength": 1.0,
            },
        ),
        (
            "camera.add",
            {
                "name": "camera",
                "location": [0, -3.2, 2.6],
                "rotation": [round(math.radians(50), 6), 0, 0],
                "lens": 50,
            },
        ),
        (
            "light.add",
            {
                "kind": "area",
                "name": "key",
                "energy": 400,
                "location": [1.5, -1.5, 3.0],
                "rotation": [0.6, 0, 0.5],
            },
        ),
        ("render.set_resolution", {"width": WIDTH, "height": HEIGHT, "percentage": 100}),
        ("render.set_property", {"property": "view_settings.view_transform", "value": "Standard"}),
    ]
    return Recipe(
        id=f"{brief.id}-device",
        shot_id=brief.id,
        blender_version=BLENDER_VERSION,
        steps=_steps(calls),
    )


def tier_steps(tier: Tier, start: int = 0) -> list[RecipeStep]:
    """Render steps for ``tier``: preview is Eevee at 50%; final is seeded, denoised Cycles."""
    calls: list[tuple[str, dict[str, JsonValue]]]
    if tier is Tier.PREVIEW:
        calls = [
            ("render.set_engine", {"engine": "eevee"}),
            (
                "render.set_resolution",
                {"width": WIDTH, "height": HEIGHT, "percentage": PREVIEW_PERCENTAGE},
            ),
        ]
    elif tier is Tier.FINAL:
        calls = [
            ("render.set_engine", {"engine": "cycles"}),
            ("render.set_resolution", {"width": WIDTH, "height": HEIGHT, "percentage": 100}),
            ("render.set_property", {"property": "cycles.samples", "value": FINAL_SAMPLES}),
            ("render.set_property", {"property": "cycles.seed", "value": FINAL_SEED}),
            ("render.set_property", {"property": "cycles.use_denoising", "value": True}),
            ("render.set_property", {"property": "cycles.denoiser", "value": "OPENIMAGEDENOISE"}),
        ]
    else:
        raise ValueError(f"device-shot renders preview or final tiers, not {tier.value!r}")
    return _steps(calls, start)


def with_tier(recipe: Recipe, tier: Tier) -> Recipe:
    """``recipe`` with ``tier``'s render steps appended."""
    steps = list(recipe.steps) + tier_steps(tier, start=len(recipe.steps))
    return recipe.model_copy(update={"steps": steps})


def sign_to_shared(envelope: JobEnvelope, key: Ed25519PrivateKey, shared: Path) -> Path:
    """Sign ``envelope`` and write it to ``<shared>/jobs/<job_id>.json``."""
    path = Path(shared) / "jobs" / f"{envelope.job_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(sign_job(envelope, key).model_dump_json(), encoding="utf-8")
    return path


@dataclass(frozen=True)
class DeviceShotConfig:
    """Where device-shot jobs go: Flamenco Manager, shared storage, worker tag, assets."""

    flamenco_url: str
    shared: Path
    worker_tag: str
    assets_dir: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> DeviceShotConfig:
        env = os.environ if env is None else env
        names = ("SPECTRACUT_FLAMENCO_URL", "SPECTRACUT_FLAMENCO_SHARED", "SPECTRACUT_FLAMENCO_TAG")
        missing = [n for n in names if not env.get(n)]
        if missing:
            raise LookupError(f"device-shot needs {', '.join(missing)} set")
        return cls(
            flamenco_url=env["SPECTRACUT_FLAMENCO_URL"],
            shared=Path(env["SPECTRACUT_FLAMENCO_SHARED"]),
            worker_tag=env["SPECTRACUT_FLAMENCO_TAG"],
            assets_dir=env.get("SPECTRACUT_ASSETS_DIR") or None,
        )

    def job_file(self, job_id: str) -> Path:
        return self.shared / "jobs" / f"{job_id}.json"

    def out_dir(self, job_id: str) -> Path:
        return self.shared / "out" / job_id


@dataclass
class ExecutorOptions:
    """Test and tuning knobs passed to :class:`FlamencoExecutor`."""

    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    poll_interval: float = 5.0
    timeout_s: float | None = 3600.0
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def frame_hashes(out: Path) -> dict[str, str]:
    """sha256 of every ``frame_*`` file in ``out``, by file name."""
    return {p.name: _sha256(p) for p in sorted(out.glob("frame_*")) if p.is_file()}


def read_scene_graph_hash(out: Path) -> str | None:
    """``scene_graph_hash`` from ``report.json``, else ``result.json`` or a chunk result."""
    candidates = [out / "report.json", out / "result.json", *sorted(out.glob("result-*.json"))]
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        value = data.get("scene_graph_hash") if isinstance(data, dict) else None
        if isinstance(value, str):
            return value
    return None


def _fresh_job_id(config: DeviceShotConfig, base: str) -> str:
    """``base``, or ``base-<n>`` for the first n whose job file and output dir are unused."""
    candidate, n = base, 1
    while config.job_file(candidate).exists() or config.out_dir(candidate).exists():
        n += 1
        candidate = f"{base}-{n}"
    return candidate


def _envelope(job_id: str, recipe: Recipe, config: DeviceShotConfig, now: datetime) -> JobEnvelope:
    return JobEnvelope(
        job_id=job_id,
        recipe=recipe,
        recipe_hash=recipe.content_hash,
        frame_start=1,
        frame_end=1,
        output=recipe.shot_id,
        required_tag=config.worker_tag,
        issued_at=now,
        expires_at=now + JOB_TTL,
    )


def _blender_job(
    envelope: JobEnvelope, job_file: Path, out: Path, config: DeviceShotConfig, tier: Tier
) -> Job:
    payload: dict[str, JsonValue] = {
        "envelope": envelope.model_dump(mode="json"),
        "job_file": str(job_file),
        "worker_tag": config.worker_tag,
        "output_dir": str(out),
    }
    if config.assets_dir:
        payload["assets_dir"] = config.assets_dir
    return Job(
        id=envelope.job_id,
        kind="blender",
        tier=tier,
        recipe_hash=envelope.recipe_hash,
        payload=payload,
    )


def _executor(
    client: FlamencoApi, out: Path, config: DeviceShotConfig, opts: ExecutorOptions
) -> FlamencoExecutor:
    return FlamencoExecutor(
        client,
        output_dir=str(out),
        sleep=opts.sleep,
        poll_interval=opts.poll_interval,
        timeout_s=opts.timeout_s,
        assets_dir=config.assets_dir,
        clock=opts.clock,
    )


def _render_record(out: Path) -> tuple[str, dict[str, str]]:
    scene = read_scene_graph_hash(out)
    if scene is None:
        raise RuntimeError(f"render in {out} recorded no scene_graph_hash")
    frames = frame_hashes(out)
    if not frames:
        raise RuntimeError(f"render in {out} produced no frame_* files")
    return scene, frames


def _base_recipe(ctx: RunContext) -> Recipe:
    for nid in ctx.spec.inputs:
        value = ctx.inputs.get(nid)
        if isinstance(value, dict) and isinstance(value.get("recipe"), dict):
            return Recipe.model_validate(value["recipe"])
    raise ValueError(f"node {ctx.spec.id!r} has no input carrying a device recipe")


def _load_brief(data: Any) -> Brief | CreativeBrief:
    try:
        return Brief.model_validate(data)
    except ValueError:
        return CreativeBrief.model_validate(data)


def device_shot_runner(
    graph: ProductionGraph,
    config: DeviceShotConfig,
    *,
    client: FlamencoApi,
    judge: VisionJudge | None = None,
    key: Ed25519PrivateKey,
    now: Callable[[], datetime] | None = None,
    options: ExecutorOptions | None = None,
) -> Runner:
    """A graph Runner for the ``device-shot`` profile's ``shot`` and render stages."""
    opts = options or ExecutorOptions()
    clock_now = now or opts.now

    def shot(ctx: RunContext) -> RunResult:
        params = ctx.spec.params
        recipe = build_device_recipe(
            _load_brief(params[BRIEF_PARAM]),
            BrandKit.model_validate(params[BRAND_KIT_PARAM]),
            screen_image=str(params[SCREEN_IMAGE_PARAM]),
            screen_sha256=str(params[SCREEN_SHA256_PARAM]),
        )
        output: dict[str, JsonValue] = {
            "recipe": recipe.model_dump(mode="json"),
            "recipe_hash": recipe.content_hash,
        }
        return RunResult(output=output)

    def render(ctx: RunContext) -> RunResult:
        base = _base_recipe(ctx)
        recipe = with_tier(base, ctx.tier)
        job_id = _fresh_job_id(
            config, f"{graph.production_id}-{ctx.spec.id}-{recipe.content_hash[7:19]}"
        )
        envelope = _envelope(job_id, recipe, config, clock_now())
        job_file = sign_to_shared(envelope, key, config.shared)
        out = config.out_dir(job_id)
        job = _blender_job(envelope, job_file, out, config, ctx.tier)
        queue = JobQueue([_executor(client, out, config, opts)], now=clock_now)
        result = queue.as_runner(lambda _ctx: job, sleep=opts.sleep)(ctx)
        scene, frames = _render_record(out)
        flamenco = result.output if isinstance(result.output, dict) else {}
        output: dict[str, JsonValue] = {
            "recipe": base.model_dump(mode="json"),
            "recipe_hash": recipe.content_hash,
            "tier": ctx.tier.value,
            "scene_graph_hash": scene,
            "frames": dict(frames),
            "output_dir": str(out),
            "worker_tag": flamenco.get("worker_tag", config.worker_tag),
            "job_id": job_id,
            "flamenco_job_id": flamenco.get("flamenco_job_id"),
        }
        usage = result.usage
        if ctx.tier is Tier.PREVIEW:
            active = judge
            if active is None:
                from spectracut.providers.base import select_vision_judge

                active = select_vision_judge()
            verdict = active.score(out / sorted(frames)[0], VISION_RUBRIC)
            output["vision_qa"] = {"score": float(verdict.score), "notes": verdict.notes}
            ai = {**usage.ai_usage, VISION_QA_METRIC: usage.ai_usage.get(VISION_QA_METRIC, 0) + 1}
            usage = usage.model_copy(update={"ai_usage": ai})
        return RunResult(output=output, usage=usage)

    def run(ctx: RunContext) -> RunResult:
        if ctx.spec.kind == SHOT_STAGE:
            return shot(ctx)
        if ctx.spec.kind in RENDER_STAGES:
            return render(ctx)
        raise LookupError(f"device-shot has no stage {ctx.spec.kind!r}")

    return run


@dataclass(frozen=True)
class ReplayResult:
    """A replayed render next to the recorded one."""

    node_id: str
    original_job_id: str
    job_id: str
    output_dir: Path
    original_scene_graph_hash: str | None
    scene_graph_hash: str | None
    original_frames: dict[str, str]
    frames: dict[str, str]

    @property
    def scene_matches(self) -> bool:
        return (
            self.scene_graph_hash is not None
            and self.scene_graph_hash == self.original_scene_graph_hash
        )

    @property
    def frames_match(self) -> bool:
        return bool(self.frames) and self.frames == self.original_frames

    @property
    def matches(self) -> bool:
        return self.scene_matches and self.frames_match

    def mismatches(self) -> list[str]:
        out: list[str] = []
        if not self.scene_matches:
            out.append("scene_graph_hash")
        for name in sorted(set(self.original_frames) | set(self.frames)):
            if self.original_frames.get(name) != self.frames.get(name):
                out.append(name)
        return out


def _fresh_replay_id(config: DeviceShotConfig, job_id: str) -> str:
    n = 1
    while True:
        candidate = f"{job_id}-replay-{n}"
        if not config.job_file(candidate).exists() and not config.out_dir(candidate).exists():
            return candidate
        n += 1


def replay(
    graph: ProductionGraph,
    node_id: str,
    config: DeviceShotConfig,
    *,
    client: FlamencoApi,
    key: Ed25519PrivateKey,
    now: Callable[[], datetime] | None = None,
    options: ExecutorOptions | None = None,
) -> ReplayResult:
    """Re-render ``node_id``'s recorded recipe, bypassing caches, and compare hashes.

    Raises :class:`KeyError` for an unknown node and :class:`ValueError` for a
    node with no accepted render output.
    """
    opts = options or ExecutorOptions()
    recorded = graph.output(node_id)
    if not isinstance(recorded, dict) or not all(
        k in recorded for k in ("recipe", "tier", "frames", "job_id")
    ):
        raise ValueError(f"node {node_id!r} has no recorded render to replay")
    tier = Tier(str(recorded["tier"]))
    recipe = with_tier(Recipe.model_validate(recorded["recipe"]), tier)
    original_job = str(recorded["job_id"])
    job_id = _fresh_replay_id(config, original_job)
    out = config.out_dir(job_id)
    envelope = _envelope(job_id, recipe, config, (now or opts.now)())
    job_file = sign_to_shared(envelope, key, config.shared)
    job = _blender_job(envelope, job_file, out, config, tier)
    job.node_id = node_id
    _executor(client, out, config, opts).run(job)  # direct: no FrameCache, no graph cache
    raw_frames = recorded["frames"]
    original_frames = (
        {str(k): str(v) for k, v in raw_frames.items()} if isinstance(raw_frames, dict) else {}
    )
    original_scene = recorded.get("scene_graph_hash")
    return ReplayResult(
        node_id=node_id,
        original_job_id=original_job,
        job_id=job_id,
        output_dir=out,
        original_scene_graph_hash=original_scene if isinstance(original_scene, str) else None,
        scene_graph_hash=read_scene_graph_hash(out),
        original_frames=original_frames,
        frames=frame_hashes(out),
    )
