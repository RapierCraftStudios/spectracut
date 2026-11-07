# SPDX-License-Identifier: AGPL-3.0-or-later
"""Light and world build tools.

``light.add`` adds one light, or a rig preset expanded into several ``light.add``
steps. ``world.set_hdri``, ``world.set_gradient`` and ``world.set_haze`` set the
world background and volumetric haze (density capped Look-safe).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Annotated, Literal

from pydantic import Field, JsonValue, model_validator

from spectracut.harness.tools import (
    HarnessTool,
    Output,
    Session,
    Step,
    ToolArgs,
    Vec3,
    build_tool,
    dump,
)

Energy = Annotated[float, Field(ge=0, le=1e7)]
Unit = Annotated[float, Field(ge=0, le=1)]
Color = Annotated[list[Unit], Field(min_length=3, max_length=4)]
RGB = Annotated[list[Unit], Field(min_length=3, max_length=3)]
# Same Look-safe ceiling as host/bpy_runtime/ops/world.py HAZE_DENSITY_MAX.
HAZE_DENSITY_MAX = 0.05

# Rig presets: (suffix, kind, direction from the target towards the light, energy in W).
RIGS: dict[str, list[tuple[str, str, tuple[float, float, float], float]]] = {
    "three_point": [
        ("Key", "area", (1.0, -1.0, 0.8), 600.0),
        ("Fill", "area", (-1.2, -0.8, 0.4), 250.0),
        ("Rim", "area", (0.0, 1.0, 1.0), 400.0),
    ],
    "key_fill": [
        ("Key", "area", (1.0, -1.0, 0.8), 600.0),
        ("Fill", "area", (-1.2, -0.8, 0.4), 250.0),
    ],
    "product_softbox": [
        ("Left", "area", (-1.0, -0.6, 0.5), 500.0),
        ("Right", "area", (1.0, -0.6, 0.5), 500.0),
        ("Top", "area", (0.0, 0.0, 1.0), 350.0),
    ],
    "rim_area": [
        ("Key", "area", (0.8, -1.0, 0.7), 500.0),
        ("RimLeft", "area", (-1.0, 1.0, 0.6), 450.0),
        ("RimRight", "area", (1.0, 1.0, 0.6), 450.0),
    ],
}


def aim(location: Sequence[float], target: Sequence[float]) -> list[float]:
    """XYZ Euler rotation pointing a light (-Z forward) from ``location`` at ``target``."""
    fx, fy, fz = (float(t) - float(p) for p, t in zip(location, target, strict=True))
    n = math.sqrt(fx * fx + fy * fy + fz * fz) or 1.0
    fx, fy, fz = fx / n, fy / n, fz / n
    pitch = math.acos(max(-1.0, min(1.0, -fz)))
    yaw = math.atan2(-fx, fy) if abs(fx) + abs(fy) > 1e-12 else 0.0
    return [round(pitch, 6), 0.0, round(yaw, 6)]


def rig_steps(
    rig: str, name: str, target: Sequence[float], distance: float, power: float
) -> list[Step]:
    """The ``light.add`` steps for a rig preset around ``target``."""
    steps: list[Step] = []
    for suffix, kind, direction, energy in RIGS[rig]:
        n = math.sqrt(sum(d * d for d in direction))
        location = [round(t + distance * d / n, 6) for t, d in zip(target, direction, strict=True)]
        args: dict[str, JsonValue] = {
            "kind": kind,
            "name": f"{name}{suffix}",
            "energy": round(energy * power, 6),
            "location": list(location),
            "rotation": list(aim(location, target)),
        }
        steps.append(("light.add", args))
    return steps


class LightAdd(ToolArgs):
    """Either one light or a preset rig, never both.

    One light: kind, energy, location, rotation, size (area only), color,
    spot_angle (spot only); no target, distance or power. Rig: rig with optional
    target, distance and power, and none of the single-light arguments.
    """

    kind: Literal["point", "sun", "area", "spot"] | None = None
    name: str | None = None
    energy: Energy | None = None
    location: Vec3 | None = None
    rotation: Vec3 | None = None
    size: Annotated[float, Field(gt=0, le=100)] | None = Field(
        default=None, description="area light size in m (area only)"
    )
    color: RGB | None = None
    spot_angle: Annotated[float, Field(ge=1, le=180)] | None = Field(
        default=None, description="spot cone angle in degrees (spot only)"
    )
    rig: Literal["three_point", "key_fill", "product_softbox", "rim_area"] | None = Field(
        default=None, description="add a preset rig of lights aimed at target instead of one light"
    )
    target: Vec3 | None = Field(default=None, description="point the rig aims at (rig only)")
    distance: Annotated[float, Field(gt=0, le=1000)] | None = Field(
        default=None, description="rig light distance from target in m (rig only)"
    )
    power: Annotated[float, Field(gt=0, le=100)] | None = Field(
        default=None, description="rig energy multiplier (rig only)"
    )

    @model_validator(mode="after")
    def _rig_or_single(self) -> LightAdd:
        single = {
            "kind",
            "energy",
            "location",
            "rotation",
            "size",
            "color",
            "spot_angle",
        } & self.model_fields_set
        rig_only = {"target", "distance", "power"} & self.model_fields_set
        if self.rig is not None and single:
            raise ValueError(f"rig places its own lights; drop {sorted(single)}")
        if self.rig is None and rig_only:
            raise ValueError(f"{sorted(rig_only)} apply only with rig")
        kind = self.kind or "point"
        if self.size is not None and kind != "area":
            raise ValueError("size applies only to area lights")
        if self.spot_angle is not None and kind != "spot":
            raise ValueError("spot_angle applies only to spot lights")
        return self


class SetHdri(ToolArgs):
    path: str
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    strength: Annotated[float, Field(ge=0, le=1000)] | None = None
    rotation: Annotated[float, Field(ge=-360, le=360)] | None = None


class SetGradient(ToolArgs):
    bottom: Color
    top: Color
    strength: Annotated[float, Field(ge=0, le=1000)] | None = None


class SetHaze(ToolArgs):
    density: Annotated[float, Field(gt=0, le=HAZE_DENSITY_MAX)]
    color: Color | None = None
    anisotropy: Annotated[float, Field(ge=-1, le=1)] | None = None


def light_add(session: Session, args: LightAdd) -> Output:
    if args.rig is None:
        step_args = dump(args)
        return session.record_build("light.add", step_args, [("light.add", step_args)])
    steps = rig_steps(
        args.rig,
        args.name or "Rig",
        args.target or [0.0, 0.0, 0.0],
        args.distance or 4.0,
        args.power or 1.0,
    )
    return session.record_build("light.add", dump(args), steps)


TOOLS = [
    HarnessTool(
        "light.add",
        "Add a point/sun/area/spot light (optional area size, colour, spot angle), or a rig "
        "preset (three_point, key_fill, product_softbox, rim_area) aimed at a target.",
        LightAdd,
        "build",
        light_add,
        frozenset({"light.add"}),
    ),
    build_tool(
        "world.set_hdri",
        "Light the world from a sha256-pinned HDRI in the host assets dir.",
        SetHdri,
    ),
    build_tool(
        "world.set_gradient",
        "Set a vertical colour gradient world background (bottom to top) with a strength.",
        SetGradient,
    ),
    build_tool(
        "world.set_haze",
        "Add volumetric haze to the world (density capped Look-safe); call after the background.",
        SetHaze,
    ),
]
