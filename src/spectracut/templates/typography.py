# SPDX-License-Identifier: AGPL-3.0-or-later
"""Depth and cutout typography templates through the host's ``text.add``.

``depth`` stands extruded 3D type in front of a palette backdrop; ``cutout``
punches the type out of a palette-coloured plate (a boolean difference) so the
backdrop shows through. The font family comes from ``BrandKit.typography`` for
the slot's role, resolved to a pinned, commercial-safe file when one is given
(else Blender's built-in font). Copy is checked against the kit's forbidden words.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Literal

from pydantic import Field

from spectracut.finishing.color import ColorPipeline
from spectracut.finishing.typography import FontFile
from spectracut.spec._base import NonEmptyStr, Slug, StrictModel
from spectracut.spec.brand import BrandKit
from spectracut.templates.base import (
    Call,
    CameraVariant,
    Template,
    font_arg,
    require_clean_copy,
    swatch,
)

BACKDROP = "backdrop"
PLATE = "type_plate"
TEXT = "type"
_UPRIGHT = [1.570796, 0, 0]


class TypeSlots(StrictModel):
    """The line of copy, its font role and size, and the kit's pinned font files."""

    body: NonEmptyStr = Field(
        max_length=120,
        description=(
            "The one line of copy set as type on screen (a headline, claim or VO line), "
            "never a brief field, id or note."
        ),
    )
    mode: Literal["depth", "cutout"] = Field(
        default="depth",
        description="`depth`: extruded 3D type; `cutout`: type punched out of a coloured plate.",
    )
    role: Slug = Field(
        default="display", description="The brand kit typography role whose font sets the line."
    )
    size: Annotated[float, Field(gt=0, le=5)] = Field(
        default=0.3, description="Type size in scene units."
    )
    extrude: Annotated[float, Field(ge=0, le=1)] = Field(
        default=0.06, description="Depth of the extruded type in scene units."
    )
    fonts: Mapping[str, FontFile] = Field(
        default_factory=dict,
        description="Pinned brand font files by role; leave empty unless given font files.",
    )


def _scene(kit: BrandKit, slots: TypeSlots, pipeline: ColorPipeline) -> list[Call]:
    del pipeline  # no emissive surfaces to calibrate
    body = require_clean_copy(kit, slots.body)
    calls: list[Call] = [
        (
            "object.add_primitive",
            {"kind": "plane", "name": BACKDROP, "location": [0, 0.6, 0], "scale": [8, 1, 5]},
        ),
        (
            "material.apply_preset",
            {"object": BACKDROP, "preset": "matte_plastic", "color": swatch(kit, "accent")},
        ),
    ]
    text: dict[str, object] = {
        "body": body,
        "name": TEXT,
        "size": slots.size,
        "extrude": slots.extrude,
        "mode": slots.mode,
        "location": [0, 0, 0],
        "rotation": _UPRIGHT,
    }
    if slots.mode == "cutout":
        calls += [
            (
                "object.add_primitive",
                {"kind": "cube", "name": PLATE, "location": [0, 0, 0], "scale": [1.6, 0.03, 0.5]},
            ),
            (
                "material.apply_preset",
                {"object": PLATE, "preset": "matte_plastic", "color": swatch(kit, "ink")},
            ),
        ]
        text["plate"] = PLATE
        text["extrude"] = max(slots.extrude, 0.1)  # cut through the whole plate
    font = font_arg(kit, slots.role, slots.fonts)
    if font is not None:
        text["font"] = font
    calls.append(("text.add", text))  # type: ignore[arg-type]
    calls += [
        ("light.add", {"kind": "area", "name": "key", "energy": 200, "location": [0.8, -1.6, 1.4]}),
        (
            "light.add",
            {"kind": "area", "name": "fill", "energy": 80, "location": [-1.2, -1.0, 0.4]},
        ),
    ]
    return calls


TYPOGRAPHY = Template(
    id="typography",
    slots=TypeSlots,
    variants=(
        CameraVariant(
            id="straight",
            location=(0, -3.0, 0),
            rotation=(1.570796, 0, 0),
            lens=50,
            vertical_location=(0, -4.6, 0),
            vertical_rotation=(1.570796, 0, 0),
            vertical_lens=50,
        ),
        CameraVariant(
            id="raking",
            location=(1.8, -2.2, 0.3),
            rotation=(1.4835, 0, 0.6807),
            lens=50,
            vertical_location=(1.1, -2.9, 0.2),
            vertical_rotation=(1.5184, 0, 0.3491),
            vertical_lens=40,
        ),
    ),
    scene=_scene,
)
