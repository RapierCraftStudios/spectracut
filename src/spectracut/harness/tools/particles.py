# SPDX-License-Identifier: AGPL-3.0-or-later
"""Particle build tool: a seeded dust/emitter system on a mesh (``particles.add``)."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, model_validator

from spectracut.harness.tools import ToolArgs, build_tool

Name = Annotated[str, Field(min_length=1, max_length=63)]
Frame = Annotated[int, Field(ge=0, le=1_048_574)]


class ParticlesAdd(ToolArgs):
    """frame_end must not be before frame_start; an emitter cannot instance itself."""

    emitter: Name = Field(description="mesh object that emits the particles")
    name: Name | None = Field(default=None, description="system name (default Dust)")
    count: Annotated[int, Field(ge=1, le=100_000)] | None = None
    seed: Annotated[int, Field(ge=0, le=2**31 - 1)] | None = Field(
        default=None, description="same seed and settings, same particles"
    )
    lifetime: Annotated[float, Field(ge=1, le=10_000)] | None = Field(
        default=None, description="frames"
    )
    size: Annotated[float, Field(gt=0, le=100)] | None = None
    velocity: Annotated[float, Field(ge=0, le=100)] | None = Field(
        default=None, description="initial speed along the emitter's normals"
    )
    frame_start: Frame | None = None
    frame_end: Frame | None = None
    instance: Name | None = Field(default=None, description="object to render per particle")

    @model_validator(mode="after")
    def _check(self) -> ParticlesAdd:
        start = 1 if self.frame_start is None else self.frame_start
        end = 1 if self.frame_end is None else self.frame_end
        if end < start:
            raise ValueError("frame_end must not be before frame_start")
        if self.instance is not None and self.instance == self.emitter:
            raise ValueError("an emitter cannot instance itself")
        return self


TOOLS = [
    build_tool(
        "particles.add", "Add a seeded particle/dust system on a mesh emitter.", ParticlesAdd
    ),
]
