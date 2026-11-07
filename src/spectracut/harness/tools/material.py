# SPDX-License-Identifier: AGPL-3.0-or-later
"""Material build tool: Principled BSDF presets with a colour and typed overrides.

A ``brand_color`` (a brand-kit ``#rrggbb`` swatch) is linearised with the sRGB
EOTF into a numeric scene-linear ``color`` here, so the recorded runtime step only
ever carries ``color`` and the swatch renders back to its own hex under
:data:`~spectracut.finishing.color.BRAND_PIPELINE`.

Surface detail (#335): ``bump_*`` (noise or voronoi micro-texture), ``brushing`` and
``brushing_angle`` (anisotropic streaks), ``scratches`` and ``edge_wear`` (a Bevel edge
highlight) build a procedural node graph on the host; ``seed`` picks the pattern
deterministically.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StrictInt, model_validator

from spectracut.finishing.color import hex_to_linear
from spectracut.harness.tools import HarnessTool, Output, Session, ToolArgs, dump
from spectracut.spec._base import HexColor

Unit = Annotated[float, Field(ge=0, le=1)]
Color = Annotated[list[Unit], Field(min_length=3, max_length=4)]


BRAND_EMISSION_MAX = 1.0
"""Highest emission_strength for an emissive preset with a brand_color.

Under the brand ``Standard`` view a linearised swatch at strength 1 displays as its
own hex; above 1 the brightest channel clips first, so a blue accent drifts to cyan
and then white. Brightness comes from contrast with the void, not energy.
"""


class ApplyPreset(ToolArgs):
    """Pass color or brand_color, not both.

    brand_color is linearised (sRGB EOTF) and recorded as a scene-linear color;
    numeric colors are scene-linear already, never hex/255. An emissive preset with
    brand_color defaults emission_strength to 1.0 and refuses more than 1.0.
    """

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
        default=None,
        description="a brand-kit swatch '#rrggbb'; linearised and recorded as color",
    )
    name: str | None = None
    roughness: Unit | None = None
    ior: Annotated[float, Field(ge=1, le=3)] | None = None
    transmission: Unit | None = None
    emission_color: Color | None = None
    emission_strength: Annotated[float, Field(ge=0, le=1000)] | None = Field(
        default=None,
        description="emissive strength; with brand_color it defaults to 1 and is at most 1",
    )
    anisotropy: Unit | None = None
    metallic: Unit | None = None
    bump_kind: Literal["noise", "voronoi"] | None = Field(
        default=None, description="micro-texture pattern for bump_strength (default noise)"
    )
    bump_strength: Unit | None = Field(default=None, description="micro-texture bump amount")
    bump_scale: Annotated[float, Field(gt=0, le=1000)] | None = Field(
        default=None, description="micro-texture frequency per object unit (default 150)"
    )
    brushing: Unit | None = Field(default=None, description="brushed streaks + anisotropy")
    brushing_angle: Annotated[float, Field(ge=0, lt=360)] | None = Field(
        default=None, description="streak direction in degrees"
    )
    scratches: Unit | None = Field(default=None, description="fine scratch lines")
    edge_wear: Unit | None = Field(default=None, description="bevel edge highlight (Cycles)")
    seed: Annotated[StrictInt, Field(ge=0, le=2**31 - 1)] | None = Field(
        default=None, description="pattern seed; same seed, same surface"
    )

    @model_validator(mode="after")
    def _one_colour(self) -> ApplyPreset:
        if self.color is not None and self.brand_color is not None:
            raise ValueError("pass color or brand_color, not both")
        if (
            self.preset == "emissive"
            and self.brand_color is not None
            and self.emission_strength is not None
            and self.emission_strength > BRAND_EMISSION_MAX
        ):
            raise ValueError(
                f"emission_strength {self.emission_strength:g} with brand_color clips the hue "
                f"to cyan or white under the brand Standard view; use at most "
                f"{BRAND_EMISSION_MAX:g} (brightness comes from contrast with the void)"
            )
        return self


def apply_preset(session: Session, args: ApplyPreset) -> Output:
    step_args = dump(args)
    brand = step_args.pop("brand_color", None)
    if isinstance(brand, str):
        step_args["color"] = list(hex_to_linear(brand))
        if args.preset == "emissive" and "emission_strength" not in step_args:
            step_args["emission_strength"] = BRAND_EMISSION_MAX
    return session.record_build(
        "material.apply_preset", dump(args), [("material.apply_preset", step_args)]
    )


TOOLS = [
    HarnessTool(
        "material.apply_preset",
        "Give a mesh a material preset (plastics, metals, glass incl. clear/frosted/smoked, "
        "emissive, anodised metal) with a colour or brand colour and optional roughness, ior, "
        "transmission, emission, anisotropy and metallic overrides, plus seeded surface detail "
        "(noise/voronoi bump, brushing, scratches, edge wear).",
        ApplyPreset,
        "build",
        apply_preset,
        frozenset({"material.apply_preset"}),
    ),
]
