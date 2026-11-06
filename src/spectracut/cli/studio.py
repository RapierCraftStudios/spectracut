# SPDX-License-Identifier: AGPL-3.0-or-later
"""``spectracut studio``: create, run, inspect and approve productions."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from pydantic import JsonValue, ValidationError

from spectracut.providers.base import TextProvider, select_text_provider
from spectracut.queue.executors.flamenco import FlamencoApi
from spectracut.spec.brand import BrandKit, load_brand_kit
from spectracut.spec.brief import Brief, CreativeBrief
from spectracut.studio import device_shot as ds
from spectracut.studio.department import Department
from spectracut.studio.departments import all_departments
from spectracut.studio.estimate import BRIEF_PARAM, breakdown, estimate
from spectracut.studio.graph import ProductionGraph, RunContext, Runner, RunReport, RunResult
from spectracut.studio.node import NodeSpec
from spectracut.studio.producer import BudgetCaps, Producer, scheduled
from spectracut.studio.profiles import available_profiles, load_profile
from spectracut.studio.runner import DepartmentRunner
from spectracut.studio.store import ProductionError, ProductionStore

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from spectracut.providers.base import VisionJudge

studio_app = typer.Typer(
    no_args_is_help=True,
    help="Productions: create one from a brief, run it, review its status, pass human gates.",
)

DEPARTMENTS: dict[str, type[Department]] = all_departments()
"""Departments available to ``studio run``, by name (the registry in ``studio.departments``)."""

text_provider_factory: Callable[[], TextProvider] | None = select_text_provider
"""Builds the text provider departments run through: Claude sign-in, else Codex sign-in."""


class Production:
    """A stored production as ``studio run`` sees it."""

    def __init__(self, production_id: str, store: ProductionStore) -> None:
        self.id = production_id
        self.store = store

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

SCREEN_ASSET = Path("ui") / "dashboard.png"
"""The UI screen image, relative to the brand kit's ``assets`` dir."""


def default_runner(production: Production, graph: ProductionGraph) -> Runner:
    """Run nodes through :data:`DEPARTMENTS`; a node with no department fails clearly.

    Productions on the ``device-shot`` profile run through the device-shot runner.
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

    def run(ctx: RunContext) -> RunResult:
        nonlocal inner
        name = ctx.spec.department
        if name not in DEPARTMENTS:
            raise LookupError(f"no department registered for {name!r}")
        if inner is None:
            if text_provider_factory is None:
                raise LookupError("no text provider configured for departments")
            inner = DepartmentRunner(
                DEPARTMENTS.values(), text_provider_factory(), brief=production.brief, graph=graph
            )
        return inner(ctx)

    return run


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


def _save(store: ProductionStore, production_id: str, graph: ProductionGraph) -> None:
    try:
        store.save(production_id, graph)
    except (ProductionError, OSError) as exc:
        raise _fail(f"cannot save production {production_id!r}: {exc}") from exc


@studio_app.command("new")
def new(
    brief_file: Annotated[Path, typer.Argument(metavar="BRIEF", help="Brief JSON file.")],
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
) -> None:
    """Create a production from a brief file and print its estimate.

    Over-cap nodes are held by ``studio run`` until ``studio approve-budget``.
    """
    brief = _load_brief(brief_file)
    try:
        quality = load_profile(profile)
    except ValueError as exc:
        raise _fail(str(exc)) from exc
    shot_params: dict[str, JsonValue] | None = None
    if quality.name == ds.PROFILE:
        if brand_kit is None:
            raise _fail(f"profile {ds.PROFILE} needs --brand-kit PATH")
        shot_params = _device_shot_params(brand_kit)
    pid = production_id or brief.id
    store = ProductionStore(home)
    caps = BudgetCaps(elevenlabs_characters=cap_elevenlabs, gpu_minutes=cap_gpu, ai_requests=cap_ai)
    try:
        graph = ProductionGraph(pid, profile=quality)
        if shot_params is not None:
            specs = breakdown(brief, quality)
            specs = [_with_params(s, shot_params) if s.kind == ds.SHOT_STAGE else s for s in specs]
        else:
            specs = list(Producer(DEPARTMENTS).breakdown(brief, quality, duration_s=duration).nodes)
        graph.add_nodes(specs)
        path = store.create(pid, brief, graph, force=force, caps=caps if caps.limits() else None)
    except (ProductionError, ValueError) as exc:
        raise _fail(str(exc)) from exc
    typer.echo(f"production {pid} created at {path}")
    est = estimate(graph, DEPARTMENTS)
    for line in est.lines():
        typer.echo(line)
    if shot_params is None and est.unknown_departments:
        typer.echo("no department registered yet for: " + ", ".join(est.unknown_departments))


def _device_shot_params(kit_path: Path) -> dict[str, JsonValue]:
    """The brand kit JSON, its assets root and screen asset (path, sha256) for the shot node."""
    try:
        kit: BrandKit = load_brand_kit(kit_path)
    except ValueError as exc:
        raise _fail(str(exc)) from exc
    assets = (kit_path.parent / "assets").resolve()
    screen = (assets / SCREEN_ASSET).resolve()
    try:
        digest = hashlib.sha256(screen.read_bytes()).hexdigest()
    except OSError as exc:
        raise _fail(f"cannot read screen asset {screen}: {exc.strerror or exc}") from exc
    return {
        ds.BRAND_KIT_PARAM: kit.model_dump(mode="json"),
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
) -> None:
    """Build every pending or dirty node whose inputs are ready, then save the production."""
    store = ProductionStore(home)
    graph = _load(store, production_id)
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
        meta = store.meta(production_id)
        result = Producer(DEPARTMENTS).run(
            graph,
            scheduled(runner, departments=DEPARTMENTS),
            caps=meta.caps,
            approved=meta.budget_approvals,
            max_workers=workers,
        )
        report = result.run
        holds = [f"needs approval: {h.text()}" for h in result.needs_approval]
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
    """Show each node's status, tier, gate and decision, plus the cost ledger."""
    graph = _load(ProductionStore(home), production_id)
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
        typer.echo(line)
    t = graph.ledger.totals()
    usage = " ".join(f"{k}={v}" for k, v in sorted(t.ai_usage.items()))
    typer.echo(
        f"ledger: runs={t.runs} gpu_minutes={t.gpu_minutes:g} "
        f"elevenlabs_characters={t.elevenlabs_characters}" + (f" {usage}" if usage else "")
    )


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
            store.update_meta(
                production_id, meta.model_copy(update={"budget_approvals": approvals})
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
