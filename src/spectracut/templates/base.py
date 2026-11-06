# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared shot-template machinery: slots, camera variants, per-aspect cameras and tiers.

A template is a pure function from ``(BrandKit, slots, variant, aspect)`` to a
declarative :class:`~spectracut.spec.recipe.Recipe` built only from the host's
fixed ``TOOL_NAMES`` registry. The control plane never imports ``bpy``.

Brand independence: colours come only from the kit palette (:func:`swatch`),
fonts from ``BrandKit.typography`` and the Look from the kit (:func:`resolve_look`),
so the same template renders any kit with no code change.

Per-aspect cameras: the 16:9 camera is ``camera.add``; the 9:16 camera is
``camera.add_aspect`` (a copy of the 16:9 camera), made active with ``camera.use``
and then re-framed with its own ``camera.set_transform`` and ``camera.set_lens``,
so the vertical view is composed for itself and never a crop of the 16:9 one.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import BaseModel, Field, JsonValue, field_validator

from spectracut.finishing import comp
from spectracut.finishing.color import ColorPipeline
from spectracut.finishing.looks import render_rules, resolve_look
from spectracut.finishing.typography import FontFile
from spectracut.spec._base import NonEmptyStr, Slug, StrictModel
from spectracut.spec.brand import BrandKit, Look
from spectracut.spec.recipe import Recipe, RecipeStep
from spectracut.studio.device_shot import BLENDER_VERSION, tier_steps
from spectracut.studio.node import Tier

TemplateAspect = Literal["16:9", "9:16"]
ASPECTS: tuple[TemplateAspect, ...] = ("16:9", "9:16")
RESOLUTIONS: dict[TemplateAspect, tuple[int, int]] = {"16:9": (1920, 1080), "9:16": (1080, 1920)}
LONG_EDGE = 1920
CAMERA = "camera"
VERTICAL_CAMERA = "camera_9x16"
PREVIEW_PERCENTAGE = 50

Vec3 = tuple[float, float, float]
Call = tuple[str, dict[str, JsonValue]]


class TemplateError(ValueError):
    """A template cannot be built from these slots and this kit."""


class PinnedAsset(StrictModel):
    """A file under the host assets dir, pinned by sha256 (the host re-checks the hash)."""

    path: NonEmptyStr
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

    @field_validator("path")
    @classmethod
    def _relative(cls, v: str) -> str:
        p = PurePosixPath(v)
        if p.is_absolute() or ".." in p.parts or "\\" in v:
            raise ValueError(f"asset path {v!r} must be relative to the assets dir")
        return v

    def arg(self) -> dict[str, JsonValue]:
        return {"path": self.path, "sha256": self.sha256}


class CameraVariant(StrictModel):
    """One framing of a template, with its own separately composed vertical camera."""

    id: Slug
    location: Vec3
    rotation: Vec3
    lens: Annotated[float, Field(ge=1, le=5000)]
    vertical_location: Vec3
    vertical_rotation: Vec3
    vertical_lens: Annotated[float, Field(ge=1, le=5000)]


@dataclass(frozen=True)
class Template[S: BaseModel]:
    """A brand-kit-driven shot template.

    ``scene(kit, slots, pipeline)`` returns the template's tool calls (geometry, materials,
    animation, lighting) without cameras or render settings; :meth:`build` adds
    ``scene.clear`` and the variant's cameras for ``aspect``. ``frames`` is the
    shot length its animation is keyed for (the job envelope's ``frame_end``).
    """

    id: str
    slots: type[S]
    variants: tuple[CameraVariant, ...]
    scene: Callable[[BrandKit, S, ColorPipeline], list[Call]]
    frames: int = 120

    def __post_init__(self) -> None:
        if len(self.variants) < 2:
            raise TemplateError(f"template {self.id!r} needs at least 2 camera variants")

    def variant(self, variant_id: str) -> CameraVariant:
        for v in self.variants:
            if v.id == variant_id:
                return v
        known = ", ".join(v.id for v in self.variants)
        raise TemplateError(f"template {self.id!r} has no variant {variant_id!r}: {known}")

    def build(
        self,
        kit: BrandKit,
        slots: S | Mapping[str, object],
        *,
        variant: str | None = None,
        aspect: TemplateAspect = "16:9",
        pipeline: ColorPipeline | None = None,
    ) -> Recipe:
        """The template's scene recipe for ``kit`` (no tier, Look or output yet).

        ``pipeline`` is the colour pipeline the shot will be viewed through (screen
        exposure is calibrated for it); it defaults to the production default.
        """
        filled = slots if isinstance(slots, self.slots) else self.slots.model_validate(slots)
        cam = self.variant(variant) if variant is not None else self.variants[0]
        calls: list[Call] = [("scene.clear", {})]
        calls += self.scene(kit, filled, pipeline or ColorPipeline())
        calls += camera_calls(cam, aspect)
        shot = f"{self.id}-{cam.id}-{aspect.replace(':', 'x')}"
        return Recipe(
            id=f"{kit.id}-{shot}"[:63],
            shot_id=shot[:63],
            blender_version=BLENDER_VERSION,
            steps=steps(calls),
        )

    def final(
        self,
        kit: BrandKit,
        slots: S | Mapping[str, object],
        *,
        variant: str | None = None,
        aspect: TemplateAspect = "16:9",
        look_id: str | None = None,
        pipeline: ColorPipeline | None = None,
    ) -> Recipe:
        """:meth:`build` at the final tier with the kit's Look and render passes."""
        pipeline = pipeline or ColorPipeline()
        recipe = self.build(kit, slots, variant=variant, aspect=aspect, pipeline=pipeline)
        look = resolve_look(kit, look_id)
        return final_tier_steps(recipe, look, pipeline, aspect=aspect)

    def preview(
        self,
        kit: BrandKit,
        slots: S | Mapping[str, object],
        *,
        variant: str | None = None,
        aspect: TemplateAspect = "16:9",
        pipeline: ColorPipeline | None = None,
    ) -> Recipe:
        """:meth:`build` at the preview tier (Eevee, half resolution) to PNG."""
        pipeline = pipeline or ColorPipeline()
        recipe = self.build(kit, slots, variant=variant, aspect=aspect, pipeline=pipeline)
        return preview_steps(recipe, pipeline, aspect=aspect)


# ---- helpers ------------------------------------------------------------------------


def steps(calls: Sequence[Call], start: int = 0) -> list[RecipeStep]:
    return [RecipeStep(index=start + i, tool=t, args=a) for i, (t, a) in enumerate(calls)]


def _rgb(hex_colour: str) -> list[JsonValue]:
    h = hex_colour.lstrip("#")
    return [round(int(h[i : i + 2], 16) / 255.0, 4) for i in (0, 2, 4)]


def swatch(kit: BrandKit, role: str) -> list[JsonValue]:
    """The kit swatch ``role`` as RGB in [0, 1]; a kit without that role is refused.

    Templates name the roles they paint with (``ink``, ``surface``, ``accent``), so
    any kit that defines them renders with no code change, and a kit that does not
    fails loudly instead of silently picking an unrelated colour.
    """
    swatches = kit.palette.swatches
    if role not in swatches:
        raise TemplateError(
            f"brand kit {kit.id!r} has no {role!r} swatch (has {', '.join(sorted(swatches))})"
        )
    return _rgb(swatches[role])


def check_copy(kit: BrandKit, text: str) -> str:
    """Refuse on-screen ``text`` containing one of the kit's forbidden words."""
    folded = text.casefold()
    for word in kit.forbidden_words:
        if word.casefold() in folded:
            raise TemplateError(f"text {text!r} uses forbidden word {word!r} for {kit.id!r}")
    return text


def font_arg(
    kit: BrandKit, role: str, fonts: Mapping[str, FontFile] | None
) -> dict[str, JsonValue] | None:
    """``text.add``'s pinned ``font`` for the kit's ``role`` family, or ``None`` (built-in).

    The family comes from ``kit.typography``; ``fonts`` maps a family to its
    pinned, commercial-safe file, as :func:`spectracut.finishing.typography.type_steps`.
    """
    families = kit.typography.families
    family = families.get(role) or families[sorted(families)[0]]
    file = (fonts or {}).get(family)
    if file is None:
        return None
    if not file.licence.commercial_safe:
        raise TemplateError(f"font {family!r} ({file.path}) is not commercial-safe")
    return {"path": file.path, "sha256": file.sha256}


def camera_calls(cam: CameraVariant, aspect: TemplateAspect) -> list[Call]:
    """The variant's 16:9 camera and, for 9:16, its own re-framed vertical camera."""
    calls: list[Call] = [
        (
            "camera.add",
            {"name": CAMERA, "location": list(cam.location), "rotation": list(cam.rotation)},
        ),
        ("camera.set_lens", {"mm": cam.lens}),
    ]
    if aspect == "9:16":
        calls += [
            ("camera.add_aspect", {"aspect": "9:16", "name": VERTICAL_CAMERA}),
            ("camera.use", {"name": VERTICAL_CAMERA, "long_edge": LONG_EDGE}),
            (
                "camera.set_transform",
                {
                    "location": list(cam.vertical_location),
                    "rotation": list(cam.vertical_rotation),
                },
            ),
            ("camera.set_lens", {"mm": cam.vertical_lens}),
        ]
    else:
        calls.append(("camera.use", {"name": CAMERA, "long_edge": LONG_EDGE}))
    return calls


def _at_aspect(tier: list[RecipeStep], aspect: TemplateAspect) -> list[RecipeStep]:
    width, height = RESOLUTIONS[aspect]
    out = []
    for step in tier:
        if step.tool == "render.set_resolution":
            args = {**step.args, "width": width, "height": height}
            step = step.model_copy(update={"args": args})
        out.append(step)
    return out


def final_tier_steps(
    recipe: Recipe, look: Look, pipeline: ColorPipeline, *, aspect: TemplateAspect = "16:9"
) -> Recipe:
    """``recipe`` at the final tier, colour-managed, under ``look``, writing render passes.

    Reuses ``tier_steps(FINAL)`` (at the aspect's resolution), the pipeline's
    view settings, the Look's render rules, and ``render.set_passes`` with
    ``render.set_output`` to multilayer EXR (:func:`comp.passes_steps`).
    """
    out = list(recipe.steps)
    out += _at_aspect(tier_steps(Tier.FINAL, start=len(out)), aspect)
    out += pipeline.render_steps(start=len(out))
    out += render_rules(look, start=len(out))
    out += comp.passes_steps(start=len(out))
    return recipe.model_copy(update={"steps": out})


def preview_steps(
    recipe: Recipe, pipeline: ColorPipeline, *, aspect: TemplateAspect = "16:9"
) -> Recipe:
    """``recipe`` at the preview tier (Eevee, half resolution), view-encoded to PNG."""
    out = list(recipe.steps)
    out += _at_aspect(tier_steps(Tier.PREVIEW, start=len(out)), aspect)
    out += pipeline.render_steps(start=len(out))
    out.append(RecipeStep(index=len(out), tool="render.set_output", args={"format": "PNG"}))
    return recipe.model_copy(update={"steps": out})
