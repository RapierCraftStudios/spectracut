# SPDX-License-Identifier: AGPL-3.0-or-later
"""Scene build tools: clear, primitives, glTF import, render settings, geometry and text.

Geometry tools (``curve.add``, ``object.add_slab``, ``object.array``,
``object.boolean``, ``object.parent``) and ``text.add`` each record one runtime
step of the same name (``host/bpy_runtime/ops/geometry.py``, ``ops/text.py``).
None is random, so none takes a seed.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

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


Name = Annotated[str, Field(min_length=1, max_length=63)]


class CurveAdd(ToolArgs):
    points: Annotated[list[Vec3], Field(min_length=2, max_length=256)] = Field(
        description="control points the curve passes through"
    )
    name: Name | None = None
    kind: Literal["bezier", "poly"] | None = None
    bevel_depth: Annotated[float, Field(ge=0, le=10)] | None = Field(
        default=None, description="tube radius in m; 0 is a bare line"
    )
    bevel_resolution: Annotated[int, Field(ge=0, le=32)] | None = None
    resolution: Annotated[int, Field(ge=1, le=64)] | None = None
    cyclic: bool | None = None
    location: Vec3 | None = None


Dimension = Annotated[float, Field(gt=0, le=1000)]


class AddSlab(ToolArgs):
    """bevel_width must be at most half the smallest dimension."""

    dimensions: Annotated[list[Dimension], Field(min_length=3, max_length=3)] = Field(
        description="box size in m along x, y, z"
    )
    name: Name | None = None
    bevel_width: Annotated[float, Field(ge=0)] | None = Field(
        default=None, description="bevel width in m, at most half the smallest dimension"
    )
    segments: Annotated[int, Field(ge=1, le=16)] | None = None
    location: Vec3 | None = None
    rotation: Vec3 | None = None

    @model_validator(mode="after")
    def _bevel_fits(self) -> AddSlab:
        if self.bevel_width is not None and self.bevel_width > min(self.dimensions) / 2:
            raise ValueError("bevel_width must be at most half the smallest dimension")
        return self


class ObjectArray(ToolArgs):
    object: Name
    count: Annotated[int, Field(ge=2, le=1000)] = Field(
        description="total copies, original included"
    )
    offset: Vec3 = Field(description="constant offset between copies, in m")
    mode: Literal["modifier", "linked"] | None = Field(
        default=None,
        description="an Array modifier (default) or linked duplicates sharing the mesh",
    )


class ObjectBoolean(ToolArgs):
    """object and cutter must be different objects."""

    object: Name
    cutter: Name = Field(description="mesh to cut with; hidden from renders")
    operation: Literal["difference", "union", "intersect"] | None = None

    @model_validator(mode="after")
    def _distinct(self) -> ObjectBoolean:
        if self.object == self.cutter:
            raise ValueError("object and cutter must be different objects")
        return self


class ObjectParent(ToolArgs):
    """child and parent must be different objects."""

    child: Name
    parent: Name

    @model_validator(mode="after")
    def _distinct(self) -> ObjectParent:
        if self.child == self.parent:
            raise ValueError("an object cannot be its own parent")
        return self


class TextFont(ToolArgs):
    path: str = Field(description="font file in the host assets dir (brand-kit OFL font)")
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class TextAdd(ToolArgs):
    """plate is required for mode 'cutout' and only allowed there."""

    body: Annotated[str, Field(min_length=1, max_length=500)]
    name: Name | None = None
    size: Annotated[float, Field(ge=0.001, le=1000)] | None = None
    extrude: Annotated[float, Field(ge=0, le=100)] | None = None
    mode: Literal["depth", "cutout"] | None = Field(
        default=None, description="depth: extruded type; cutout: engraved through plate"
    )
    plate: Name | None = Field(default=None, description="mesh the cutout is cut into")
    location: Vec3 | None = None
    rotation: Vec3 | None = None
    font: TextFont | None = Field(default=None, description="sha256-pinned font; default builtin")

    @field_validator("body")
    @classmethod
    def _no_nul(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("body must not contain NUL")
        return value

    @model_validator(mode="after")
    def _plate_iff_cutout(self) -> TextAdd:
        if (self.mode == "cutout") != (self.plate is not None):
            raise ValueError("plate is required for mode 'cutout' and only allowed there")
        return self


TOOLS = [
    build_tool("scene.clear", "Remove every object from the scene.", SceneClear),
    build_tool("object.add_primitive", "Add a cube, plane, sphere or cylinder.", AddPrimitive),
    build_tool("object.import_gltf", "Import a glTF/GLB asset from the assets dir.", ImportGltf),
    build_tool("render.set_engine", "Set the render engine.", SetEngine),
    build_tool("render.set_resolution", "Set the output resolution.", SetResolution),
    build_tool("render.set_property", "Set an allowlisted render setting.", SetProperty),
    build_tool(
        "curve.add", "Add a bezier or poly curve through points; bevel it into a tube.", CurveAdd
    ),
    build_tool("object.add_slab", "Add a box of given dimensions with bevelled edges.", AddSlab),
    build_tool(
        "object.array", "Repeat an object by an offset (Array modifier or linked).", ObjectArray
    ),
    build_tool(
        "object.boolean", "Cut, join or intersect a mesh with a hidden cutter.", ObjectBoolean
    ),
    build_tool(
        "object.parent", "Parent an object to another, keeping its transform.", ObjectParent
    ),
    build_tool("text.add", "Add 3D text: extruded depth or engraved cutout in a plate.", TextAdd),
]
