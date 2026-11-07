# SPDX-License-Identifier: AGPL-3.0-or-later
"""``spectracut studio``: create, run, inspect and approve productions."""

from __future__ import annotations

import hashlib
import logging
import os
import socket
import threading
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer
from pydantic import JsonValue, ValidationError

from spectracut.critique.scene_verifier import scene_critic, style_frames_critic
from spectracut.edit.preview import AnimaticPreview, FfmpegPreview
from spectracut.providers.base import (
    ProviderError,
    ReferenceJudge,
    TextProvider,
    VisionJudge,
    VisionScore,
    select_text_provider,
    select_vision_judge,
)
from spectracut.providers.config import config_path
from spectracut.providers.local.installed import installed_models
from spectracut.providers.local.runtime import ModelRuntime
from spectracut.queue.executors.flamenco import FlamencoApi
from spectracut.spec.brand import BrandKit, load_brand_kit
from spectracut.spec.brief import Brief, CreativeBrief
from spectracut.spec.product import (
    FrontendRoute,
    ProductSource,
    frontend_pages,
    load_product_source,
    product_routes,
)
from spectracut.spec.references import (
    DEFAULT_REFERENCE_SET,
    ReferenceError,
    ReferenceSet,
    load_reference_set,
)
from spectracut.storyboard.models import ArtDirection
from spectracut.studio import device_shot as ds
from spectracut.studio import intake
from spectracut.studio.autonomy import AutonomyPolicy, BudgetApproval, load_autonomy
from spectracut.studio.department import Department
from spectracut.studio.departments import all_departments
from spectracut.studio.departments import art as art_depts
from spectracut.studio.departments import discovery as discovery_depts
from spectracut.studio.departments import finishing as finishing_depts
from spectracut.studio.departments import references as reference_depts
from spectracut.studio.departments import shots as shot_depts
from spectracut.studio.departments import sound as sound_depts
from spectracut.studio.departments.capture import UI_CAPTURE, CaptureRunner
from spectracut.studio.departments.style_frame_farm import (
    FarmRecipeRenderer,
    FarmStyleFrameRenderer,
    SceneStyleFrameRenderer,
)
from spectracut.studio.estimate import BRIEF_PARAM, breakdown, estimate
from spectracut.studio.graph import (
    ProductionGraph,
    RevisionNote,
    RunContext,
    Runner,
    RunReport,
    RunResult,
)
from spectracut.studio.node import NodeSpec, Tier
from spectracut.studio.producer import (
    BudgetCaps,
    Producer,
    edge_problems,
    product_capture_nodes,
    scheduled,
)
from spectracut.studio.profiles import available_profiles, load_profile
from spectracut.studio.runner import CapacityLimits, DepartmentRunner
from spectracut.studio.scene_builder import SceneBrief, SceneBuild, SceneBuilder
from spectracut.studio.store import ProductionError, ProductionStore, write_atomic

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from spectracut.audio.music_edit import Stretcher
    from spectracut.audio.render import ArdourRunner
    from spectracut.audio.vo import VoiceOver
    from spectracut.providers.local.registry import LocalProvider

studio_app = typer.Typer(
    no_args_is_help=True,
    help="Productions: create one from a brief, run it, review its status, pass human gates.",
)

DEPARTMENTS: dict[str, type[Department]] = all_departments()
"""Departments available to ``studio run``, by name (the registry in ``studio.departments``)."""

log = logging.getLogger(__name__)

text_provider_factory: Callable[[], TextProvider] | None = select_text_provider
"""Builds the text provider departments run through: Claude sign-in, else Codex sign-in."""


class Production:
    """A stored production as ``studio run`` sees it."""

    def __init__(self, production_id: str, store: ProductionStore) -> None:
        self.id = production_id
        self.store = store
        self._models: ModelRuntimeResult | None = None
        self._models_lock = threading.Lock()

    def model_runtime(self) -> ModelRuntimeResult:
        """The model runtime for this run, resolved once (:data:`model_runtime_factory`).

        Sound and preproduction share it, so model jobs take leases from one pool.
        """
        with self._models_lock:
            if self._models is None:
                self._models = model_runtime_factory(self.store.home)
            return self._models

    @property
    def brief(self) -> Brief | CreativeBrief:
        return self.store.brief(self.id)


@dataclass
class DeviceShotDeps:
    """What the ``device-shot`` runner and ``studio replay`` talk to."""

    config: ds.DeviceShotConfig
    client: FlamencoApi
    key: Ed25519PrivateKey
    judge: VisionJudge | None = None
    """``None`` picks :func:`~spectracut.providers.base.select_vision_judge` on first use."""
    options: ds.ExecutorOptions | None = None


def default_device_shot(production: Production) -> DeviceShotDeps:
    """Real farm dependencies from the environment and the SpectraCut home's signing key."""
    from spectracut.queue.flamenco import FlamencoClient
    from spectracut.queue.signing import load_private_key

    config = ds.DeviceShotConfig.from_env()
    key = load_private_key(home=production.store.home)
    return DeviceShotDeps(config=config, client=FlamencoClient(config.flamenco_url), key=key)


device_shot_factory: Callable[[Production], DeviceShotDeps] = default_device_shot
"""Builds the device-shot dependencies; tests inject a fake Flamenco and a mock judge."""

animatic_preview_factory: Callable[[], AnimaticPreview] = FfmpegPreview
"""Builds the animatic's mp4 preview encoder (ffmpeg); tests inject a fake."""

SCREEN_ASSET = Path("ui") / "dashboard.png"
"""The UI screen image, relative to the brand kit's ``assets`` dir."""


MODEL_HOST_ENV = "SPECTRACUT_MODEL_HOST"
"""Names the model host (a registered host alias); else ``[models] host`` in the config."""

ModelRuntimeResult = tuple[ModelRuntime | None, str | None]
"""A model runtime, or ``None`` and the problem that names the missing model host."""


def _configured_model_host() -> str | None:
    """The model host from ``SPECTRACUT_MODEL_HOST``, else ``[models] host`` in the config."""
    wanted = os.environ.get(MODEL_HOST_ENV, "").strip()
    if wanted:
        return wanted
    path = config_path()
    if not path.is_file():
        return None
    try:
        table = tomllib.loads(path.read_text(encoding="utf-8")).get("models", {})
    except (OSError, tomllib.TOMLDecodeError):
        return None
    host = table.get("host") if isinstance(table, dict) else None
    return host.strip() if isinstance(host, str) and host.strip() else None


def model_host_name(home: Path) -> str | None:
    """The model host's name, resolved as :func:`model_runtime` does, or ``None``.

    :data:`MODEL_HOST_ENV`, else ``[models] host``, else the single registered host with
    GPUs. ``None`` when none is configured and zero or several GPU hosts are registered,
    or the host registry cannot be read.
    """
    from spectracut.cli.render import load_hosts

    wanted = _configured_model_host()
    if wanted is not None:
        return wanted
    try:
        gpu_hosts = [h for h in load_hosts(home) if h.gpus]
    except (OSError, ValueError):
        return None
    return gpu_hosts[0].name if len(gpu_hosts) == 1 else None


def _note_once(graph: ProductionGraph, node_id: str, text: str) -> None:
    """Pin the studio's note ``text`` to ``node_id`` unless the node already carries it."""
    text = text[:4000]
    if any(n.text == text for n in graph.notes(node_id)):
        return
    note = RevisionNote(node_id=node_id, text=text, author="studio", created_at=datetime.now(UTC))
    graph.add_note(node_id, note)


def model_runtime(home: Path) -> ModelRuntimeResult:
    """The model runtime on the model host from setup state, or the problem naming the host.

    The model host is :data:`MODEL_HOST_ENV`, else ``[models] host``, else the single
    registered host with GPUs (``legion``). On the model host itself with the host
    loaders installed (``spectracut_host``) models load in-process; elsewhere they
    run on the host over ssh (:class:`~spectracut.providers.local.remote.RemoteLoader`).
    """
    from spectracut.cli.render import load_hosts
    from spectracut.providers.local.remote import RemoteLoader
    from spectracut.queue.executors.base import LeasePool

    wanted = _configured_model_host()
    try:
        hosts = load_hosts(home)
    except (OSError, ValueError) as exc:
        return None, f"cannot read the host registry for the model host: {exc}"
    if wanted is not None:
        slot = next((h for h in hosts if h.name == wanted), None)
        if slot is None:
            return None, (
                f"model host {wanted!r} is not registered: run "
                f"`spectracut hosts add {wanted} --gpu MODEL:VRAM_GB`"
            )
    else:
        gpu_hosts = [h for h in hosts if h.gpus]
        if not gpu_hosts:
            return None, (
                "no model host: register the GPU model host (legion) with "
                "`spectracut hosts add legion --gpu MODEL:VRAM_GB`, or set "
                f"{MODEL_HOST_ENV} or [models] host"
            )
        if len(gpu_hosts) > 1:
            names = ", ".join(sorted(h.name for h in gpu_hosts))
            return None, (
                f"several GPU hosts ({names}): pick the model host with "
                f"{MODEL_HOST_ENV} or [models] host"
            )
        slot = gpu_hosts[0]
    pool = LeasePool(slot.cpus, slot.mem_gb, slot.gpus)
    installed = installed_models(home, slot.name)
    if slot.name == socket.gethostname():
        try:
            from spectracut_host.models import (  # type: ignore[import-not-found,unused-ignore]
                load_model,
            )
        except ImportError:
            pass
        else:
            return ModelRuntime(load_model, pool, host=slot.name, installed=installed), None
    loader = RemoteLoader(slot.name, slot.profile.os)
    return ModelRuntime(loader, pool, host=slot.name, installed=installed), None


model_runtime_factory: Callable[[Path], ModelRuntimeResult] = model_runtime
"""Resolves the model runtime from a SpectraCut home; tests inject a fake runtime."""


@dataclass
class SoundDeps:
    """What the sound runner (voiceover, sound design, music, mix) talks to.

    ``None`` fields take :class:`~spectracut.studio.departments.sound.SoundRunner`'s
    defaults: ElevenLabs final VO when configured else local TTS temp VO, the local
    audio models on ``runtime``, and Ardour for the mix.
    """

    runtime: ModelRuntime | None = None
    runtime_problem: str | None = None
    """Why there is no ``runtime`` (names the model host); raised when a local model is needed."""
    voice_over: VoiceOver | None = None
    elevenlabs_configured: Callable[[], bool] = sound_depts.elevenlabs_configured
    providers: Mapping[str, LocalProvider] | None = None
    ardour: ArdourRunner | None = None
    stretcher: Stretcher | None = None
    picture: AnimaticPreview | None = None
    """Renders each picture clip for foley; ``None`` means sound design adds no foley."""


def default_sound(production: Production) -> SoundDeps:
    """Real sound dependencies: the model runtime on the model host from setup state."""
    runtime, problem = production.model_runtime()
    return SoundDeps(runtime=runtime, runtime_problem=problem, picture=animatic_preview_factory())


sound_factory: Callable[[Production], SoundDeps] = default_sound
"""Builds the sound dependencies; tests inject a fake voice, music model and Ardour."""

SOUND_KINDS = frozenset(
    {sound_depts.VOICEOVER, sound_depts.SOUND_DESIGN, sound_depts.MUSIC, sound_depts.MIX}
)
"""Node kinds :class:`~spectracut.studio.departments.sound.SoundRunner` builds."""


def default_runner(production: Production, graph: ProductionGraph) -> Runner:
    """Run nodes through :data:`DEPARTMENTS`; a node with no department fails clearly.

    Productions on the ``device-shot`` profile run through the device-shot runner.
    Other profiles render ``template_shots`` and ``shots`` nodes on the farm through
    :class:`~spectracut.studio.departments.shots.ShotRunner` (boards no template fits
    are built by the scene builder and rendered as animated sequences at the board's
    duration, lens and move), and build ``style_frames`` and ``animatic`` through
    :class:`~spectracut.studio.departments.art.PreproductionRunner`: style frames get
    a local concept image (when a local image model is reachable) and are built by the
    scene builder (:class:`SceneStyleFrameRenderer`: the text provider builds, the
    vision judge verifies, preview renders on the farm) and re-rendered at the final
    tier, with every frame listed for the gate's review page; the animatic is cut in
    code to the voiceover's audio: its boards render at the preview tier on the farm
    and its review mp4 is encoded by ffmpeg (:data:`animatic_preview_factory`). Farm dependencies
    (:data:`device_shot_factory`) are built on the first shot or style-frame render,
    so text-only runs never need the Flamenco env or the signing key. ``edit``,
    ``render`` and ``delivery`` nodes are built in code by
    :class:`~spectracut.studio.departments.finishing.FinishingRunner` (on the same lazily
    built farm dependencies), never by the text runner. ``voiceover``,
    ``sound_design``, ``music`` and ``mix`` are built in code by
    :class:`~spectracut.studio.departments.sound.SoundRunner` on dependencies from
    :data:`sound_factory`, built on the first sound node.
    """
    if graph.profile is not None and graph.profile.name == ds.PROFILE:
        deps = device_shot_factory(production)
        return ds.device_shot_runner(
            graph,
            deps.config,
            client=deps.client,
            judge=deps.judge,
            key=deps.key,
            options=deps.options,
        )
    inner: DepartmentRunner | None = None
    shots: shot_depts.ShotRunner | None = None
    preproduction: art_depts.PreproductionRunner | None = None
    farm: DeviceShotDeps | None = None
    finishing: finishing_depts.FinishingRunner | None = None
    sound: sound_depts.SoundRunner | None = None
    capture: CaptureRunner | None = None
    lock = threading.Lock()
    farm_lock = threading.Lock()

    def farm_deps() -> DeviceShotDeps:
        nonlocal farm
        with farm_lock:
            if farm is None:
                farm = device_shot_factory(production)
            return farm

    provider: list[TextProvider] = []
    judge: list[VisionJudge] = []
    profile = graph.profile or load_profile("standard")
    production_dir = production.store.path(production.id)

    def builder_text() -> TextProvider:
        with farm_lock:
            if not provider:
                if text_provider_factory is None:
                    raise LookupError("no text provider configured for the scene builder")
                provider.append(text_provider_factory())
            return provider[0]

    def builder_judge() -> VisionJudge:
        found = farm_deps().judge
        with farm_lock:
            if not judge:
                judge.append(found if found is not None else select_vision_judge())
            return judge[0]

    def build_scene(brief: SceneBrief) -> SceneBuild:
        """One scene builder per shot brief (its critic is bound to the description)."""
        workdir = production_dir / "shots" / "scene"
        builder = SceneBuilder(
            builder_text(),
            FarmRecipeRenderer(farm_deps, graph.production_id, workdir),
            scene_critic(
                builder_judge(),
                profile=profile,
                description=brief.description,
                art=brief.art,
                brand_kit=brief.brand_kit,
            ),
            profile,
            workdir,
        )
        return builder.build(brief)

    def text(ctx: RunContext) -> RunResult:
        nonlocal inner
        name = ctx.spec.department
        if name not in DEPARTMENTS:
            raise LookupError(f"no department registered for {name!r}")
        with lock:
            if inner is None:
                if text_provider_factory is None:
                    raise LookupError("no text provider configured for departments")
                inner = DepartmentRunner(
                    DEPARTMENTS.values(),
                    text_provider_factory(),
                    brief=production.brief,
                    graph=graph,
                    limits=CapacityLimits(gpu_leases=1),
                )
            runner = inner
        return runner(ctx)

    def pre(ctx: RunContext) -> RunResult:
        nonlocal preproduction
        with lock:
            if preproduction is None:
                workdir = production.store.path(production.id) / "preproduction"
                runtime, _problem = production.model_runtime()
                preproduction = art_depts.PreproductionRunner(
                    text,
                    DEPARTMENTS,
                    explorer=art_depts.default_explorer(runtime=runtime),
                    renderer=SceneStyleFrameRenderer(
                        builder_text,
                        builder_judge,
                        profile,
                        farm_deps,
                        graph.production_id,
                        workdir,
                        brand_kit=lambda: _brand_kit_of(graph),
                    ),
                    voice=art_depts.default_voice(runtime=runtime),
                    workdir=workdir,
                    board_renderer=FarmStyleFrameRenderer(
                        farm_deps,
                        graph.production_id,
                        workdir / "boards",
                        tier=Tier.PREVIEW,
                        node_id=art_depts.ANIMATIC,
                    ),
                    preview=animatic_preview_factory(),
                    media_root=production_dir,
                    concept_note=art_depts.concept_skip_reason(runtime=runtime),
                    note=lambda node_id, text: _note_once(graph, node_id, text),
                    reference_judge=LazyReferenceJudge(builder_judge),
                )
            runner = preproduction
        return runner(ctx)

    def finish(ctx: RunContext) -> RunResult:
        nonlocal finishing
        with lock:
            if finishing is None:
                deps = farm_deps()
                brief = production.brief
                finishing = finishing_depts.FinishingRunner(
                    text,
                    DEPARTMENTS,
                    config=deps.config,
                    client=deps.client,
                    key=deps.key,
                    judge=deps.judge,
                    production_id=graph.production_id,
                    workdir=production.store.path(production.id) / "finishing",
                    options=deps.options,
                    brief=brief if isinstance(brief, Brief) else None,
                    reference_judge=LazyReferenceJudge(builder_judge),
                )
            runner = finishing
        return runner(ctx)

    def audio(ctx: RunContext) -> RunResult:
        nonlocal sound
        with lock:
            if sound is None:
                deps = sound_factory(production)
                sound = sound_depts.SoundRunner(
                    text,
                    DEPARTMENTS,
                    workdir=production.store.path(production.id) / "sound",
                    voice_over=deps.voice_over,
                    elevenlabs_configured=deps.elevenlabs_configured,
                    runtime=deps.runtime,
                    runtime_problem=deps.runtime_problem,
                    providers=deps.providers,
                    ardour=deps.ardour,
                    stretcher=deps.stretcher,
                    picture=deps.picture,
                )
            runner = sound
        return runner(ctx)

    def ui_capture(ctx: RunContext) -> RunResult:
        nonlocal capture
        with lock:
            if capture is None:
                capture = CaptureRunner(
                    text,
                    DEPARTMENTS,
                    workdir=production_dir / "capture",
                    source=production.store.meta(production.id).product,
                )
            runner = capture
        return runner(ctx)

    def run(ctx: RunContext) -> RunResult:
        nonlocal shots
        if ctx.spec.kind == UI_CAPTURE:
            return ui_capture(ctx)
        if ctx.spec.kind == art_depts.BRAND_KIT:
            return art_depts.build_brand_kit(ctx)
        if ctx.spec.kind == reference_depts.REFERENCES:
            return reference_depts.build_references(ctx)
        if ctx.spec.kind in SOUND_KINDS:
            return audio(ctx)
        if ctx.spec.kind in FINISHING_KINDS:
            return finish(ctx)
        if ctx.spec.kind in (art_depts.STYLE_FRAMES, art_depts.ANIMATIC):
            return pre(ctx)
        if ctx.spec.kind not in (shot_depts.TEMPLATE_SHOTS, shot_depts.SHOTS):
            return text(ctx)
        with lock:
            if shots is None:
                deps = farm_deps()
                shots = shot_depts.ShotRunner(
                    text,
                    DEPARTMENTS,
                    config=deps.config,
                    client=deps.client,
                    key=deps.key,
                    judge=deps.judge,
                    production_id=graph.production_id,
                    options=deps.options,
                    scene_builder=build_scene,
                    art_for=_art_for(graph),
                )
            runner = shots
        return runner(ctx)

    return run


FINISHING_KINDS = frozenset(
    {finishing_depts.EDIT, finishing_depts.RENDER, finishing_depts.DELIVERY}
)
"""Node kinds :class:`~spectracut.studio.departments.finishing.FinishingRunner` builds."""


runner_factory: Callable[[Production, ProductionGraph], Runner] = default_runner
"""Builds the runner ``studio run`` uses; tests and embedders may replace it."""

HomeOpt = Annotated[
    Path | None,
    typer.Option("--home", help="SpectraCut home (default $SPECTRACUT_HOME or XDG data dir)."),
]
IdArg = Annotated[str, typer.Argument(metavar="PRODUCTION", help="Production id.")]


def _fail(message: str) -> typer.Exit:
    typer.echo(f"error: {message}", err=True)
    return typer.Exit(2)


def _load_brief(path: Path) -> Brief | CreativeBrief:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise _fail(f"cannot read brief {path}: {exc.strerror or exc}") from exc
    try:
        return Brief.model_validate_json(data)
    except ValidationError as brief_exc:
        try:
            return CreativeBrief.model_validate_json(data)
        except ValidationError:
            raise _fail(
                f"{path} is not a valid brief (JSON): {brief_exc.error_count()} error(s), "
                f"first: {brief_exc.errors()[0]['msg']}"
            ) from brief_exc


def _load(store: ProductionStore, production_id: str) -> ProductionGraph:
    try:
        graph, _ = store.load(production_id)
    except ProductionError as exc:
        raise _fail(str(exc)) from exc
    return graph


AutonomyOpt = Annotated[
    Path | None,
    typer.Option("--autonomy", help="Autonomy policy JSON: delegated gates, budget ceiling."),
]
DelegateOpt = Annotated[
    list[str] | None,
    typer.Option("--delegate", help="Hand this human gate (node kind) to the delegate."),
]


def _policy(
    path: Path | None, delegate: list[str] | None, base: AutonomyPolicy | None
) -> AutonomyPolicy | None:
    """The autonomy policy from ``--autonomy`` and ``--delegate``, on top of ``base``."""
    policy = base
    if path is not None:
        try:
            policy = load_autonomy(path)
        except ValueError as exc:
            raise _fail(str(exc)) from exc
    if delegate:
        policy = policy or AutonomyPolicy()
        kinds = (*policy.delegate_kinds, *(k for k in delegate if k not in policy.delegate_kinds))
        try:
            policy = AutonomyPolicy.model_validate({**policy.model_dump(), "delegate_kinds": kinds})
        except ValidationError as exc:
            raise _fail(f"--delegate: {exc.errors()[0]['msg']}") from exc
    return policy


def _save(store: ProductionStore, production_id: str, graph: ProductionGraph) -> None:
    try:
        store.save(production_id, graph)
    except (ProductionError, OSError) as exc:
        raise _fail(f"cannot save production {production_id!r}: {exc}") from exc


@studio_app.command("new")
def new(
    brief_file: Annotated[
        Path | None, typer.Argument(metavar="[BRIEF]", help="Brief JSON file (or use --idea).")
    ] = None,
    idea: Annotated[
        str | None, typer.Option("--idea", help="A vague idea; the brief is generated from it.")
    ] = None,
    source: Annotated[
        list[str] | None,
        typer.Option("--source", help="README, file path or URL to source claims from."),
    ] = None,
    headless: Annotated[
        bool, typer.Option("--headless", help="With --idea: take every recommended option.")
    ] = False,
    profile: Annotated[
        str, typer.Option("--profile", help=f"Quality profile: {', '.join(available_profiles())}.")
    ] = "standard",
    production_id: Annotated[
        str | None, typer.Option("--id", help="Production id (default: the brief's id).")
    ] = None,
    home: HomeOpt = None,
    force: Annotated[bool, typer.Option("--force", help="Replace an existing production.")] = False,
    brand_kit: Annotated[
        Path | None,
        typer.Option("--brand-kit", help="brand-kit.yaml (required for profile device-shot)."),
    ] = None,
    duration: Annotated[
        float, typer.Option("--duration", min=0.1, help="Film length in seconds.")
    ] = 60.0,
    cap_elevenlabs: Annotated[
        int | None,
        typer.Option("--cap-elevenlabs-characters", min=0, help="ElevenLabs character cap."),
    ] = None,
    cap_gpu: Annotated[
        float | None, typer.Option("--cap-gpu-minutes", min=0, help="GPU minutes cap.")
    ] = None,
    cap_ai: Annotated[
        int | None, typer.Option("--cap-ai-requests", min=0, help="AI requests cap.")
    ] = None,
    autonomy: AutonomyOpt = None,
    delegate: DelegateOpt = None,
    product: Annotated[
        Path | None,
        typer.Option(
            "--product", help="product.yaml: where the product's repo and front end live."
        ),
    ] = None,
) -> None:
    """Create a production from a brief file and print its estimate.

    Over-cap nodes are held by ``studio run`` until ``studio approve-budget``,
    unless the autonomy policy's ceiling covers them.
    """
    if (brief_file is None) == (idea is None):
        raise _fail("give exactly one of BRIEF or --idea")
    if brief_file is not None:
        brief = _load_brief(brief_file)
    else:
        assert idea is not None
        brief = _autofill(idea, source or [], headless, brand_kit)
    caps = BudgetCaps(elevenlabs_characters=cap_elevenlabs, gpu_minutes=cap_gpu, ai_requests=cap_ai)
    try:
        path, graph = create_production(
            ProductionStore(home),
            brief,
            production_id=production_id,
            profile=profile,
            brand_kit=brand_kit,
            duration=duration,
            caps=caps,
            policy=lambda: _policy(autonomy, delegate, None),
            force=force,
            from_idea=idea is not None,
            product=product,
        )
    except (ProductionError, ValueError, OSError) as exc:
        raise _fail(str(exc)) from exc
    pid = graph.production_id
    quality = graph.profile
    assert quality is not None
    device = quality.name == ds.PROFILE
    typer.echo(f"production {pid} created at {path}")
    if product is not None:
        meta = ProductionStore(home).meta(pid)
        assert meta.product is not None
        routes = len(meta.product.routes) or _product_route_count(graph)
        typer.echo(f"product {meta.product.id}: {routes} routes from {meta.product.frontend}")
        if discovery_depts.PRODUCT not in graph.order():
            typer.echo(
                "no script node: the product source is recorded but no screen map is planned"
            )
        elif UI_CAPTURE in graph.order():
            typer.echo(f"{UI_CAPTURE} node planned: captures every mapped screen per device")
    if not device and brand_kit is None and art_depts.ART_DIRECTION in quality.stages:
        typer.echo("no --brand-kit: art direction has no brand kit to draw from")
    est = estimate(graph, DEPARTMENTS)
    for line in est.lines():
        typer.echo(line)
    if not device and est.unknown_departments:
        typer.echo("no department registered yet for: " + ", ".join(est.unknown_departments))


class LazyReferenceJudge:
    """The run's vision judge as a :class:`ReferenceJudge`, selected on first comparison.

    A judge that cannot compare two images raises :class:`ProviderError`, which the
    reference comparison records as a note (it never fails the node).
    """

    name = "vision-judge"

    def __init__(self, judge: Callable[[], VisionJudge]) -> None:
        self._judge = judge

    def compare(self, frame_path: Path, reference_path: Path, rubric: str) -> VisionScore:
        found = self._judge()
        self.name = getattr(found, "name", self.name)
        if not isinstance(found, ReferenceJudge):
            raise ProviderError(self.name, "this vision judge cannot compare with a reference")
        return found.compare(frame_path, reference_path, rubric)


def _references() -> ReferenceSet | None:
    """The default reference set (craft notes, local keyframes); ``None`` when unreadable."""
    try:
        return load_reference_set(DEFAULT_REFERENCE_SET)
    except ReferenceError as exc:
        log.warning("no reference set for this production: %s", exc)
        return None


def create_production(
    store: ProductionStore,
    brief: Brief | CreativeBrief,
    *,
    production_id: str | None = None,
    profile: str = "standard",
    brand_kit: Path | None = None,
    duration: float = 60.0,
    caps: BudgetCaps | None = None,
    policy: Callable[[], AutonomyPolicy | None] = lambda: None,
    force: bool = False,
    from_idea: bool = False,
    product: Path | None = None,
) -> tuple[Path, ProductionGraph]:
    """Plan ``brief`` under ``profile`` and store it as a new production.

    Writes ``assumptions.md`` for a brief generated from an idea (``from_idea``).
    With a ``product`` (``product.yaml``) the product source is recorded on the
    production and, outside the device-shot profile, a ``product`` node is added
    after the script node: it maps each proof point and beat to the scanned
    front-end routes.
    Raises :class:`ValueError` (bad profile, missing brand kit, bad plan) or
    :class:`ProductionError` (the production exists and ``force`` is not set).
    """
    quality = load_profile(profile)
    shot_params: dict[str, JsonValue] | None = None
    template_params: dict[str, JsonValue] = {}
    if quality.name == ds.PROFILE:
        if brand_kit is None:
            raise ValueError(f"profile {ds.PROFILE} needs --brand-kit PATH")
        shot_params = _device_shot_params(brand_kit)
    elif brand_kit is not None:
        template_params = _brand_params(brand_kit)
    pid = production_id or brief.id
    autonomy = policy()
    graph = ProductionGraph(pid, profile=quality, autonomy=autonomy)
    source: ProductSource | None = None
    routes: list[FrontendRoute] = []
    if product is not None:
        source = load_product_source(product)
        routes = product_routes(source)
    if shot_params is not None:
        specs = breakdown(brief, quality)
        specs = [_with_params(s, shot_params) if s.kind == ds.SHOT_STAGE else s for s in specs]
    else:
        kit = (
            BrandKit.model_validate(template_params[ds.BRAND_KIT_PARAM])
            if template_params
            else None
        )
        plan = Producer(DEPARTMENTS).breakdown(
            brief,
            quality,
            duration_s=duration,
            brand_kit=kit,
            references=_references(),
            product=source
            if source is not None and _has_script(breakdown(brief, quality))
            else None,
        )
        specs = list(plan.nodes)
        if template_params:
            specs = [
                s.model_copy(update={"params": {**s.params, **template_params}})
                if s.kind in (shot_depts.TEMPLATE_SHOTS, shot_depts.SHOTS, *FINISHING_KINDS)
                else s
                for s in specs
            ]
    if source is not None and quality.name != ds.PROFILE:
        specs = _with_product_nodes(brief, specs, source, routes)
    graph.add_nodes(specs)
    limited = caps if caps is not None and caps.limits() else None
    path = store.create(
        pid, brief, graph, force=force, caps=limited, autonomy=autonomy, product=source
    )
    if isinstance(brief, CreativeBrief) and from_idea:
        write_atomic(path / "assumptions.md", intake.assumptions_markdown(brief).encode())
    return path, graph


def _has_script(specs: Sequence[NodeSpec]) -> bool:
    return any(s.kind == discovery_depts.SCRIPT for s in specs)


def _with_product_nodes(
    brief: Brief | CreativeBrief,
    specs: Sequence[NodeSpec],
    source: ProductSource,
    routes: Sequence[FrontendRoute],
) -> list[NodeSpec]:
    """``specs`` plus the ``product`` and ``ui_capture`` nodes, shots rewired to the captures.

    The storyboard also reads the product node (its device boards are checked against
    the screen map, #339): product follows the script and the storyboard reads the
    script, so this adds no cycle.
    """
    product = _product_nodes(brief, specs, source, routes)
    if not product:
        return list(specs)
    added = product_capture_nodes(specs, source, product[0].id)
    rewired = {s.id: s for s in added[1:]}
    pid = product[0].id
    for s in specs:
        if s.kind == art_depts.STORYBOARD and pid not in rewired.get(s.id, s).inputs:
            spec = rewired.get(s.id, s)
            rewired[s.id] = spec.model_copy(update={"inputs": (*spec.inputs, pid)})
    return [rewired.get(s.id, s) for s in specs] + [*product, added[0]]


def _product_nodes(
    brief: Brief | CreativeBrief,
    specs: Sequence[NodeSpec],
    source: ProductSource,
    routes: Sequence[FrontendRoute],
) -> list[NodeSpec]:
    """The ``product`` screen-map node after the plan's script node (none without one).

    Not a breakdown seed: the strategist declares a ``product`` input, so seeding it
    before the brief would make brief -> product -> script a cycle.
    """
    script = next((s for s in specs if s.kind == discovery_depts.SCRIPT), None)
    if script is None:
        return []
    carried = {k: script.params[k] for k in (BRIEF_PARAM,) if k in script.params}
    return [
        discovery_depts.product_screen_node(
            source,
            routes,
            frontend_pages(source, routes),
            [p.claim for p in brief.proof_points],
            script.id,
            extra=carried,
        )
    ]


def _product_route_count(graph: ProductionGraph) -> int:
    try:
        routes = graph.spec(discovery_depts.PRODUCT).params.get("routes")
    except KeyError:
        return 0
    return len(routes) if isinstance(routes, list) else 0


def _ask_doubts(batch: Sequence[intake.Aspect]) -> list[int]:
    answers: list[int] = []
    for aspect in batch:
        typer.echo(aspect.question)
        for i, option in enumerate(aspect.options, 1):
            star = " (recommended)" if i - 1 == aspect.recommended else ""
            typer.echo(f"  {i}. {option}{star}")
        answers.append(int(typer.prompt("choice", default=aspect.recommended + 1)) - 1)
    return answers


def _autofill(idea: str, refs: list[str], headless: bool, kit_path: Path | None) -> CreativeBrief:
    try:
        words = load_brand_kit(kit_path).forbidden_words if kit_path else []
        return intake.build_brief(
            idea,
            intake.load_sources(refs),
            headless=headless,
            ask=None if headless else _ask_doubts,
            forbidden_words=words,
        )
    except ValueError as exc:  # IntakeError is a ValueError
        raise _fail(str(exc)) from exc


def _brand_params(kit_path: Path) -> dict[str, JsonValue]:
    """The brand kit JSON and its assets root, for the shot nodes."""
    try:
        kit: BrandKit = load_brand_kit(kit_path)
    except ValueError as exc:
        raise _fail(str(exc)) from exc
    assets = (kit_path.parent / "assets").resolve()
    return {ds.BRAND_KIT_PARAM: kit.model_dump(mode="json"), ds.ASSETS_DIR_PARAM: str(assets)}


def _device_shot_params(kit_path: Path) -> dict[str, JsonValue]:
    """The brand kit JSON, its assets root and screen asset (path, sha256) for the shot node."""
    brand = _brand_params(kit_path)
    assets = Path(str(brand[ds.ASSETS_DIR_PARAM]))
    screen = (assets / SCREEN_ASSET).resolve()
    try:
        digest = hashlib.sha256(screen.read_bytes()).hexdigest()
    except OSError as exc:
        raise _fail(f"cannot read screen asset {screen}: {exc.strerror or exc}") from exc
    return {
        ds.BRAND_KIT_PARAM: brand[ds.BRAND_KIT_PARAM],
        ds.SCREEN_IMAGE_PARAM: SCREEN_ASSET.as_posix(),
        ds.SCREEN_PATH_PARAM: str(screen),
        ds.SCREEN_SHA256_PARAM: digest,
        ds.ASSETS_DIR_PARAM: str(assets),
    }


def _with_params(spec: NodeSpec, extra: dict[str, JsonValue]) -> NodeSpec:
    params = {**spec.params, **extra}
    if BRIEF_PARAM not in params:
        raise ValueError(f"node {spec.id!r} carries no brief")
    return spec.model_copy(update={"params": params})


def _output_of_kind(graph: ProductionGraph, kind: str, prefer: tuple[str, ...] = ()) -> Any:
    """The accepted output of a node of ``kind`` (one of ``prefer`` first), or ``None``."""
    ids = [*prefer, *graph.order()]
    for nid in ids:
        try:
            if graph.spec(nid).kind != kind:
                continue
            return graph.output(nid)
        except (KeyError, LookupError, ValueError):
            continue
    return None


def _art_for(graph: ProductionGraph) -> Callable[[NodeSpec], ArtDirection | None]:
    """The art direction a node reads: its ``art_direction`` input, else the graph's."""

    def art_for(spec: NodeSpec) -> ArtDirection | None:
        raw = _output_of_kind(graph, art_depts.ART_DIRECTION, spec.inputs)
        try:
            return ArtDirection.model_validate(raw) if raw is not None else None
        except ValidationError:
            return None

    return art_for


def _brand_kit_of(graph: ProductionGraph) -> BrandKit | None:
    """The production's brand kit node output, if it has run."""
    raw = _output_of_kind(graph, art_depts.BRAND_KIT)
    try:
        return BrandKit.model_validate(raw) if raw is not None else None
    except ValidationError:
        return None


def _pass_score(graph: ProductionGraph) -> float:
    """The profile's Critic pass score (the default profile's when none is set)."""
    if graph.profile is not None:
        return graph.profile.critique.pass_score
    return load_profile("standard").critique.pass_score


def _report_lines(report: RunReport) -> list[str]:
    lines = []
    for label, ids in (
        ("executed", report.executed),
        ("reused", report.reused),
        ("awaiting approval", report.awaiting_approval),
        ("rejected", report.rejected),
        ("failed", report.failed),
        ("blocked", report.blocked),
    ):
        if ids:
            lines.append(f"{label}: {', '.join(ids)}")
    return lines or ["nothing to do"]


@studio_app.command("run")
def run_cmd(
    production_id: IdArg,
    home: HomeOpt = None,
    workers: Annotated[int, typer.Option("--workers", min=1, help="Parallel node builds.")] = 1,
    autonomy: AutonomyOpt = None,
    delegate: DelegateOpt = None,
) -> None:
    """Build every pending or dirty node whose inputs are ready, then save the production."""
    store = ProductionStore(home)
    graph = _load(store, production_id)
    if graph.profile is None or graph.profile.name != ds.PROFILE:
        problems = edge_problems(graph, DEPARTMENTS)
        if problems:
            raise _fail(
                f"production {production_id!r} was created with an older production graph "
                f"({problems[0]}); recreate it with 'spectracut studio new ... --force'"
            )
    meta = store.meta(production_id)
    policy = _policy(autonomy, delegate, meta.autonomy or graph.autonomy)
    if policy != meta.autonomy:
        meta = meta.model_copy(update={"autonomy": policy})
        store.update_meta(production_id, meta)
    graph.autonomy = policy
    try:
        runner = runner_factory(Production(production_id, store), graph)
    except (LookupError, ValueError) as exc:
        if graph.profile is None or graph.profile.name != ds.PROFILE:
            raise
        raise _fail(str(exc)) from exc  # device-shot farm config or signing key missing
    holds: list[str] = []
    if graph.profile is not None and graph.profile.name == ds.PROFILE:
        report = graph.run(runner, max_workers=workers)
    else:
        result = Producer(DEPARTMENTS).run(
            graph,
            scheduled(runner, departments=DEPARTMENTS),
            caps=meta.caps,
            approved=meta.budget_approvals,
            critic=shot_depts.shot_critic(
                _pass_score(graph),
                inner=style_frames_critic(
                    select_vision_judge,
                    profile=graph.profile or load_profile("standard"),
                    art_for=_art_for(graph),
                ),
            ),
            max_workers=workers,
            autonomy=policy,
        )
        report = result.run
        holds = [f"needs approval: {h.text()}" for h in result.needs_approval]
        if result.auto_approved:
            meta = meta.model_copy(update={"budget_log": (*meta.budget_log, *result.auto_approved)})
            store.update_meta(production_id, meta)
            holds = [f"auto-approved budget: {a.text()}" for a in result.auto_approved] + holds
    _save(store, production_id, graph)
    for line in _report_lines(report):
        typer.echo(line)
    for line in holds:
        typer.echo(line)
    for nid in report.failed:
        typer.echo(f"{nid}: {graph.state(nid).error}", err=True)
    if report.failed:
        raise typer.Exit(1)


@studio_app.command("status")
def status_cmd(production_id: IdArg, home: HomeOpt = None) -> None:
    """Show each node's status, tier, gate and who approved it on what evidence, plus the ledger."""
    store = ProductionStore(home)
    graph = _load(store, production_id)
    profile = graph.profile.name if graph.profile is not None else "none"
    typer.echo(f"production {production_id} (profile {profile})")
    for nid in graph.order():
        spec = graph.spec(nid)
        state = graph.state(nid)
        tier = graph.tier_for(spec).value
        line = f"  {nid}: {state.status.value} tier={tier} gate={graph.gate_for(spec).value}"
        decision = graph.decision(nid)
        if decision is not None and decision.human is not None:
            line += f" human={decision.human}"
        if decision is not None and decision.critic is not None:
            line += f" critic={'passed' if decision.critic.passed else 'failed'}"
        if decision is not None:
            line += _approval(decision.approver, decision.evidence, decision.escalated)
        typer.echo(line)
    budget_log = store.meta(production_id).budget_log
    if budget_log:
        typer.echo("budget approvals:")
        for approval in budget_log:
            typer.echo(f"  {approval.text()}")
    t = graph.ledger.totals()
    usage = " ".join(f"{k}={v}" for k, v in sorted(t.ai_usage.items()))
    typer.echo(
        f"ledger: runs={t.runs} gpu_minutes={t.gpu_minutes:g} "
        f"elevenlabs_characters={t.elevenlabs_characters}" + (f" {usage}" if usage else "")
    )


def _approval(approver: str | None, evidence: list[str], escalated: list[str]) -> str:
    """Status tokens for who passed a gate and on what evidence."""
    out = f" by={approver}" if approver is not None else ""
    if evidence:
        out += " evidence=" + "; ".join(evidence)
    if escalated:
        out += " escalated=" + "; ".join(escalated)
    return out


def _decide(production_id: str, node: str, comment: str, home: Path | None, approve: bool) -> None:
    store = ProductionStore(home)
    graph = _load(store, production_id)
    try:
        if approve:
            graph.approve(node, comment)
        else:
            graph.reject(node, comment)
    except KeyError as exc:
        raise _fail(str(exc.args[0]) if exc.args else str(exc)) from exc
    except ValueError as exc:
        raise _fail(str(exc)) from exc
    _save(store, production_id, graph)
    typer.echo(f"{node}: {graph.status(node).value}")


NodeArg = Annotated[str, typer.Argument(metavar="NODE", help="Node id awaiting approval.")]
CommentOpt = Annotated[str, typer.Option("--comment", help="Reviewer's note.")]


@studio_app.command("approve")
def approve(
    production_id: IdArg, node: NodeArg, comment: CommentOpt = "", home: HomeOpt = None
) -> None:
    """Pass a node's human gate; its dependants build on the next ``studio run``."""
    _decide(production_id, node, comment, home, approve=True)


@studio_app.command("reject")
def reject(
    production_id: IdArg, node: NodeArg, comment: CommentOpt = "", home: HomeOpt = None
) -> None:
    """Reject a node at its human gate; its dependants do not build."""
    _decide(production_id, node, comment, home, approve=False)


@studio_app.command("approve-budget")
def approve_budget(production_id: IdArg, node: NodeArg, home: HomeOpt = None) -> None:
    """Let a node held by a budget cap build on the next ``studio run``."""
    store = ProductionStore(home)
    graph = _load(store, production_id)
    try:
        graph.spec(node)
        meta = store.meta(production_id)
        if node not in meta.budget_approvals:
            approvals = (*meta.budget_approvals, node)
            log = (
                *meta.budget_log,
                BudgetApproval(node_id=node, approver="owner", evidence="studio approve-budget"),
            )
            store.update_meta(
                production_id,
                meta.model_copy(update={"budget_approvals": approvals, "budget_log": log}),
            )
    except KeyError as exc:
        raise _fail(str(exc.args[0]) if exc.args else str(exc)) from exc
    except ProductionError as exc:
        raise _fail(str(exc)) from exc
    typer.echo(f"{node}: budget approved")


ReplayNodeArg = Annotated[str, typer.Argument(metavar="NODE", help="Render node to replay.")]


@studio_app.command("replay")
def replay_cmd(production_id: IdArg, node: ReplayNodeArg, home: HomeOpt = None) -> None:
    """Re-render a node's recorded recipe, bypassing caches, into a fresh output dir.

    Exits 0 when the scene-graph hash and every frame's sha256 match, 1 otherwise.
    """
    store = ProductionStore(home)
    graph = _load(store, production_id)
    try:
        graph.spec(node)
        graph.output(node)
    except KeyError as exc:
        raise _fail(str(exc.args[0]) if exc.args else str(exc)) from exc
    except ValueError as exc:
        raise _fail(str(exc)) from exc
    try:
        deps = device_shot_factory(Production(production_id, store))
    except (LookupError, ValueError) as exc:
        raise _fail(str(exc)) from exc
    try:
        result = ds.replay(
            graph, node, deps.config, client=deps.client, key=deps.key, options=deps.options
        )
    except ValueError as exc:
        raise _fail(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - a failed render is a failed replay
        typer.echo(f"error: replay of {node} failed: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"replay {node}: job {result.job_id} -> {result.output_dir}")
    typer.echo(f"  scene_graph_hash recorded={result.original_scene_graph_hash}")
    typer.echo(f"  scene_graph_hash replayed={result.scene_graph_hash}")
    for name in sorted(set(result.original_frames) | set(result.frames)):
        typer.echo(
            f"  {name} recorded={result.original_frames.get(name, '-')} "
            f"replayed={result.frames.get(name, '-')}"
        )
    if not result.matches:
        typer.echo(f"mismatch: {', '.join(result.mismatches())}", err=True)
        raise typer.Exit(1)
    typer.echo("match")
