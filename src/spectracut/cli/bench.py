# SPDX-License-Identifier: AGPL-3.0-or-later
"""``spectracut bench``: score benchmark sets, record human ratings, catch regressions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from spectracut.critique import benchmarks as bm
from spectracut.providers.base import ProviderError

bench_app = typer.Typer(
    help="Benchmark films: score sets per rubric, rate outputs, check for regressions."
)

RootOpt = Annotated[
    Path | None,
    typer.Option(
        "--benchmarks-dir",
        help=f"Benchmarks directory (default ${bm.BENCHMARKS_DIR_ENV} or ./benchmarks).",
    ),
]
OutOpt = Annotated[
    Path | None, typer.Option("--out", help="Results directory (default sets/<set>/results).")
]


def _fail(message: str) -> typer.Exit:
    typer.echo(f"error: {message}", err=True)
    return typer.Exit(2)


def _run(set_id: str, root: Path | None, out: Path | None) -> bm.BenchmarkReport:
    try:
        return bm.run_set(set_id, root=root, out_dir=out)
    except (bm.BenchmarkError, ProviderError) as exc:
        raise _fail(str(exc)) from exc


def _print_report(report: bm.BenchmarkReport) -> None:
    for p in report.pieces:
        human = f" human={p.human_score:.3f}" if p.human_score is not None else ""
        mark = "ok" if p.meets_target else "below target"
        typer.echo(f"{p.piece}: critic={p.score:.3f}{human} target={p.target:.3f} ({mark})")
    a = report.agreement
    if a.agreement_rate is not None:
        typer.echo(f"agreement: {a.agreement_rate:.3f} over {a.pairs} pairs")


@bench_app.command("run")
def run(
    set_id: Annotated[str, typer.Argument(metavar="SET")],
    benchmarks_dir: RootOpt = None,
    out: OutOpt = None,
    write_baseline: Annotated[
        bool, typer.Option("--write-baseline", help="Accept these scores as the new baseline.")
    ] = False,
) -> None:
    """Score each piece's latest output per its rubric and write report.json and report.md."""
    report = _run(set_id, benchmarks_dir, out)
    _print_report(report)
    target = out if out is not None else bm.results_dir(set_id, benchmarks_dir)
    typer.echo(f"report: {target / 'report.json'}")
    if write_baseline:
        typer.echo(f"baseline: {bm.write_baseline(report, benchmarks_dir)}")


@bench_app.command("rate")
def rate(
    set_id: Annotated[str, typer.Argument(metavar="SET")],
    piece: Annotated[str, typer.Argument(help="Piece id.")],
    score: Annotated[float, typer.Argument(help="Rating in [0, 1].")],
    rater: Annotated[str, typer.Option("--rater", help="Rater id (slug).")] = "owner",
    output: Annotated[
        Path | None, typer.Option("--output", help="Output file (default: the latest).")
    ] = None,
    notes: Annotated[str, typer.Option("--notes")] = "",
    benchmarks_dir: RootOpt = None,
    out: OutOpt = None,
) -> None:
    """Record a human rating for an output, stored next to the Critic's score."""
    try:
        rating = bm.record_rating(
            set_id,
            piece,
            score,
            rater=rater,
            output=output,
            notes=notes,
            root=benchmarks_dir,
            out_dir=out,
        )
    except bm.BenchmarkError as exc:
        raise _fail(str(exc)) from exc
    target = out if out is not None else bm.results_dir(set_id, benchmarks_dir)
    critic = None
    report_path = target / "report.json"
    if report_path.is_file():
        report = bm.BenchmarkReport.model_validate_json(report_path.read_bytes())
        critic = next((p.score for p in report.pieces if p.output_hash == rating.output_hash), None)
    shown = f"{critic:.3f}" if critic is not None else "not scored yet"
    typer.echo(
        f"rated {rating.piece} {rating.output}: human={rating.score:.3f} critic={shown} "
        f"-> {target / 'ratings.jsonl'}"
    )


@bench_app.command("check")
def check(
    set_id: Annotated[str, typer.Argument(metavar="SET")],
    benchmarks_dir: RootOpt = None,
    out: OutOpt = None,
) -> None:
    """Score the set and fail if any piece drops below its baseline beyond its tolerance."""
    try:
        baseline = bm.load_baseline(set_id, benchmarks_dir)
        bset = bm.load_set(set_id, benchmarks_dir)
    except bm.BenchmarkError as exc:
        raise _fail(str(exc)) from exc
    report = _run(set_id, benchmarks_dir, out)
    try:
        results = bm.check_regression(report, baseline, bset)
    except bm.BenchmarkError as exc:
        raise _fail(str(exc)) from exc
    failed = [r for r in results if r.regressed]
    for r in results:
        score = f"{r.score:.3f}" if r.score is not None else "missing"
        status = "REGRESSED" if r.regressed else "ok"
        typer.echo(
            f"{r.piece}: {score} vs baseline {r.baseline:.3f} "
            f"(tolerance {r.tolerance:.3f}) {status}"
        )
    if failed:
        typer.echo(f"{len(failed)} benchmark(s) regressed", err=True)
        raise typer.Exit(1)


@bench_app.command("agreement")
def agreement_cmd(
    set_id: Annotated[str, typer.Argument(metavar="SET")],
    benchmarks_dir: RootOpt = None,
    out: OutOpt = None,
) -> None:
    """Report Critic-vs-human agreement (BlenderGym pairwise protocol)."""
    try:
        report = bm.set_agreement(set_id, root=benchmarks_dir, out_dir=out)
    except bm.BenchmarkError as exc:
        raise _fail(str(exc)) from exc
    typer.echo(json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True))


@bench_app.command("harness")
def harness(
    benchmarks_dir: RootOpt = None,
    solutions: Annotated[
        Path | None,
        typer.Option("--solutions", help="Directory of <task>.json scenes to score."),
    ] = None,
    width: Annotated[int | None, typer.Option("--width", min=4, max=256)] = None,
    height: Annotated[int | None, typer.Option("--height", min=4, max=256)] = None,
) -> None:
    """Run the ported harness tasks headless at tiny resolution against hidden ground truth.

    Without --solutions this is a self-check: each start scene must fail and each
    ground truth must pass.
    """
    resolution = (width, height) if width and height else None
    try:
        tasks = bm.load_harness_tasks(benchmarks_dir)
    except bm.BenchmarkError as exc:
        raise _fail(str(exc)) from exc
    if not tasks:
        raise _fail("no harness tasks found")
    bad = 0
    for task in tasks:
        if solutions is not None:
            try:
                result = bm.score_harness(
                    task, bm.load_solution(solutions / f"{task.id}.json"), resolution
                )
            except bm.BenchmarkError as exc:
                raise _fail(str(exc)) from exc
            ok = result.passed
            detail = f"score={result.score:.3f}"
        else:
            start, truth = bm.harness_self_check(task, resolution)
            ok = truth.passed and not start.passed
            result = truth
            detail = f"start={start.score:.3f} truth={truth.score:.3f}"
        w, h = result.resolution
        status = "ok" if ok else "FAIL"
        typer.echo(
            f"{task.id} [{task.source}] {w}x{h} {detail} threshold={task.threshold:.3f} {status}"
        )
        bad += 0 if ok else 1
    if bad:
        raise typer.Exit(1)
