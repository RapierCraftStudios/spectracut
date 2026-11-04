# SPDX-License-Identifier: AGPL-3.0-or-later
"""Material build tool: Principled BSDF presets with a colour."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from spectracut.harness.tools import ToolArgs, build_tool

Unit = Annotated[float, Field(ge=0, le=1)]


class ApplyPreset(ToolArgs):
    object: str
    preset: Literal[
        "matte_plastic",
        "glossy_plastic",
        "brushed_metal",
        "polished_metal",
        "glass",
        "rubber",
        "ceramic",
    ]
    color: Annotated[list[Unit], Field(min_length=3, max_length=4)] | None = None
    name: str | None = None


TOOLS = [
    build_tool("material.apply_preset", "Give a mesh a material preset and colour.", ApplyPreset),
]
