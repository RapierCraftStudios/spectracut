# SPDX-License-Identifier: AGPL-3.0-or-later
"""Preview tool: render the recorded recipe on a render host and return the frame."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from spectracut.harness.tools import HarnessTool, Output, Session, Step, ToolArgs


class Preview(ToolArgs):
    width: Annotated[int, Field(ge=16, le=16384)] | None = None
    height: Annotated[int, Field(ge=16, le=16384)] | None = None
    percentage: Annotated[int, Field(ge=1, le=100)] | None = Field(
        default=None, description="render at this % of width x height (needs both)"
    )


def preview_render(session: Session, args: Preview) -> Output:
    extra: list[Step] = []
    if args.width is not None or args.height is not None or args.percentage is not None:
        if args.width is None or args.height is None:
            from spectracut.harness.errors import HarnessError

            raise HarnessError("args.invalid", "preview.render: pass width and height together")
        res: dict[str, int] = {"width": args.width, "height": args.height}
        if args.percentage is not None:
            res["percentage"] = args.percentage
        extra.append(("render.set_resolution", dict(res)))
    result = session.dispatcher.render(session.recorder.recipe(), extra)
    data = {
        "job_id": result.job_id,
        "recipe_hash": result.recipe_hash,
        "recipe_steps": len(session.recorder.steps),
        "frame": "frame_0001.png",
    }
    return Output(data, [("preview", result.frame)])


TOOLS = [
    HarnessTool(
        "preview.render",
        "Render the scene built so far on a render host; returns the PNG frame.",
        Preview,
        "meta",
        preview_render,
        frozenset({"render.set_resolution"}),
    ),
]
