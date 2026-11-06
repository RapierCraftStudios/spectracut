# SPDX-License-Identifier: AGPL-3.0-or-later
"""End-card template: palette background, logo plate and the CTA in 3D type.

The logo is a slot: a sha256-pinned image shown on an emissive plate
(``material.apply_screen``, exposed so its white reads as display white). With no
logo the plate is a plain accent-coloured card, so no mark is ever invented. The
CTA is checked against the kit's forbidden words and set in the kit's display
family through ``text.add``.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import Field

from spectracut.finishing.color import ColorPipeline, screen_white_strength
from spectracut.finishing.typography import FontFile
from spectracut.spec._base import NonEmptyStr, StrictModel
from spectracut.spec.brand import BrandKit
from spectracut.templates.base import (
    Call,
    CameraVariant,
    PinnedAsset,
    Template,
    check_copy,
    font_arg,
    swatch,
)

BACKGROUND = "background"
LOGO_PLATE = "logo_plate"
CTA = "cta"
_UPRIGHT = [1.570796, 0, 0]


class EndCardSlots(StrictModel):
    """The CTA line, an optional pinned logo image and the kit's pinned font files."""

    cta: NonEmptyStr = Field(max_length=80)
    logo: PinnedAsset | None = None
    fonts: Mapping[str, FontFile] = Field(default_factory=dict)


def _scene(kit: BrandKit, slots: EndCardSlots, pipeline: ColorPipeline) -> list[Call]:
    text = check_copy(kit, slots.cta)
    calls: list[Call] = [
        (
            "object.add_primitive",
            {"kind": "plane", "name": BACKGROUND, "location": [0, 0.4, 0], "scale": [6, 1, 4]},
        ),
        (
            "material.apply_preset",
            {"object": BACKGROUND, "preset": "matte_plastic", "color": swatch(kit, "ink", 0)},
        ),
        (
            "object.add_primitive",
            {"kind": "cube", "name": LOGO_PLATE, "location": [0, 0, 0.32], "scale": [0.4, 0.01, 0.2]},
        ),
    ]
    if slots.logo is not None:
        calls.append(
            (
                "material.apply_screen",
                {
                    "object": LOGO_PLATE,
                    "image": slots.logo.path,
                    "image_sha256": slots.logo.sha256,
                    "strength": screen_white_strength(pipeline),
                },
            )
        )
    else:
        calls.append(
            (
                "material.apply_preset",
                {"object": LOGO_PLATE, "preset": "glossy_plastic", "color": swatch(kit, "accent", 2)},
            )
        )
    cta: dict[str, object] = {
        "body": text,
        "name": CTA,
        "size": 0.16,
        "extrude": 0.02,
        "mode": "depth",
        "location": [0, -0.05, -0.12],
        "rotation": _UPRIGHT,
    }
    font = font_arg(kit, "display", slots.fonts)
    if font is not None:
        cta["font"] = font
    calls.append(("text.add", cta))  # type: ignore[arg-type]
    calls.append(
        ("light.add", {"kind": "area", "name": "key", "energy": 150, "location": [0, -1.5, 1.2]})
    )
    return calls


END_CARD = Template(
    id="end-card",
    slots=EndCardSlots,
    variants=(
        CameraVariant(
            id="straight",
            location=(0, -2.4, 0.1),
            rotation=(1.570796, 0, 0),
            lens=50,
            vertical_location=(0, -3.6, 0.1),
            vertical_rotation=(1.570796, 0, 0),
            vertical_lens=50,
        ),
        CameraVariant(
            id="tilted",
            location=(0.9, -2.2, 0.6),
            rotation=(1.3963, 0, 0.3840),
            lens=50,
            vertical_location=(0.6, -3.2, 0.5),
            vertical_rotation=(1.4486, 0, 0.1920),
            vertical_lens=45,
        ),
    ),
    scene=_scene,
)
