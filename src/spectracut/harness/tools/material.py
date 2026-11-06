# SPDX-License-Identifier: AGPL-3.0-or-later
"""Material build tool: Principled BSDF presets with a colour and typed overrides.

A ``brand_color`` (a brand-kit ``#rrggbb`` swatch) is turned into a numeric
``color`` here, so the recorded runtime step only ever carries ``color``.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, JsonValue, model_validator

from spectracut.harness.tools import HarnessTool, Output, Session, ToolArgs, dump
from spectracut.spec._base import HexColor

Unit = Annotated[float, Field(ge=0, le=1)]
Color = Annotated[list[Unit], Field(min_length=3, max_length=4)]


def hex_rgb(hex_colour: str) -> list[JsonValue]:
    """``#rrggbb`` as RGB in [0, 1], rounded like the shot templates."""
    h = hex_colour.lstrip("#")
    return [round(int(h[i : i + 2], 16) / 255.0, 4) for i in (0, 2, 4)]


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
        "clear_glass",
        "frosted_glass",
        "smoked_glass",
        "emissive",
        "anodised_metal",
    ]
    color: Color | None = None
    brand_color: HexColor | None = Field(
        default=None, description="a brand-kit swatch '#rrggbb'; recorded as color"
    )
    name: str | None = None
    roughness: Unit | None = None
    ior: Annotated[float, Field(ge=1, le=3)] | None = None
    transmission: Unit | None = None
    emission_color: Color | None = None
    emission_strength: Annotated[float, Field(ge=0, le=1000)] | None = None
    anisotropy: Unit | None = None
    metallic: Unit | None = None

    @model_validator(mode="after")
    def _one_colour(self) -> ApplyPreset:
        if self.color is not None and self.brand_color is not None:
            raise ValueError("pass color or brand_color, not both")
        return self


def apply_preset(session: Session, args: ApplyPreset) -> Output:
    step_args = dump(args)
    brand = step_args.pop("brand_color", None)
    if isinstance(brand, str):
        step_args["color"] = hex_rgb(brand)
    return session.record_build(
        "material.apply_preset", dump(args), [("material.apply_preset", step_args)]
    )


TOOLS = [
    HarnessTool(
        "material.apply_preset",
        "Give a mesh a material preset (plastics, metals, glass incl. clear/frosted/smoked, "
        "emissive, anodised metal) with a colour or brand colour and optional roughness, ior, "
        "transmission, emission, anisotropy and metallic overrides.",
        ApplyPreset,
        "build",
        apply_preset,
        frozenset({"material.apply_preset"}),
    ),
]
