# SPDX-License-Identifier: AGPL-3.0-or-later
"""Light build tool: one light, or a rig preset expanded into several ``light.add`` steps."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Annotated, Literal

from pydantic import Field, JsonValue, model_validator

from spectracut.harness.tools import HarnessTool, Output, Session, Step, ToolArgs, Vec3, dump

Energy = Annotated[float, Field(ge=0, le=1e7)]

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
    kind: Literal["point", "sun", "area", "spot"] | None = None
    name: str | None = None
    energy: Energy | None = None
    location: Vec3 | None = None
    rotation: Vec3 | None = None
    rig: Literal["three_point", "key_fill", "product_softbox"] | None = Field(
        default=None, description="add a preset rig of lights aimed at target instead of one light"
    )
    target: Vec3 | None = None
    distance: Annotated[float, Field(gt=0, le=1000)] | None = None
    power: Annotated[float, Field(gt=0, le=100)] | None = None

    @model_validator(mode="after")
    def _rig_or_single(self) -> LightAdd:
        single = {"kind", "energy", "location", "rotation"} & self.model_fields_set
        rig_only = {"target", "distance", "power"} & self.model_fields_set
        if self.rig is not None and single:
            raise ValueError(f"rig places its own lights; drop {sorted(single)}")
        if self.rig is None and rig_only:
            raise ValueError(f"{sorted(rig_only)} apply only with rig")
        return self


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
        "Add a point/sun/area/spot light, or a rig preset (three_point, key_fill, "
        "product_softbox) aimed at a target.",
        LightAdd,
        "build",
        light_add,
        frozenset({"light.add"}),
    ),
]
