# SPDX-License-Identifier: AGPL-3.0-or-later
"""Animation build tools: keyframes with easing, the frame range and preset moves.

``object.keyframe`` keys transforms, a camera's lens and allowlisted material
inputs; ``render.set_frame_range`` sets the scene range; ``object.animate``
keys a preset move (turntable, reveal, shatter, assemble; the last two are
seeded). Each records one runtime step of the same name.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import ConfigDict, Field, model_validator

from spectracut.harness.tools import (
    HarnessTool,
    Output,
    Session,
    ToolArgs,
    Vec3,
    build_tool,
)

Name = Annotated[str, Field(min_length=1, max_length=63)]
Frame = Annotated[int, Field(ge=0, le=1_048_574)]
Easing = Literal["linear", "ease_in", "ease_out", "ease_in_out"]
Seed = Annotated[int, Field(ge=0, le=2**31 - 1)]


class KeyframeInputs(ToolArgs):
    """Allowlisted Principled BSDF inputs of the object's material."""

    emission_strength: Annotated[float, Field(ge=0, le=1000)] | None = None
    alpha: Annotated[float, Field(ge=0, le=1)] | None = None
    roughness: Annotated[float, Field(ge=0, le=1)] | None = None
    transmission: Annotated[float, Field(ge=0, le=1)] | None = None

    @model_validator(mode="after")
    def _not_empty(self) -> KeyframeInputs:
        if all(v is None for v in self.model_dump().values()):
            raise ValueError("inputs needs at least one input")
        return self


class KeyframeKey(ToolArgs):
    frame: Frame
    location: Vec3 | None = None
    rotation: Vec3 | None = None
    scale: Vec3 | None = None
    lens: Annotated[float, Field(ge=1, le=5000)] | None = Field(
        default=None, description="camera objects only"
    )
    inputs: KeyframeInputs | None = None

    @model_validator(mode="after")
    def _has_channel(self) -> KeyframeKey:
        channels = (self.location, self.rotation, self.scale, self.lens, self.inputs)
        if all(c is None for c in channels):
            raise ValueError("a key needs location, rotation, scale, lens or inputs")
        return self


class ObjectKeyframe(ToolArgs):
    object: Name
    keys: Annotated[list[KeyframeKey], Field(min_length=2, max_length=1000)]
    material: Name | None = Field(
        default=None, description="material slot to key inputs on (default: active material)"
    )
    easing: Easing | None = None

    @model_validator(mode="after")
    def _increasing(self) -> ObjectKeyframe:
        frames = [k.frame for k in self.keys]
        if any(b <= a for a, b in zip(frames, frames[1:], strict=False)):
            raise ValueError("keys must have strictly increasing frames")
        if self.material is not None and all(k.inputs is None for k in self.keys):
            raise ValueError("material applies only to keys with inputs")
        return self


class SetFrameRange(ToolArgs):
    frame_start: Frame
    frame_end: Frame

    @model_validator(mode="after")
    def _ordered(self) -> SetFrameRange:
        if self.frame_end <= self.frame_start:
            raise ValueError("frame_end must be after frame_start")
        return self


# Preset -> optional fields it accepts (mirrors host/bpy_runtime/ops/animate.py).
PRESET_FIELDS: dict[str, frozenset[str]] = {
    "turntable": frozenset({"degrees", "axis"}),
    "reveal": frozenset({"from_", "offset"}),
    "shatter": frozenset({"pieces", "seed", "distance"}),
    "assemble": frozenset({"pieces", "seed", "distance"}),
}


class ObjectAnimate(ToolArgs):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    object: Name
    preset: Literal["turntable", "reveal", "shatter", "assemble"]
    frame_start: Frame
    frame_end: Frame
    degrees: Annotated[float, Field(ge=-3600, le=3600)] | None = None
    axis: Literal["x", "y", "z"] | None = None
    from_: Literal["scale", "position"] | None = Field(default=None, alias="from")
    offset: Vec3 | None = Field(default=None, description="position reveal only")
    pieces: Annotated[int, Field(ge=2, le=256)] | None = None
    seed: Seed | None = None
    distance: Annotated[float, Field(ge=0, le=100)] | None = None

    @model_validator(mode="after")
    def _fits_preset(self) -> ObjectAnimate:
        if self.frame_end <= self.frame_start:
            raise ValueError("frame_end must be after frame_start")
        allowed = PRESET_FIELDS[self.preset]
        for field in ("degrees", "axis", "from_", "offset", "pieces", "seed", "distance"):
            if getattr(self, field) is not None and field not in allowed:
                name = "from" if field == "from_" else field
                raise ValueError(f"{name} does not apply to the {self.preset} preset")
        if self.preset == "reveal":
            if self.from_ == "position" and self.offset is None:
                raise ValueError("a position reveal needs offset")
            if self.from_ != "position" and self.offset is not None:
                raise ValueError("offset applies to a position reveal only")
        return self


def _animate(session: Session, args: ObjectAnimate) -> Output:
    # by_alias: the runtime reads the ``from`` key, a Python keyword here.
    step_args = args.model_dump(mode="json", exclude_none=True, by_alias=True)
    return session.record_build("object.animate", step_args, [("object.animate", step_args)])


TOOLS = [
    build_tool(
        "object.keyframe",
        "Keyframe an object's transforms, a camera's lens or material inputs, with easing.",
        ObjectKeyframe,
    ),
    build_tool("render.set_frame_range", "Set the scene's frame range.", SetFrameRange),
    HarnessTool(
        "object.animate",
        "Animate an object with a preset: turntable, reveal, shatter or assemble (seeded).",
        ObjectAnimate,
        "build",
        _animate,
        frozenset({"object.animate"}),
    ),
]
