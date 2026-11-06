# SPDX-License-Identifier: AGPL-3.0-or-later
"""End-card template: palette background, logo plate and the CTA in 3D type.

The logo is the kit's ``logo`` asset: its sha256-pinned file (the ``logo`` slot)
is shown on an emissive plate (``material.apply_screen``, exposed so its white
reads as display white). A kit with no logo gets a plain accent-coloured card, so
no mark is ever invented or borrowed. The
CTA is checked against the kit's forbidden words and set in the kit's display
family through ``text.add``.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePosixPath

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
    TemplateError,
    font_arg,
    require_clean_copy,
    swatch,
)

BACKGROUND = "background"
LOGO_PLATE = "logo_plate"
CTA = "cta"
_UPRIGHT = [1.570796, 0, 0]


class EndCardSlots(StrictModel):
    """The CTA line, the pinned file of the kit's logo (if it has one) and pinned fonts."""

    cta: NonEmptyStr = Field(max_length=80)
    logo: PinnedAsset | None = None
    fonts: Mapping[str, FontFile] = Field(default_factory=dict)


def kit_logo(kit: BrandKit, logo: PinnedAsset | None) -> PinnedAsset | None:
    """The pinned file of the kit's ``logo`` asset id, refusing a mark the kit does not own.

    A kit with a ``logo`` needs its pinned file (``logo`` slot, file stem = the asset
    id); a kit without one gets the plain accent plate, never a supplied mark.
    """
    if kit.logo is None:
        if logo is not None:
            raise TemplateError(f"brand kit {kit.id!r} has no logo; refusing {logo.path!r}")
        return None
    if logo is None:
        raise TemplateError(f"brand kit {kit.id!r} logo {kit.logo!r} needs its pinned file")
    if PurePosixPath(logo.path).stem != kit.logo:
        raise TemplateError(f"logo file {logo.path!r} is not the kit's logo {kit.logo!r}")
    return logo


def _scene(kit: BrandKit, slots: EndCardSlots, pipeline: ColorPipeline) -> list[Call]:
    text = require_clean_copy(kit, slots.cta)
    logo = kit_logo(kit, slots.logo)
    calls: list[Call] = [
        (
            "object.add_primitive",
            {"kind": "plane", "name": BACKGROUND, "location": [0, 0.4, 0], "scale": [6, 1, 4]},
        ),
        (
            "material.apply_preset",
            {"object": BACKGROUND, "preset": "matte_plastic", "color": swatch(kit, "ink")},
        ),
        (
            "object.add_primitive",
            {
                "kind": "cube",
                "name": LOGO_PLATE,
                "location": [0, 0, 0.32],
                "scale": [0.4, 0.01, 0.2],
            },
        ),
    ]
    if logo is not None:
        calls.append(
            (
                "material.apply_screen",
                {
                    "object": LOGO_PLATE,
                    "image": logo.path,
                    "image_sha256": logo.sha256,
                    "strength": screen_white_strength(pipeline),
                },
            )
        )
    else:
        calls.append(
            (
                "material.apply_preset",
                {"object": LOGO_PLATE, "preset": "glossy_plastic", "color": swatch(kit, "accent")},
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
