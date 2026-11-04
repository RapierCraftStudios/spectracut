# SPDX-License-Identifier: AGPL-3.0-or-later
"""``spectracut studio``: create, run, inspect and approve productions."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from spectracut.providers.base import TextProvider
from spectracut.spec.brief import Brief, CreativeBrief
from spectracut.studio.department import Department
from spectracut.studio.estimate import breakdown, estimate
from spectracut.studio.graph import ProductionGraph, RunContext, Runner, RunReport, RunResult
from spectracut.studio.profiles import available_profiles, load_profile
from spectracut.studio.runner import DepartmentRunner
from spectracut.studio.store import ProductionError, ProductionStore

studio_app = typer.Typer(
    no_args_is_help=True,
    help="Productions: create one from a brief, run it, review its status, pass human gates.",
)

DEPARTMENTS: dict[str, type[Department]] = {}
"""Departments available to ``studio run``, by name. Concrete departments register here."""

text_provider_factory: Callable[[], TextProvider] | None = None
"""Builds the text provider departments run through; ``None`` until one is configured."""


class Production:
    """A stored production as ``studio run`` sees it."""

    def __init__(self, production_id: str, store: ProductionStore) -> None:
        self.id = production_id
        self.store = store

    @property
    def brief(self) -> Brief | CreativeBrief:
        return self.store.brief(self.id)


def default_runner(production: Production, graph: ProductionGraph) -> Runner:
    """Run nodes through :data:`DEPARTMENTS`; a node with no department fails clearly."""
    inner: DepartmentRunner | None = None
    if DEPARTMENTS and text_provider_factory is not None:
        inner = DepartmentRunner(
            DEPARTMENTS.values(), text_provider_factory(), brief=production.brief, graph=graph
        )

    def run(ctx: RunContext) -> RunResult:
        name = ctx.spec.department
        if name not in DEPARTMENTS:
            raise LookupError(f"no department registered for {name!r}")
        if inner is None:
            raise LookupError("no text provider configured for departments")
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
) -> None:
    """Create a production from a brief file and print its estimate."""
    brief = _load_brief(brief_file)
    try:
        quality = load_profile(profile)
    except ValueError as exc:
        raise _fail(str(exc)) from exc
    pid = production_id or brief.id
    store = ProductionStore(home)
    try:
        graph = ProductionGraph(pid, profile=quality)
        graph.add_nodes(breakdown(brief, quality))
        path = store.create(pid, brief, graph, force=force)
    except (ProductionError, ValueError) as exc:
        raise _fail(str(exc)) from exc
    typer.echo(f"production {pid} created at {path}")
    for line in estimate(graph, DEPARTMENTS).lines():
        typer.echo(line)


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
    runner = runner_factory(Production(production_id, store), graph)
    report = graph.run(runner, max_workers=workers)
    _save(store, production_id, graph)
    for line in _report_lines(report):
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
