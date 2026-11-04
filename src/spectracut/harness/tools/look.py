# SPDX-License-Identifier: AGPL-3.0-or-later
"""Look tool: solid, material and rendered views of the scene from an auto-framed angle."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field, JsonValue

from spectracut.harness.tools import HarnessTool, Output, Session, ToolArgs

Mode = Literal["solid", "material", "rendered"]


def _all_modes() -> list[Mode]:
    return ["solid", "material", "rendered"]


class Look(ToolArgs):
    angle: Literal["front", "three_quarter", "top"] = "three_quarter"
    modes: Annotated[list[Mode], Field(min_length=1, max_length=3)] = Field(
        default_factory=_all_modes
    )


def look(session: Session, args: Look) -> Output:
    """One render per mode; each appends ``look.set_view`` to a copy of the recipe."""
    recipe = session.recorder.recipe()
    views: list[dict[str, Any]] = []
    images: list[tuple[str, bytes]] = []
    for mode in dict.fromkeys(args.modes):
        step: dict[str, JsonValue] = {"shading": mode, "angle": args.angle}
        result = session.dispatcher.render(recipe, [("look.set_view", step)])
        views.append({"mode": mode, "job_id": result.job_id, **result.report("look.set_view")})
        images.append((mode, result.frame))
    return Output({"angle": args.angle, "views": views}, images)


TOOLS = [
    HarnessTool(
        "look",
        "Solid, material and rendered views from an auto-framed front/three_quarter/top angle.",
        Look,
        "meta",
        look,
        frozenset({"look.set_view"}),
    ),
]
