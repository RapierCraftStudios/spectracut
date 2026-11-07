# SPDX-License-Identifier: AGPL-3.0-or-later
"""Device templates: a logo-free laptop or phone with the product UI on its screen.

The rig is the host's ``device.add`` (bevelled aluminium body, bezel, reflective
glass cover, moire-free pixel-grid screen, untextured keyboard deck). The screen is a slot: a sha256-pinned image sequence or movie from the
host assets dir. Its emission strength is calibrated with
:func:`spectracut.finishing.color.screen_white_strength` so UI white reads as
display white under the shot's view transform. Lighting is a studio preset
(``lighting``: key, fill and rim area lights from ``light.add``) over either a
pinned HDRI world (``world.set_hdri``) or, when no HDRI is pinned, a neutral
studio gradient (``world.set_gradient``). The floor is a glossy plastic in the
kit's surface colour so the device reflects in it. No step adds text or a logo.
"""

from __future__ import annotations

import math
from typing import Annotated, Literal

from pydantic import Field, model_validator

from spectracut.finishing.color import ColorPipeline, screen_white_strength
from spectracut.spec._base import StrictModel
from spectracut.spec.brand import BrandKit
from spectracut.templates.base import Call, CameraVariant, PinnedAsset, Template, swatch

DEVICE = "device"
FLOOR = "floor"
DeviceKind = Literal["laptop", "phone"]
# Half the device height (metres), so the floor sits just under the rig.
_HALF_HEIGHT: dict[str, float] = {"laptop": 0.11, "phone": 0.08}
Lighting = Literal["studio-soft", "studio-dark", "studio-bright"]
# Area-light energies (W) for key, fill and rim, and the gradient world (bottom,
# top grey in scene-linear, strength) used when no HDRI is pinned.
LIGHTING: dict[str, dict[str, tuple[float, ...]]] = {
    "studio-soft": {"energy": (60, 25, 40), "world": (0.02, 0.12, 0.6)},
    "studio-dark": {"energy": (45, 8, 70), "world": (0.004, 0.03, 0.4)},
    "studio-bright": {"energy": (90, 45, 35), "world": (0.08, 0.4, 0.9)},
}
FLOOR_PRESET = "glossy_plastic"
FINISH = "aluminium"
# Corner bevel (metres) passed to the host rig per kind.
_BEVEL: dict[str, float] = {"laptop": 0.003, "phone": 0.004}


def _deg(*angles: float) -> tuple[float, float, float]:
    x, y, z = (round(math.radians(a), 6) for a in angles)
    return (x, y, z)


class DeviceSlots(StrictModel):
    """The screen content (frames or a movie), the studio ``lighting`` preset and an
    optional pinned HDRI world (``hdri``; without it a studio gradient lights the set)."""

    screen_frames: list[PinnedAsset] | None = Field(
        default=None,
        min_length=1,
        description="Pinned product UI frames shown on the screen; set this or screen_movie.",
    )
    screen_movie: PinnedAsset | None = Field(
        default=None,
        description="A pinned product UI movie shown on the screen; set this or screen_frames.",
    )
    frame_offset: Annotated[int, Field(ge=0)] = Field(
        default=0, description="Frames to skip at the start of the screen content."
    )
    lighting: Lighting = Field(
        default="studio-soft", description="Studio lighting preset: key, fill and rim lights."
    )
    hdri: PinnedAsset | None = Field(
        default=None, description="A pinned HDRI world; omit for the studio gradient world."
    )
    hdri_strength: Annotated[float, Field(ge=0, le=1000)] = Field(
        default=1.0, description="Brightness of the HDRI world."
    )
    hdri_rotation: Annotated[float, Field(ge=-360, le=360)] = Field(
        default=0.0, description="Rotation of the HDRI world in degrees."
    )

    @model_validator(mode="after")
    def _one_screen(self) -> DeviceSlots:
        if (self.screen_frames is None) == (self.screen_movie is None):
            raise ValueError("a device screen takes exactly one of screen_frames or screen_movie")
        if self.screen_movie is not None and self.frame_offset:
            raise ValueError("frame_offset applies to screen_frames only")
        return self

    def screen_arg(self) -> dict[str, object]:
        if self.screen_movie is not None:
            return {"movie": self.screen_movie.arg()}
        frames = [f.arg() for f in self.screen_frames or []]
        return {"frames": frames, "frame_offset": self.frame_offset}


def _scene(kind: DeviceKind) -> object:
    def scene(kit: BrandKit, slots: DeviceSlots, pipeline: ColorPipeline) -> list[Call]:
        floor_z = -_HALF_HEIGHT[kind]
        preset = LIGHTING[slots.lighting]
        key, fill, rim = preset["energy"]
        bottom, top, strength = preset["world"]
        world: Call = (
            (
                "world.set_hdri",
                {
                    **slots.hdri.arg(),
                    "strength": slots.hdri_strength,
                    "rotation": slots.hdri_rotation,
                },
            )
            if slots.hdri is not None
            else (
                "world.set_gradient",
                {"bottom": [bottom] * 3, "top": [top] * 3, "strength": strength},
            )
        )
        return [
            (
                "object.add_primitive",
                {"kind": "plane", "name": FLOOR, "location": [0, 0, floor_z], "scale": [6, 6, 1]},
            ),
            (
                "material.apply_preset",
                {"object": FLOOR, "preset": FLOOR_PRESET, "color": swatch(kit, "surface")},
            ),
            (
                "device.add",
                {
                    "kind": kind,
                    "name": DEVICE,
                    "location": [0, 0, 0],
                    "rotation": [0, 0, 0],
                    "screen": slots.screen_arg(),  # type: ignore[dict-item]
                    "emission_strength": screen_white_strength(pipeline),
                    "bevel": _BEVEL[kind],
                    "finish": FINISH,
                },
            ),
            world,
            (
                "light.add",
                {
                    "kind": "area",
                    "name": "key",
                    "energy": key,
                    "location": [0.8, -0.9, 0.8],
                    "rotation": list(_deg(55, 0, 40)),
                },
            ),
            (
                "light.add",
                {
                    "kind": "area",
                    "name": "fill",
                    "energy": fill,
                    "location": [-0.9, -0.7, 0.3],
                    "rotation": list(_deg(70, 0, -50)),
                },
            ),
            (
                "light.add",
                {
                    "kind": "area",
                    "name": "rim",
                    "energy": rim,
                    "location": [-0.7, 0.6, 0.5],
                    "rotation": list(_deg(-60, 0, -130)),
                },
            ),
        ]

    return scene


LAPTOP = Template(
    id="device-laptop",
    slots=DeviceSlots,
    variants=(
        CameraVariant(
            id="front",
            location=(0, -0.95, 0.04),
            rotation=_deg(88, 0, 0),
            lens=50,
            vertical_location=(0, -1.55, 0.04),
            vertical_rotation=_deg(88, 0, 0),
            vertical_lens=50,
        ),
        CameraVariant(
            id="three-quarter",
            location=(0.62, -0.74, 0.22),
            rotation=_deg(80, 0, 40),
            lens=50,
            vertical_location=(0.38, -0.5, 0.12),
            vertical_rotation=_deg(84, 0, 38),
            vertical_lens=35,
        ),
    ),
    scene=_scene("laptop"),  # type: ignore[arg-type]
)

PHONE = Template(
    id="device-phone",
    slots=DeviceSlots,
    variants=(
        CameraVariant(
            id="front",
            location=(0, -0.62, 0.0),
            rotation=_deg(90, 0, 0),
            lens=50,
            vertical_location=(0, -0.42, 0.0),
            vertical_rotation=_deg(90, 0, 0),
            vertical_lens=50,
        ),
        CameraVariant(
            id="three-quarter",
            location=(0.36, -0.45, 0.1),
            rotation=_deg(80, 0, 38),
            lens=50,
            vertical_location=(0.22, -0.3, 0.06),
            vertical_rotation=_deg(82, 0, 36),
            vertical_lens=50,
        ),
    ),
    scene=_scene("phone"),  # type: ignore[arg-type]
)
