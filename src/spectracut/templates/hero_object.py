# SPDX-License-Identifier: AGPL-3.0-or-later
"""Hero-object template: any mesh on a palette plinth, animated by ``object.animate``.

The mesh is the brand kit's ``hero_object_mesh`` (a glTF under the assets dir,
``meshes/<asset id>.glb``), a glTF slot, or a primitive fallback. The animation
is one of the host presets: turntable, reveal, shatter (seeded, deterministic)
or assemble (shatter reversed).
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import Field, JsonValue, field_validator, model_validator

from spectracut.finishing.color import ColorPipeline
from spectracut.spec._base import StrictModel
from spectracut.spec.brand import BrandKit
from spectracut.templates.base import Call, CameraVariant, Template, swatch

HERO = "hero"
PLINTH = "plinth"
Animation = Literal["turntable", "reveal", "shatter", "assemble"]
Primitive = Literal["cube", "uv_sphere", "cylinder"]
MESH_DIR = "meshes"


def kit_mesh_path(kit: BrandKit) -> str | None:
    """Where the kit's ``hero_object_mesh`` asset id lives under the assets dir."""
    return None if kit.hero_object_mesh is None else f"{MESH_DIR}/{kit.hero_object_mesh}.glb"


class HeroSlots(StrictModel):
    """The mesh (glTF path, else the kit's hero mesh, else a primitive) and its animation."""

    mesh: str | None = Field(
        default=None,
        pattern=r"^[\w./-]+\.(glb|gltf)$",
        description="A glTF file under the assets dir; unset uses the kit's hero mesh.",
    )
    primitive: Primitive = Field(
        default="cube", description="The stand-in shape when there is no mesh."
    )
    animation: Animation = Field(default="turntable", description="How the object moves.")
    frame_start: Annotated[int, Field(ge=1)] = Field(
        default=1, description="First frame of the animation."
    )
    frame_end: Annotated[int, Field(ge=2)] = Field(
        default=120, description="Last frame of the animation."
    )
    seed: Annotated[int, Field(ge=0, le=2**31 - 1)] = Field(
        default=0, description="Seed for the shatter and assemble pieces."
    )
    pieces: Annotated[int, Field(ge=2, le=256)] = Field(
        default=24, description="Number of pieces for shatter and assemble."
    )

    @field_validator("mesh")
    @classmethod
    def _relative(cls, v: str | None) -> str | None:
        if v is not None and (v.startswith("/") or ".." in PurePosixPath(v).parts):
            raise ValueError(f"mesh {v!r} must be relative to the assets dir")
        return v

    @model_validator(mode="after")
    def _range(self) -> HeroSlots:
        if self.frame_end <= self.frame_start:
            raise ValueError("frame_end must be after frame_start")
        return self


def _animate(slots: HeroSlots) -> dict[str, JsonValue]:
    args: dict[str, JsonValue] = {
        "object": HERO,
        "preset": slots.animation,
        "frame_start": slots.frame_start,
        "frame_end": slots.frame_end,
    }
    if slots.animation == "turntable":
        args.update(degrees=360, axis="z")
    elif slots.animation == "reveal":
        args["from"] = "scale"
    else:
        args.update(pieces=slots.pieces, seed=slots.seed, distance=0.6)
    return args


def _scene(kit: BrandKit, slots: HeroSlots, pipeline: ColorPipeline) -> list[Call]:
    del pipeline  # no emissive surfaces to calibrate
    mesh = slots.mesh or kit_mesh_path(kit)
    hero: Call
    if mesh is not None:
        hero = ("object.import_gltf", {"path": mesh, "name": HERO, "location": [0, 0, 0.35]})
    else:
        hero = (
            "object.add_primitive",
            {"kind": slots.primitive, "name": HERO, "location": [0, 0, 0.35], "scale": [0.25] * 3},
        )
    return [
        (
            "object.add_primitive",
            {"kind": "plane", "name": "backdrop", "location": [0, 0, 0], "scale": [8, 8, 1]},
        ),
        (
            "material.apply_preset",
            {"object": "backdrop", "preset": "matte_plastic", "color": swatch(kit, "ink")},
        ),
        (
            "object.add_primitive",
            {
                "kind": "cylinder",
                "name": PLINTH,
                "location": [0, 0, 0.05],
                "scale": [0.5, 0.5, 0.05],
            },
        ),
        (
            "material.apply_preset",
            {"object": PLINTH, "preset": "ceramic", "color": swatch(kit, "surface")},
        ),
        hero,
        ("object.animate", _animate(slots)),
        (
            "light.add",
            {"kind": "area", "name": "key", "energy": 300, "location": [1.2, -1.2, 1.6]},
        ),
        (
            "light.add",
            {"kind": "area", "name": "rim", "energy": 200, "location": [-1.0, 1.0, 1.2]},
        ),
    ]


HERO_OBJECT = Template(
    id="hero-object",
    slots=HeroSlots,
    variants=(
        CameraVariant(
            id="eye-level",
            location=(0, -2.2, 0.55),
            rotation=(1.48353, 0, 0),
            lens=50,
            vertical_location=(0, -2.9, 0.6),
            vertical_rotation=(1.48353, 0, 0),
            vertical_lens=50,
        ),
        CameraVariant(
            id="low-hero",
            location=(1.1, -1.6, 0.25),
            rotation=(1.6057, 0, 0.6109),
            lens=35,
            vertical_location=(0.7, -1.2, 0.2),
            vertical_rotation=(1.6406, 0, 0.5236),
            vertical_lens=28,
        ),
    ),
    scene=_scene,
)
