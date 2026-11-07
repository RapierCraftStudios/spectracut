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
    final: Cycles, fixed seed, OIDN denoise). Before submitting,
    :func:`stage_assets` copies the brand kit's ``assets/`` once into
    ``<shared>/assets/<sha>/`` (content-addressed, every referenced file's
    sha256 verified) and the job's assets dir is ``{storage}/assets/<sha>``,
    which Flamenco's two-way ``storage`` variable resolves on Linux and
    Windows workers (``SPECTRACUT_ASSETS_DIR`` overrides it verbatim and
    disables staging). The job is signed to shared
    storage (:func:`sign_to_shared`) and runs through
    ``JobQueue.as_runner`` + :class:`FlamencoExecutor`. The output records the
    scene-graph hash and every frame's sha256. The preview frame is scored once
    by the vision judge and counted as ``vision_qa_requests`` AI usage, plus
    ``vision_qa_input_tokens`` and ``vision_qa_output_tokens`` when the judge
    reports its token usage.

The ``device-shot-graded`` profile (``profiles/device-shot-graded.json``) adds:

``render_scene``
    The final tier plus the production's :class:`ColorPipeline` steps, the
    Look's :func:`render_rules` (motion blur, DoF) and
    ``render.set_output {OPEN_EXR, 16}``: linear scene-referred EXR frames.
``finish``
    :func:`spectracut.finishing.looks.finish_runner`: stages the EXRs and runs
    the node's Look (param ``look_id`` in param ``brand_kit``) as a separate
    compositor job. Only this node keys on the Look's finishing, so switching
    Looks re-runs ``finish`` alone and reuses the render.

The ``device-shot-comped`` profile (``profiles/device-shot-comped.json``) puts a
``comp`` stage between them:

``render_scene`` with param ``render_passes: true``
    Enables the view-layer passes (AOVs, depth, motion vectors, object
    cryptomatte) and writes 32-bit multilayer EXR (:func:`comp.passes_steps`).
``comp``
    :func:`spectracut.finishing.comp.comp_runner`: the node's comp graph (param
    ``comp_id`` in ``comps_dir``, the repo's ``comps/`` by default) as a
    compositor-only job on the multilayer EXRs. A comp change re-runs ``comp``
    and ``finish``, never the render.

:func:`replay` re-renders a recorded render node with a fresh job id into a
fresh output dir, bypassing the FrameCache and the graph cache, and compares
the scene-graph hash and per-frame hashes.

The control plane never imports ``bpy`` or the bpy runtime; hosts only run the
declarative steps.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import os
import re
import shutil
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from pydantic import JsonValue

from spectracut.finishing import comp
from spectracut.finishing.color import ColorPipeline
from spectracut.finishing.looks import finish_runner, render_rules, resolve_look
from spectracut.harness.imagecmp import ImageError, decode_png
from spectracut.providers.base import judge_usage
from spectracut.queue.executors.flamenco import FlamencoApi, FlamencoExecutor
from spectracut.queue.queue import Job, JobQueue
from spectracut.queue.signing import JobEnvelope, sign_job
from spectracut.spec.brand import BrandKit, Look
from spectracut.spec.brief import Brief, CreativeBrief
from spectracut.spec.recipe import Recipe, RecipeStep
from spectracut.studio.estimate import BRIEF_PARAM
from spectracut.studio.graph import ProductionGraph, RunContext, Runner, RunResult
from spectracut.studio.ledger import Usage
from spectracut.studio.node import Tier
from spectracut.studio.profiles import QualityProfile
from spectracut.studio.store import write_atomic

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from spectracut.providers.base import VisionJudge
    from spectracut.qa.report import QAReportStore

PROFILE = "device-shot"
SHOT_STAGE = "shot"
RENDER_STAGES = frozenset({"render_preview", "render_final"})
GRADED_PROFILE = "device-shot-graded"
RENDER_SCENE_STAGE = "render_scene"
"""Graded profile: final-tier render to linear scene-referred EXR (no Look baked in)."""
FINISH_STAGE = "finish"
"""Graded profile: the Look's finishing as a separate compositor job on the EXRs."""
COMP_STAGE = "comp"
"""Comped profile: the shot's compositing graph as a compositor job on the multilayer EXRs."""
COMP_ID_PARAM = "comp_id"
COMPS_DIR_PARAM = "comps_dir"
RENDER_PASSES_PARAM = "render_passes"
"""A ``render_scene`` param: when true, render passes to multilayer EXR for ``comp``."""
COMPED_PROFILE = "device-shot-comped"
LOOK_ID_PARAM = "look_id"
COLOR_PARAM = "color"
LOOKS_DIR_PARAM = "looks_dir"

BRAND_KIT_PARAM = "brand_kit"
SCREEN_IMAGE_PARAM = "screen_image"
"""The screen image path relative to the assets dir, as the host resolves it."""
SCREEN_PATH_PARAM = "screen_path"
SCREEN_SHA256_PARAM = "screen_sha256"
ASSETS_DIR_PARAM = "assets_dir"
"""The brand kit's ``assets`` root on the control plane; staged to shared storage per render."""

STORAGE_VAR = "{storage}"
"""Flamenco's two-way storage variable; its Linux value is the shared root (``config.shared``)."""
ASSETS_SUBDIR = "assets"

BLENDER_VERSION = "4.5"
WIDTH, HEIGHT = 1920, 1200
PREVIEW_PERCENTAGE = 50
FINAL_SEED = 1153
"""Fixed Cycles seed so final renders are reproducible."""
FINAL_SAMPLES = 256

VISION_QA_METRIC = "vision_qa_requests"
VISION_QA_INPUT_TOKENS = "vision_qa_input_tokens"
VISION_QA_OUTPUT_TOKENS = "vision_qa_output_tokens"
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
    write_atomic(path, sign_job(envelope, key).model_dump_json().encode("utf-8"))
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


_BARE_SHA = re.compile(r"[0-9a-f]{64}")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def frame_identity(path: Path) -> str:
    """Identity of a rendered frame: its shape and decoded pixels, not its file bytes.

    Blender stamps run-varying metadata into the file, so replay compares pixels. A
    frame that cannot be decoded (EXR, 16-bit) falls back to its file sha256.
    """
    try:
        image = decode_png(path.read_bytes())
    except ImageError:
        return _sha256(path)
    h = hashlib.sha256(f"{image.width}x{image.height}x{image.channels}\n".encode())
    h.update(image.pixels)
    return f"pixels:sha256:{h.hexdigest()}"


def frame_hashes(out: Path) -> dict[str, str]:
    """:func:`frame_identity` of every ``frame_*`` file in ``out``, by file name."""
    return {p.name: frame_identity(p) for p in sorted(out.glob("frame_*")) if p.is_file()}


def frame_file_hashes(out: Path) -> dict[str, str]:
    """sha256 of every ``frame_*`` file's bytes, by file name (provenance)."""
    return {p.name: _sha256(p) for p in sorted(out.glob("frame_*")) if p.is_file()}


def _asset_manifest(source: Path) -> list[tuple[str, str]]:
    """``(posix relpath, sha256)`` of every regular file under ``source``, sorted by path."""
    if not source.is_dir():
        raise ValueError(f"brand kit assets dir {source} does not exist")
    files = [p for p in source.rglob("*") if p.is_file() and not p.is_symlink()]
    return sorted((p.relative_to(source).as_posix(), _sha256(p)) for p in files)


def _pin(refs: dict[str, str], value: object) -> None:
    if isinstance(value, dict):
        path, sha = value.get("path"), value.get("sha256")
        if isinstance(path, str) and isinstance(sha, str):
            refs[path] = sha


def referenced_assets(recipe: Recipe) -> dict[str, str]:
    """Asset relpath -> pinned sha256 for every pinned asset ``recipe`` loads.

    ``material.apply_screen`` images, ``device.add`` screen frames or movie and kit
    ``model`` glb, ``world.set_hdri`` environments and ``text.add`` fonts.
    """
    refs: dict[str, str] = {}
    for step in recipe.steps:
        args = step.args
        if step.tool == "material.apply_screen":
            image, sha = args.get("image"), args.get("image_sha256")
            if isinstance(image, str) and isinstance(sha, str):
                refs[image] = sha
        elif step.tool == "device.add":
            screen = args.get("screen")
            if isinstance(screen, dict):
                frames = screen.get("frames")
                for frame in frames if isinstance(frames, list) else []:
                    _pin(refs, frame)
                _pin(refs, screen.get("movie"))
            _pin(refs, args.get("model"))
        elif step.tool == "world.set_hdri":
            _pin(refs, args)
        elif step.tool == "text.add":
            _pin(refs, args.get("font"))
    return refs


def _verify_staged(staged: Path, referenced: Mapping[str, str]) -> None:
    for rel, want in sorted(referenced.items()):
        parts = PurePosixPath(rel).parts
        if PurePosixPath(rel).is_absolute() or ".." in parts:
            raise ValueError(f"referenced asset {rel!r} must be relative to the assets dir")
        path = staged.joinpath(*parts)
        if not path.is_file():
            raise ValueError(f"referenced asset {rel!r} is missing from staged assets {staged}")
        got = _sha256(path)
        if got != want:
            raise ValueError(
                f"referenced asset {rel!r} in {staged} has sha256 {got}, the recipe expects {want}"
            )


def stage_assets(
    source: Path,
    shared: Path,
    referenced: Mapping[str, str],
    extra: Mapping[str, Path] | None = None,
) -> str:
    """Stage ``source`` once under ``<shared>/assets/<sha>/`` and return ``<sha>``.

    ``<sha>`` is the sha256 of the sorted ``(relpath, file sha256)`` manifest of
    the whole tree (licence sidecars included). The copy lands in a temp dir
    unique to this call (threads and hosts never share one) and is renamed into
    place, so concurrent renders copy at most once each and an existing staged
    dir is never rewritten. Every ``referenced`` relpath must then exist in the
    staged dir with its pinned sha256, else :class:`ValueError`.

    ``extra`` maps more relpaths to absolute files (product UI captures, #339)
    staged beside the tree and included in the manifest; an extra relpath
    replaces a tree file of the same name. ``None`` stages the tree alone.
    """
    source = Path(source)
    manifest = _asset_manifest(source)
    files: dict[str, Path] = {}
    if extra:
        for rel in extra:
            parts = PurePosixPath(rel).parts
            if PurePosixPath(rel).is_absolute() or ".." in parts or not parts:
                raise ValueError(f"extra asset {rel!r} must be relative to the assets dir")
        files = {rel: Path(path) for rel, path in extra.items()}
        merged = dict(manifest)
        merged.update({rel: _sha256(path) for rel, path in files.items()})
        manifest = sorted(merged.items())
    sha = hashlib.sha256(
        "".join(f"{rel}\0{digest}\n" for rel, digest in manifest).encode("utf-8")
    ).hexdigest()
    root = Path(shared) / ASSETS_SUBDIR
    staged = root / sha
    if not staged.is_dir():
        root.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(dir=root, prefix=f".tmp-{sha}-"))
        try:
            for rel, _digest in manifest:
                dest = tmp.joinpath(*PurePosixPath(rel).parts)
                dest.parent.mkdir(parents=True, exist_ok=True)
                origin = files.get(rel) or source.joinpath(*PurePosixPath(rel).parts)
                shutil.copyfile(origin, dest)
            tmp.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(tmp, staged)
            except OSError:
                if not staged.is_dir():  # not a lost race: a real failure
                    raise
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    _verify_staged(staged, referenced)
    return sha


def storage_assets_dir(sha: str) -> str:
    """The job's assets dir for staged ``sha``: ``{storage}/assets/<sha>``."""
    return f"{STORAGE_VAR}/{ASSETS_SUBDIR}/{sha}"


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


def _claim_id(config: DeviceShotConfig, candidates: Iterable[str]) -> str:
    """Claim the first candidate id atomically by creating its output dir exclusively.

    ``mkdir(exist_ok=False)`` is atomic, so concurrent runs never share an id.
    """
    config.out_dir("x").parent.mkdir(parents=True, exist_ok=True)
    for candidate in candidates:
        if config.job_file(candidate).exists():
            continue
        try:
            config.out_dir(candidate).mkdir(exist_ok=False)
        except FileExistsError:
            continue
        return candidate
    raise RuntimeError("no job id available")  # pragma: no cover - candidates are unbounded


def fresh_job_id(config: DeviceShotConfig, base: str) -> str:
    """``base``, or ``base-<n>`` for the first n unused; the id is claimed on return."""
    return _claim_id(config, (base if n == 1 else f"{base}-{n}" for n in itertools.count(1)))


def envelope(
    job_id: str,
    recipe: Recipe,
    config: DeviceShotConfig,
    now: datetime,
    frame_end: int = 1,
    frame_start: int = 1,
) -> JobEnvelope:
    """The job envelope of ``recipe`` over frames ``frame_start``..``frame_end``."""
    return JobEnvelope(
        job_id=job_id,
        recipe=recipe,
        recipe_hash=recipe.content_hash,
        frame_start=frame_start,
        frame_end=frame_end,
        output=recipe.shot_id,
        required_tag=config.worker_tag,
        issued_at=now,
        expires_at=now + JOB_TTL,
    )


def blender_job(
    envelope: JobEnvelope,
    job_file: Path,
    out: Path,
    config: DeviceShotConfig,
    tier: Tier,
    assets_dir: str | None = None,
) -> Job:
    payload: dict[str, JsonValue] = {
        "envelope": envelope.model_dump(mode="json"),
        "job_file": str(job_file),
        "worker_tag": config.worker_tag,
        "output_dir": str(out),
    }
    assets = assets_dir or config.assets_dir
    if assets:
        payload["assets_dir"] = assets
    return Job(
        id=envelope.job_id,
        kind="blender",
        tier=tier,
        recipe_hash=envelope.recipe_hash,
        payload=payload,
    )


def executor(
    client: FlamencoApi,
    out: Path,
    config: DeviceShotConfig,
    opts: ExecutorOptions,
    assets_dir: str | None = None,
) -> FlamencoExecutor:
    return FlamencoExecutor(
        client,
        output_dir=str(out),
        sleep=opts.sleep,
        poll_interval=opts.poll_interval,
        timeout_s=opts.timeout_s,
        assets_dir=assets_dir or config.assets_dir,
        clock=opts.clock,
    )


def render_record(out: Path) -> tuple[str, dict[str, str]]:
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


def _assets_source(value: Mapping[str, Any]) -> str | None:
    source = value.get("assets_source")
    return source if isinstance(source, str) and source else None


def _shot_assets_source(ctx: RunContext) -> str | None:
    for nid in ctx.spec.inputs:
        value = ctx.inputs.get(nid)
        if isinstance(value, dict) and isinstance(value.get("recipe"), dict):
            return _assets_source(value)
    return None


def stage_for(
    config: DeviceShotConfig,
    source: str | None,
    recipe: Recipe,
    extra: Mapping[str, Path] | None = None,
) -> tuple[str | None, str | None]:
    """``(job assets_dir, staged sha)``: the override verbatim, else stage ``source``.

    Raises :class:`ValueError` before anything is submitted when there is
    neither an override nor a brand kit assets dir to stage. With ``extra`` files
    (UI captures, #339) ``source`` is always staged: the override dir cannot
    hold the production's captures.
    """
    if config.assets_dir and not extra:
        return config.assets_dir, None
    if source is None and extra:
        raise ValueError(
            "device shots stage the product's UI captures beside the brand kit assets; "
            "the production has no brand kit assets dir (recreate it with --brand-kit)"
        )
    if source is None:
        raise ValueError(
            "device-shot render has no brand kit assets dir to stage "
            "(recreate the production with --brand-kit) and SPECTRACUT_ASSETS_DIR is unset"
        )
    sha = stage_assets(Path(source), config.shared, referenced_assets(recipe), extra)
    return storage_assets_dir(sha), sha


def graded_profile() -> QualityProfile:
    """The ``device-shot-graded`` profile from ``profiles/device-shot-graded.json``."""
    text = resources.files("spectracut.studio").joinpath("profiles", f"{GRADED_PROFILE}.json")
    return QualityProfile.model_validate(json.loads(text.read_text("utf-8")))


def comped_profile() -> QualityProfile:
    """The ``device-shot-comped`` profile from ``profiles/device-shot-comped.json``."""
    text = resources.files("spectracut.studio").joinpath("profiles", f"{COMPED_PROFILE}.json")
    return QualityProfile.model_validate(json.loads(text.read_text("utf-8")))


def _node_comp(ctx: RunContext) -> comp.CompGraph:
    """The node's comp graph: param ``comp_id`` in param ``comps_dir`` (repo ``comps/``)."""
    comp_id = ctx.spec.params.get(COMP_ID_PARAM)
    if not isinstance(comp_id, str) or not comp_id:
        raise ValueError(f"node {ctx.spec.id!r} needs param {COMP_ID_PARAM!r} to pick a comp graph")
    comps_dir = ctx.spec.params.get(COMPS_DIR_PARAM)
    directory = Path(comps_dir) if isinstance(comps_dir, str) and comps_dir else None
    library = comp.load_comps(directory or comp.default_comps_dir())
    if comp_id not in library:
        known = ", ".join(sorted(library))
        raise ValueError(f"no comp graph {comp_id!r}; the library has: {known}")
    return library[comp_id]


def _node_look(ctx: RunContext) -> Look:
    """The node's Look: param ``look_id`` (else the kit default) in param ``brand_kit``."""
    kit = ctx.spec.params.get(BRAND_KIT_PARAM)
    if kit is None:
        raise ValueError(f"node {ctx.spec.id!r} needs param {BRAND_KIT_PARAM!r} to pick a Look")
    look_id = ctx.spec.params.get(LOOK_ID_PARAM)
    return resolve_look(BrandKit.model_validate(kit), str(look_id) if look_id is not None else None)


def _node_pipeline(ctx: RunContext) -> ColorPipeline:
    """The production's colour pipeline from param ``color`` (AgX, Rec.1886 by default)."""
    pipeline = ColorPipeline.model_validate(ctx.spec.params.get(COLOR_PARAM) or {})
    pipeline.require_blender(BLENDER_VERSION)
    return pipeline


def scene_render_recipe(
    base: Recipe, look: Look, pipeline: ColorPipeline, *, passes: bool = False
) -> Recipe:
    """``base`` at the final tier, colour-managed, with ``look``'s render rules, to linear EXR.

    ``image_settings.color_management = FOLLOW_SCENE`` with OpenEXR writes
    scene-linear values: the view transform is applied later, in finishing.
    With ``passes`` the output is 32-bit multilayer EXR with the view-layer
    passes :func:`comp.passes_steps` enables, for the ``comp`` stage.
    """
    steps = list(base.steps) + tier_steps(Tier.FINAL, start=len(base.steps))
    steps += pipeline.render_steps(start=len(steps))
    steps += render_rules(look, start=len(steps))
    if passes:
        steps += comp.passes_steps(start=len(steps))
        return base.model_copy(update={"steps": steps})
    steps.append(
        RecipeStep(
            index=len(steps),
            tool="render.set_output",
            args={"format": "OPEN_EXR", "color_depth": "16"},
        )
    )
    return base.model_copy(update={"steps": steps})


def _load_brief(data: Any) -> Brief | CreativeBrief:
    try:
        return Brief.model_validate(data)
    except ValueError:
        return CreativeBrief.model_validate(data)


def qa_look(ctx: RunContext) -> Look:
    """The brand kit's default Look for QA, else a neutral Look with default thresholds."""
    kit = ctx.spec.params.get(BRAND_KIT_PARAM)
    if kit is not None:
        try:
            return resolve_look(BrandKit.model_validate(kit))
        except ValueError:
            pass
    return Look(id="neutral")


def render_qa(
    ctx: RunContext, job_id: str, out: Path, look: Look, store: QAReportStore | None
) -> dict[str, JsonValue]:
    """Run QA on the rendered frames and save the report; a QA failure never raises."""
    from spectracut.qa.copy_ocr import available_reader
    from spectracut.qa.report import QAReportStore, run_qa

    kit_param = ctx.spec.params.get(BRAND_KIT_PARAM)
    try:
        kit = BrandKit.model_validate(kit_param) if kit_param is not None else None
        frames = [p.read_bytes() for p in sorted(out.glob("frame_*")) if p.is_file()]
        report = run_qa(job_id, frames, look, reader=available_reader(), brand_kit=kit)
        (store or QAReportStore()).save(report)
    except Exception as exc:  # noqa: BLE001 - QA must not fail the render; export is gated
        return {"passed": False, "failures": ["qa.error"], "render_id": job_id, "error": str(exc)}
    failures: list[JsonValue] = [c.name for c in report.failures()]
    return {"passed": report.passed, "failures": failures, "render_id": job_id}


def vision_qa_usage(usage: Usage, judge: VisionJudge) -> Usage:
    """``usage`` plus one ``vision_qa_requests`` and the judge's last token usage, if any."""
    ai = {**usage.ai_usage, VISION_QA_METRIC: usage.ai_usage.get(VISION_QA_METRIC, 0) + 1}
    tokens = judge_usage(judge)
    if tokens:
        for metric, amount in (
            (VISION_QA_INPUT_TOKENS, tokens["input_tokens"]),
            (VISION_QA_OUTPUT_TOKENS, tokens["output_tokens"]),
        ):
            ai[metric] = ai.get(metric, 0) + amount
    return usage.model_copy(update={"ai_usage": ai})


def device_shot_runner(
    graph: ProductionGraph,
    config: DeviceShotConfig,
    *,
    client: FlamencoApi,
    judge: VisionJudge | None = None,
    key: Ed25519PrivateKey,
    now: Callable[[], datetime] | None = None,
    options: ExecutorOptions | None = None,
    qa_store: QAReportStore | None = None,
) -> Runner:
    """A graph Runner for the ``device-shot``, ``-graded`` and ``-comped`` profiles' stages.

    Every render is QA'd and its report saved in ``qa_store`` (the
    :func:`~spectracut.studio.store.spectracut_home` store by default) under the job id.
    """
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
        source = params.get(ASSETS_DIR_PARAM)
        if isinstance(source, str) and source:
            output["assets_source"] = source
        return RunResult(output=output)

    def render(ctx: RunContext) -> RunResult:
        base = _base_recipe(ctx)
        scene_referred = ctx.spec.kind == RENDER_SCENE_STAGE
        passes = scene_referred and bool(ctx.spec.params.get(RENDER_PASSES_PARAM))
        if scene_referred:
            look = _node_look(ctx)
            pipeline = _node_pipeline(ctx)
            recipe = scene_render_recipe(base, look, pipeline, passes=passes)
        else:
            recipe = with_tier(base, ctx.tier)
        source = _shot_assets_source(ctx)
        assets_dir, assets_sha = _stage_for(config, source, recipe)
        job_id = _fresh_job_id(
            config, f"{graph.production_id}-{ctx.spec.id}-{recipe.content_hash[7:19]}"
        )
        envelope = _envelope(job_id, recipe, config, clock_now())
        job_file = sign_to_shared(envelope, key, config.shared)
        out = config.out_dir(job_id)
        job = _blender_job(envelope, job_file, out, config, ctx.tier, assets_dir)
        queue = JobQueue([_executor(client, out, config, opts, assets_dir)], now=clock_now)
        result = queue.as_runner(lambda _ctx: job, sleep=opts.sleep)(ctx)
        scene, frames = _render_record(out)
        flamenco = result.output if isinstance(result.output, dict) else {}
        output: dict[str, JsonValue] = {
            "recipe": base.model_dump(mode="json"),
            "recipe_hash": recipe.content_hash,
            "tier": ctx.tier.value,
            "scene_graph_hash": scene,
            "frames": dict(frames),
            "frame_files": dict(frame_file_hashes(out)),
            "output_dir": str(out),
            "worker_tag": flamenco.get("worker_tag", config.worker_tag),
            "job_id": job_id,
            "flamenco_job_id": flamenco.get("flamenco_job_id"),
        }
        if source is not None:
            output["assets_source"] = source
        if assets_sha is not None:
            output["assets_sha"] = assets_sha
        if scene_referred:
            output[LOOK_ID_PARAM] = look.id
            output["render_rules"] = [{"tool": s.tool, "args": s.args} for s in render_rules(look)]
        if passes:
            output["render_passes"] = list(comp.RENDER_PASSES)
        output["qa"] = _render_qa(
            ctx, job_id, out, look if scene_referred else _qa_look(ctx), qa_store
        )
        usage = result.usage
        if ctx.tier is Tier.PREVIEW and not scene_referred:
            active = judge
            if active is None:
                from spectracut.providers.base import select_vision_judge

                active = select_vision_judge()
            verdict = active.score(out / sorted(frames)[0], VISION_RUBRIC)
            output["vision_qa"] = {"score": float(verdict.score), "notes": verdict.notes}
            usage = vision_qa_usage(usage, active)
        return RunResult(output=output, usage=usage)

    def run(ctx: RunContext) -> RunResult:
        if ctx.spec.kind == SHOT_STAGE:
            return shot(ctx)
        if ctx.spec.kind in RENDER_STAGES or ctx.spec.kind == RENDER_SCENE_STAGE:
            return render(ctx)
        if ctx.spec.kind == COMP_STAGE:
            return comp.comp_runner(
                ctx,
                graph=_node_comp(ctx),
                production_id=graph.production_id,
                config=config,
                client=client,
                key=key,
                now=clock_now,
                options=opts,
            )
        if ctx.spec.kind == FINISH_STAGE:
            looks_dir = ctx.spec.params.get(LOOKS_DIR_PARAM)
            return finish_runner(
                ctx,
                look=_node_look(ctx),
                pipeline=_node_pipeline(ctx),
                production_id=graph.production_id,
                config=config,
                client=client,
                key=key,
                now=clock_now,
                options=opts,
                looks_dir=Path(looks_dir) if isinstance(looks_dir, str) and looks_dir else None,
            )
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
    files: dict[str, str] = field(default_factory=dict)

    @property
    def scene_matches(self) -> bool:
        return (
            self.scene_graph_hash is not None
            and self.scene_graph_hash == self.original_scene_graph_hash
        )

    @property
    def frames_match(self) -> bool:
        return bool(self.frames) and not self._differing()

    def _same(self, name: str) -> bool:
        recorded = self.original_frames.get(name)
        if recorded is not None and _BARE_SHA.fullmatch(recorded):
            # recorded before pixel identities: it is a file sha256
            return recorded == self.files.get(name, self.frames.get(name))
        return recorded is not None and recorded == self.frames.get(name)

    def _differing(self) -> list[str]:
        return [
            n for n in sorted(set(self.original_frames) | set(self.frames)) if not self._same(n)
        ]

    @property
    def matches(self) -> bool:
        return self.scene_matches and self.frames_match

    def mismatches(self) -> list[str]:
        out: list[str] = []
        if not self.scene_matches:
            out.append("scene_graph_hash")
        return out + self._differing()


def _fresh_replay_id(config: DeviceShotConfig, job_id: str) -> str:
    return _claim_id(config, (f"{job_id}-replay-{n}" for n in itertools.count(1)))


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
    recorded_sha = recorded.get("assets_sha")
    staged = config.shared / ASSETS_SUBDIR / str(recorded_sha)
    if not config.assets_dir and isinstance(recorded_sha, str) and staged.is_dir():
        # the recorded render's staged assets are still there: replay against exactly those
        _verify_staged(staged, referenced_assets(recipe))
        assets_dir: str | None = storage_assets_dir(recorded_sha)
    else:
        assets_dir, _sha = _stage_for(config, _assets_source(recorded), recipe)
    job_id = _fresh_replay_id(config, original_job)
    out = config.out_dir(job_id)
    envelope = _envelope(job_id, recipe, config, (now or opts.now)())
    job_file = sign_to_shared(envelope, key, config.shared)
    job = _blender_job(envelope, job_file, out, config, tier, assets_dir)
    job.node_id = node_id
    _executor(client, out, config, opts, assets_dir).run(
        job
    )  # direct: no FrameCache, no graph cache
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
        files=frame_file_hashes(out),
    )


# Private names kept for internal call sites; the public helpers above are reused by
# ``studio.departments.shots``.
_fresh_job_id = fresh_job_id
_envelope = envelope
_blender_job = blender_job
_executor = executor
_render_record = render_record
_stage_for = stage_for
_render_qa = render_qa
_qa_look = qa_look
