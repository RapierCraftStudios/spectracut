# SPDX-License-Identifier: AGPL-3.0-or-later
"""Scene build tools: clear, primitives, glTF import and render settings."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from spectracut.harness.tools import ToolArgs, Vec3, build_tool


class SceneClear(ToolArgs):
    pass


class AddPrimitive(ToolArgs):
    kind: Literal["cube", "plane", "uv_sphere", "cylinder"]
    name: str | None = None
    location: Vec3 | None = None
    scale: Vec3 | None = None


class ImportGltf(ToolArgs):
    path: str = Field(description="relative to the host's assets dir; no absolute paths or ..")
    name: str | None = None
    location: Vec3 | None = None
    scale: Vec3 | None = None


class SetEngine(ToolArgs):
    engine: Literal["eevee", "cycles", "workbench"]


class SetResolution(ToolArgs):
    width: Annotated[int, Field(ge=16, le=16384)]
    height: Annotated[int, Field(ge=16, le=16384)]
    percentage: Annotated[int, Field(ge=1, le=100)] | None = None


RenderProperty = Literal[
    "render.film_transparent",
    "render.fps",
    "render.use_motion_blur",
    "render.motion_blur_shutter",
    "cycles.samples",
    "eevee.taa_render_samples",
    "view_settings.view_transform",
    "view_settings.look",
    "view_settings.exposure",
    "view_settings.gamma",
]


class SetProperty(ToolArgs):
    property: RenderProperty
    value: bool | int | float | str


TOOLS = [
    build_tool("scene.clear", "Remove every object from the scene.", SceneClear),
    build_tool("object.add_primitive", "Add a cube, plane, sphere or cylinder.", AddPrimitive),
    build_tool("object.import_gltf", "Import a glTF/GLB asset from the assets dir.", ImportGltf),
    build_tool("render.set_engine", "Set the render engine.", SetEngine),
    build_tool("render.set_resolution", "Set the output resolution.", SetResolution),
    build_tool("render.set_property", "Set an allowlisted render setting.", SetProperty),
]
