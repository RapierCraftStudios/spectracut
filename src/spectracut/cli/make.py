# SPDX-License-Identifier: AGPL-3.0-or-later
"""``spectracut make IDEA``: one command from an idea to a delivered film.

The first run does a headless intake and plans the production; a later run of the
same command resumes the stored production, keeping every finished node. The run
goes on without input under the default autonomy policy (every gate the profile
keeps for a person is delegated, except ``--keep`` kinds) and stops only with one
named reason (:class:`~spectracut.studio.autopilot.AutopilotStop`). Every run
writes ``runs/<n>/report.json`` and ``report.md`` in the production dir.

Exit codes: 0 delivered, 3 stopped with a named reason, 2 usage or config error.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from spectracut.cli import studio
from spectracut.critique.panel import text_panel
from spectracut.providers.base import ProviderAuthError, TextProvider
from spectracut.studio import autopilot
from spectracut.studio import device_shot as ds
from spectracut.studio.autonomy import AutonomyPolicy, BudgetCeiling, load_autonomy
from spectracut.studio.conformance import Conformance, provider_conformance
from spectracut.studio.departments import shots as shot_depts
from spectracut.studio.estimate import estimate
from spectracut.studio.gates import Critic
from spectracut.studio.graph import ProductionGraph
from spectracut.studio.producer import BudgetCaps, edge_problems, scheduled
from spectracut.studio.profiles import available_profiles
from spectracut.studio.store import ProductionError, ProductionStore
from spectracut.studio.supervisor import Supervisor

panel_factory: Callable[[TextProvider], Critic] = text_panel
"""Builds the reviewer panel ``make`` gates nodes with; tests may replace it."""

conformance_factory: Callable[[TextProvider], Conformance] = provider_conformance
"""Builds the contract-conformance reviewer for edit and delivery; tests may replace it."""

EXIT_STOPPED = 3


def _text_provider() -> TextProvider:
    factory = studio.text_provider_factory
    if factory is None:
        raise ProviderAuthError("claude", "no text provider configured")
    return factory()


def make_cmd(
    idea: Annotated[str, typer.Argument(metavar="IDEA", help="A one-line idea for the film.")],
    source: Annotated[
        list[str] | None,
        typer.Option("--source", help="README, file path or URL to source claims from."),
    ] = None,
    production_id: Annotated[
        str | None, typer.Option("--id", help="Production id (default: the brief's id).")
    ] = None,
    home: studio.HomeOpt = None,
    profile: Annotated[
        str, typer.Option("--profile", help=f"Quality profile: {', '.join(available_profiles())}.")
    ] = "standard",
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
    ceiling_elevenlabs: Annotated[
        int | None,
        typer.Option(
            "--ceiling-elevenlabs-characters", min=0, help="Auto-approve ElevenLabs spend up to."
        ),
    ] = None,
    ceiling_gpu: Annotated[
        float | None,
        typer.Option("--ceiling-gpu-minutes", min=0, help="Auto-approve GPU minutes up to."),
    ] = None,
    ceiling_ai: Annotated[
        int | None,
        typer.Option("--ceiling-ai-requests", min=0, help="Auto-approve AI requests up to."),
    ] = None,
    keep: Annotated[
        list[str] | None,
        typer.Option("--keep", help="Keep this gate (node kind) with the owner."),
    ] = None,
    autonomy: Annotated[
        Path | None,
        typer.Option("--autonomy", help="Autonomy policy JSON; replaces the default policy."),
    ] = None,
    workers: Annotated[int, typer.Option("--workers", min=1, help="Parallel node builds.")] = 1,
) -> None:
    """Make a film from IDEA: intake, contract, the graph, polish passes and delivery.

    Re-running the same command resumes the stored production. Stops only for a
    missing prerequisite, spend above the ceiling, an owner-kept gate or a failed
    contract; a run report is written either way.
    """
    store = ProductionStore(home)
    caps = BudgetCaps(elevenlabs_characters=cap_elevenlabs, gpu_minutes=cap_gpu, ai_requests=cap_ai)
    ceiling = BudgetCeiling(
        elevenlabs_characters=ceiling_elevenlabs, gpu_minutes=ceiling_gpu, ai_requests=ceiling_ai
    )
    pid = production_id
    if pid is None or not store.exists(pid):
        brief = studio._autofill(idea, source or [], True, brand_kit)
        pid = pid or brief.id
        if not store.exists(pid):
            try:
                path, _ = studio.create_production(
                    store,
                    brief,
                    production_id=pid,
                    profile=profile,
                    brand_kit=brand_kit,
                    duration=duration,
                    caps=caps,
                    from_idea=True,
                )
            except (ProductionError, ValueError, OSError) as exc:
                raise studio._fail(str(exc)) from exc
            typer.echo(f"production {pid} created at {path}")
        else:
            typer.echo(f"resuming {pid}")
    else:
        typer.echo(f"resuming {pid}")
    graph = studio._load(store, pid)
    policy = _make_policy(graph, autonomy, keep or [], ceiling)

    pre = autopilot.preflight(_text_provider, model_host=studio.model_host_name(store.home))
    for line in pre.lines():
        typer.echo(f"preflight: {line}")
    problem = _plan_problem(graph)
    if problem is not None:
        _stop(store, pid, autopilot.AutopilotStop.MISSING_PREREQUISITE, problem, pre.lines())
    if pre.text is None:
        missing = next(p for p in pre.missing() if p.name.startswith("text provider"))
        detail = f"{missing.name}: {missing.detail}; mock in use: {missing.mock}"
        _stop(store, pid, autopilot.AutopilotStop.MISSING_PREREQUISITE, detail, pre.lines())
    assert pre.text is not None
    try:
        runner = studio.runner_factory(studio.Production(pid, store), graph)
    except (LookupError, ValueError) as exc:
        detail = f"{exc}; mock in use: {autopilot.mock_for(exc)}"
        _stop(store, pid, autopilot.AutopilotStop.MISSING_PREREQUISITE, detail, pre.lines())
    critic = shot_depts.shot_critic(studio._pass_score(graph), inner=panel_factory(pre.text))
    result = autopilot.run_to_delivery(
        store,
        pid,
        scheduled(runner, departments=studio.DEPARTMENTS),
        critic=critic,
        conformance=conformance_factory(pre.text),
        policy=policy,
        caps=caps if caps.limits() else None,
        workers=workers,
        supervisor=Supervisor(),
        departments=studio.DEPARTMENTS,
        preflight_lines=pre.lines(),
    )
    typer.echo(f"executed: {', '.join(result.executed) or 'nothing'}")
    typer.echo(f"stop: {result.stop.value}: {result.detail}")
    typer.echo(f"report: {result.report_path}")
    if result.stop is not autopilot.AutopilotStop.DELIVERED:
        raise typer.Exit(EXIT_STOPPED)


def _make_policy(
    graph: ProductionGraph, path: Path | None, keep: list[str], ceiling: BudgetCeiling
) -> AutonomyPolicy:
    """``--autonomy FILE`` when given, else the make default: delegate every human gate."""
    if path is not None:
        try:
            return load_autonomy(path)
        except ValueError as exc:
            raise studio._fail(str(exc)) from exc
    return autopilot.default_policy(graph.profile, keep, ceiling)


def _plan_problem(graph: ProductionGraph) -> str | None:
    """Why the stored plan cannot run with the registered departments, or ``None``."""
    if graph.profile is not None and graph.profile.name == ds.PROFILE:
        return None
    problems = edge_problems(graph, studio.DEPARTMENTS)
    if problems:
        return f"plan does not match the departments: {problems[0]}"
    unknown = estimate(graph, studio.DEPARTMENTS).unknown_departments
    if unknown:
        return "no department registered for: " + ", ".join(unknown)
    return None


def _stop(
    store: ProductionStore,
    pid: str,
    stop: autopilot.AutopilotStop,
    detail: str,
    preflight_lines: list[str],
) -> NoReturn:
    """Write the report of a run that stopped before the graph ran, and exit 3."""
    report = autopilot.stopped_report(pid, stop, detail, preflight_lines)
    path = report.write(store.path(pid))
    typer.echo(f"stop: {stop.value}: {detail}")
    typer.echo(f"report: {path}")
    raise typer.Exit(EXIT_STOPPED)
