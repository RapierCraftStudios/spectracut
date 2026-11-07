# SPDX-License-Identifier: AGPL-3.0-or-later
"""Camera build tools: add, lens, transform, depth of field, moves and aspects."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from spectracut.harness.tools import ToolArgs, Vec3, build_tool

Lens = Annotated[float, Field(ge=1, le=5000)]


class CameraAdd(ToolArgs):
    name: str | None = None
    location: Vec3 | None = None
    rotation: Vec3 | None = None
    lens: Lens | None = None


class SetLens(ToolArgs):
    mm: Lens


class SetTransform(ToolArgs):
    location: Vec3 | None = None
    rotation: Vec3 | None = None


class SetDof(ToolArgs):
    enabled: bool | None = None
    focus_object: str | None = None
    focus_distance: Annotated[float, Field(ge=0)] | None = None
    fstop: Annotated[float, Field(ge=0.1, le=128)] | None = None


class CameraKey(ToolArgs):
    frame: Annotated[int, Field(ge=0)]
    location: Vec3 | None = None
    rotation: Vec3 | None = None
    lens: Lens | None = None
    focus_distance: Annotated[float, Field(ge=0)] | None = None
    """Depth-of-field focus distance (metres) at this key: keys a focus pull."""
    fstop: Annotated[float, Field(ge=0.1, le=128)] | None = None
    """Aperture f-stop at this key: keys an aperture change."""


class CameraMove(ToolArgs):
    keys: Annotated[list[CameraKey], Field(min_length=1)]
    easing: Literal["linear", "ease_in", "ease_out", "ease_in_out"] | None = None


class AddAspect(ToolArgs):
    aspect: Literal["16:9", "9:16", "1:1", "4:5"]
    name: str | None = None


class CameraUse(ToolArgs):
    name: str
    long_edge: Annotated[int, Field(ge=16, le=16384)] | None = None


TOOLS = [
    build_tool("camera.add", "Add a camera and make it active.", CameraAdd),
    build_tool("camera.set_lens", "Set the active camera's focal length (mm).", SetLens),
    build_tool("camera.set_transform", "Move or rotate the active camera.", SetTransform),
    build_tool("camera.set_dof", "Set depth of field on the active camera.", SetDof),
    build_tool("camera.move", "Keyframe a camera move with easing.", CameraMove),
    build_tool("camera.add_aspect", "Add a camera for a delivery aspect ratio.", AddAspect),
    build_tool("camera.use", "Make a named camera active.", CameraUse),
]
