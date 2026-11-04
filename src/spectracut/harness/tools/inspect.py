# SPDX-License-Identifier: AGPL-3.0-or-later
"""Inspect tools: scene bounds and subject framing, measured on a render host."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from spectracut.harness.errors import HarnessError
from spectracut.harness.tools import HarnessTool, Output, Session, ToolArgs, dump


class Bounds(ToolArgs):
    objects: Annotated[list[str], Field(min_length=1)]


class Framing(ToolArgs):
    object: str = Field(description="the subject object's name")
    min_coverage_pct: Annotated[float, Field(ge=0, le=100)] | None = Field(
        default=None, description="fail with framing.subject_too_small below this coverage"
    )


def inspect_bounds(session: Session, args: Bounds) -> Output:
    result = session.dispatcher.render(session.recorder.recipe(), [("inspect.bounds", dump(args))])
    return Output(result.report("inspect.bounds"))


def inspect_framing(session: Session, args: Framing) -> Output:
    """Subject coverage as a percentage (0-100) of the active camera's frame."""
    result = session.dispatcher.render(session.recorder.recipe(), [("inspect.framing", dump(args))])
    report = dict(result.report("inspect.framing"))
    coverage = float(report["coverage_pct"])
    report["unit"] = "percent_of_frame"
    if args.min_coverage_pct is not None:
        report["min_coverage_pct"] = args.min_coverage_pct
        if coverage < args.min_coverage_pct:
            raise HarnessError(
                "framing.subject_too_small",
                f"{args.object!r} covers {coverage:.2f}% of the frame, "
                f"below the required {args.min_coverage_pct:g}%",
                coverage_pct=coverage,
                min_coverage_pct=args.min_coverage_pct,
            )
    return Output(report)


TOOLS = [
    HarnessTool(
        "inspect.bounds",
        "World bounding box of named objects.",
        Bounds,
        "query",
        inspect_bounds,
        frozenset({"inspect.bounds"}),
    ),
    HarnessTool(
        "inspect.framing",
        "Subject coverage as a percentage of the active camera's frame.",
        Framing,
        "query",
        inspect_framing,
        frozenset({"inspect.framing"}),
    ),
]
