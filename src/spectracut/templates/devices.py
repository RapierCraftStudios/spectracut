# SPDX-License-Identifier: AGPL-3.0-or-later
"""Device templates: a MacBook Pro or iPhone kit model with the product UI on its screen.

The device is a sha256-pinned glTF model from the device kit
(:mod:`spectracut.templates.device_kit`: ``macbook-pro`` for the laptop,
``iphone-15-pro`` for the phone), imported by the host's ``device.add`` with
``model``: animation cleared, scaled to real size, its lowest point on the floor and
the capture planar-mapped onto the display faces, cover-cropped from the top. The
screen is a slot: a sha256-pinned image sequence or movie from the host assets dir,
shown at a fixed emission of :data:`~spectracut.templates.device_kit.SCREEN_EMISSION`
with a fresnel-mixed glossy coat. The MacBook body takes a ``finish`` (space-black or
silver).

The set is a sweep (floor plus back wall) in one of two ``set`` variants: ``dark``
(albedo 0.004, specular 0.25, roughness 0.28) or ``light`` (warm grey, albedo 0.72),
lit by the ``apple-studio`` rig from ``light.add``: a 0.9x0.6 m top key, two thin
0.06x1.2 m rim strips behind left and right, a weak front fill and a background
sweep light, all hidden from the camera. Rig positions and the cameras (70 mm, about
1.2 m, front and three-quarter) derive from the kit's measured screen frame. A pinned
HDRI world (``world.set_hdri``) replaces the set's gradient world when given.
"""

from __future__ import annotations

import math
from typing import Annotated, Literal

from pydantic import Field, JsonValue, model_validator

from spectracut.finishing.color import ColorPipeline
from spectracut.spec._base import StrictModel
from spectracut.spec.brand import BrandKit
from spectracut.templates import device_kit
from spectracut.templates.base import Call, CameraVariant, PinnedAsset, Template, swatch
from spectracut.templates.device_kit import SCREEN_EMISSION, DeviceAsset, Vec3

DEVICE = "device"
FLOOR = "floor"
BACKDROP = "backdrop"
RIG = "apple-studio"
DeviceKind = Literal["laptop", "phone"]
SetVariant = Literal["dark", "light"]
Finish = Literal["space-black", "silver"]
DEFAULT_FINISH: Finish = "space-black"
# Set materials: (colour, material.apply_preset overrides). Dark is near-black with
# specular 0.25 (F0 0.02, IOR 1.33) at roughness 0.28; light is warm grey at albedo 0.72.
SET_PRESET = "matte_plastic"
SETS: dict[str, tuple[list[float], dict[str, float]]] = {
    "dark": ([0.004, 0.004, 0.004], {"roughness": 0.28, "ior": 1.33}),
    "light": ([0.75, 0.72, 0.68], {"roughness": 0.5, "ior": 1.45}),
}
# apple-studio energies (W): key, each rim, fill, sweep; and the gradient world
# (bottom, top grey in scene-linear, strength) when no HDRI is pinned.
RIG_ENERGY: dict[str, dict[str, float]] = {
    "dark": {"key": 90.0, "rim": 45.0, "fill": 6.0, "sweep": 20.0},
    "light": {"key": 110.0, "rim": 30.0, "fill": 18.0, "sweep": 60.0},
}
SET_WORLD: dict[str, tuple[float, float, float]] = {
    "dark": (0.0005, 0.002, 0.3),
    "light": (0.08, 0.2, 0.5),
}
# The sweep: a floor plane at z = 0 and a back wall this far behind the screen.
BACKDROP_DEPTH = 1.6
# Three-quarter yaw and the elevation (degrees) of each framing.
THREE_QUARTER = 35.0
FRAMINGS: dict[str, tuple[float, float]] = {
    "front": (0.0, 8.0),
    "three-quarter": (THREE_QUARTER, 12.0),
}
# Front framing with a different target and distance: id -> (target, scale).
EXTRA_FRAMINGS: dict[str, tuple[Literal["screen", "bounds"], float]] = {
    "wide": ("bounds", 1.0),
    "close": ("screen", 0.62),
}


def set_for(kit: BrandKit) -> SetVariant:
    """``dark`` for a kit whose surface swatch is dark (luminance < 0.05), else ``light``."""
    r, g, b = (float(c) for c in swatch(kit, "surface")[:3])  # type: ignore[arg-type]
    return "dark" if 0.2126 * r + 0.7152 * g + 0.0722 * b < 0.05 else "light"


class DeviceSlots(StrictModel):
    """The screen content (frames or a movie), the kit ``model`` (default: the kit's
    model for the template's kind), the body ``finish``, the ``set`` variant (default:
    from the kit's surface swatch) and an optional pinned HDRI world."""

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
    model: PinnedAsset | None = Field(
        default=None,
        description="The pinned device glb; omit for the device kit's model for this kind.",
    )
    finish: Finish = Field(
        default=DEFAULT_FINISH, description="Body finish of the MacBook: space-black or silver."
    )
    set: SetVariant | None = Field(
        default=None,
        description="Set variant: dark or light; omit to follow the kit's surface swatch.",
    )
    hdri: PinnedAsset | None = Field(
        default=None, description="A pinned HDRI world; omit for the set's gradient world."
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


def kit_entry(kind: DeviceKind, model: PinnedAsset | None = None) -> DeviceAsset:
    """The kit entry a ``kind`` device shows: the one pinned by ``model``'s sha256,
    else the kit's model for ``kind`` (whose screen selectors a re-pinned copy keeps)."""
    if model is not None:
        found = device_kit.by_sha256(model.sha256)
        if found is not None:
            return found
    return device_kit.kit_for(kind)


def _model_arg(entry: DeviceAsset, model: PinnedAsset | None) -> dict[str, object]:
    pinned = model if model is not None else entry.pinned()
    arg: dict[str, object] = {
        **pinned.arg(),
        "screen": entry.screen.arg(),
        "real_width": entry.real_width,
    }
    if entry.body_material is not None:
        arg["body_material"] = entry.body_material
    return arg


def _offset(c: Vec3, dx: float, dy: float, dz: float) -> list[JsonValue]:
    return [round(c[0] + dx, 4), round(c[1] + dy, 4), round(c[2] + dz, 4)]


def _aim(location: list[JsonValue], target: Vec3) -> list[JsonValue]:
    x, y, z = (float(v) for v in location)  # type: ignore[arg-type]
    loc: Vec3 = (x, y, z)
    return [round(a, 6) for a in device_kit.look_rotation(loc, target)]


def studio_rig(entry: DeviceAsset, set_variant: SetVariant) -> list[Call]:
    """The ``apple-studio`` rig around ``entry``'s screen frame, hidden from the camera:
    top key 0.9x0.6 m, rim strips 0.06x1.2 m behind left and right, a weak front fill
    and a sweep light on the back wall."""
    energy = RIG_ENERGY[set_variant]
    c = entry.frame.centre
    lo, hi = entry.bounds
    half = max(hi[0] - lo[0], hi[2] - lo[2]) / 2
    key = _offset(c, 0.0, -0.15, 0.9)
    rims = {
        "rim_left": _offset(c, -(half + 0.3), 0.45, 0.2),
        "rim_right": _offset(c, half + 0.3, 0.45, 0.2),
    }
    fill = _offset(c, 0.0, -1.6, 0.25)
    wall: Vec3 = (c[0], c[1] + BACKDROP_DEPTH, 0.35)
    sweep: list[JsonValue] = [round(c[0], 4), round(c[1] + BACKDROP_DEPTH * 0.45, 4), 1.3]
    hidden: dict[str, JsonValue] = {"kind": "area", "camera_visible": False}
    calls: list[Call] = [
        (
            "light.add",
            {
                **hidden,
                "name": "key",
                "energy": energy["key"],
                "location": key,
                "rotation": [round(math.radians(10), 6), 0.0, 0.0],
                "size": 0.9,
                "size_y": 0.6,
            },
        )
    ]
    for name, at in rims.items():
        calls.append(
            (
                "light.add",
                {
                    **hidden,
                    "name": name,
                    "energy": energy["rim"],
                    "location": at,
                    "rotation": _aim(at, c),
                    "size": 0.06,
                    "size_y": 1.2,
                },
            )
        )
    calls += [
        (
            "light.add",
            {
                **hidden,
                "name": "fill",
                "energy": energy["fill"],
                "location": fill,
                "rotation": _aim(fill, c),
                "size": 1.0,
            },
        ),
        (
            "light.add",
            {
                **hidden,
                "name": "sweep",
                "energy": energy["sweep"],
                "location": sweep,
                "rotation": _aim(sweep, wall),
                "size": 2.0,
            },
        ),
    ]
    return calls


def _set_calls(entry: DeviceAsset, set_variant: SetVariant) -> list[Call]:
    colour, overrides = SETS[set_variant]
    y = round(entry.frame.centre[1] + BACKDROP_DEPTH, 4)
    calls: list[Call] = [
        (
            "object.add_primitive",
            {"kind": "plane", "name": FLOOR, "location": [0, 0, 0], "scale": [6, 6, 1]},
        ),
        (
            "object.add_slab",
            {"name": BACKDROP, "dimensions": [12, 0.02, 6], "location": [0, y, 3]},
        ),
    ]
    for obj in (FLOOR, BACKDROP):
        calls.append(
            (
                "material.apply_preset",
                {"object": obj, "preset": SET_PRESET, "color": list(colour), **overrides},
            )
        )
    return calls


def _scene(kind: DeviceKind) -> object:
    def scene(kit: BrandKit, slots: DeviceSlots, pipeline: ColorPipeline) -> list[Call]:
        entry = kit_entry(kind, slots.model)
        set_variant: SetVariant = slots.set or set_for(kit)
        bottom, top, strength = SET_WORLD[set_variant]
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
        device: dict[str, object] = {
            "kind": kind,
            "name": DEVICE,
            "location": [0, 0, 0],
            "rotation": [0, 0, 0],
            "model": _model_arg(entry, slots.model),
            "screen": slots.screen_arg(),
            "emission_strength": SCREEN_EMISSION,
        }
        if entry.finishes:
            device["finish"] = slots.finish
        return [
            *_set_calls(entry, set_variant),
            ("device.add", device),  # type: ignore[list-item]
            world,
            *studio_rig(entry, set_variant),
        ]

    return scene


def device_variants(entry: DeviceAsset) -> tuple[CameraVariant, ...]:
    """Front, three-quarter, wide (whole device, aimed at the bounds centre) and close
    (front, 0.62x the fitted distance on the screen centre) cameras, each with its own
    9:16 camera re-framed to keep the whole device in frame."""
    framings: dict[str, tuple[float, float, Literal["screen", "bounds"], float]] = {
        vid: (yaw, el, "screen", 1.0) for vid, (yaw, el) in FRAMINGS.items()
    }
    front = FRAMINGS["front"]
    for vid, (target, scale) in EXTRA_FRAMINGS.items():
        framings[vid] = (front[0], front[1], target, scale)
    out = []
    for vid, (yaw, elevation, target, scale) in framings.items():
        wide = device_kit.kit_camera(
            entry, yaw=yaw, elevation=elevation, aspect=16 / 9, target=target, scale=scale
        )
        tall = device_kit.kit_camera(
            entry, yaw=yaw, elevation=elevation, aspect=9 / 16, target=target, scale=scale
        )
        out.append(
            CameraVariant(
                id=vid,
                location=wide.location,
                rotation=wide.rotation,
                lens=wide.lens,
                vertical_location=tall.location,
                vertical_rotation=tall.rotation,
                vertical_lens=tall.lens,
            )
        )
    return tuple(out)


LAPTOP = Template(
    id="device-laptop",
    slots=DeviceSlots,
    variants=device_variants(device_kit.kit_for("laptop")),
    scene=_scene("laptop"),  # type: ignore[arg-type]
)

PHONE = Template(
    id="device-phone",
    slots=DeviceSlots,
    variants=device_variants(device_kit.kit_for("phone")),
    scene=_scene("phone"),  # type: ignore[arg-type]
)
