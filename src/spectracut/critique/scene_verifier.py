# SPDX-License-Identifier: AGPL-3.0-or-later
"""The scene builder's verifier: does the frame show the description, with craft?

:func:`scene_critic` is a :class:`~spectracut.critique.critic.StudioCritic` whose
judging prompt (:func:`scene_verifier_prompt`) asks a :class:`VisionJudge` to
score a rendered frame against the style-frame description, the CGI craft
rubric of the ``scene-builder`` skill pack, the palette and the Look, and to
write specific revision notes. Only the vision judge is ever called; the
builder's text model never scores its own work.
"""

from __future__ import annotations

from collections.abc import Sequence

from spectracut.critique.critic import StudioCritic
from spectracut.providers.base import VisionJudge
from spectracut.skills import pack_dir
from spectracut.spec.brand import BrandKit
from spectracut.storyboard.models import ArtDirection
from spectracut.studio.node import NodeSpec
from spectracut.studio.profiles import QualityProfile

PACK = pack_dir("scene-builder")


def _palette(art: ArtDirection, brand_kit: BrandKit | None) -> list[str]:
    lines = [f"- {name}: {hex_}" for name, hex_ in art.palette.swatches.items()]
    if brand_kit is not None:
        lines += [f"- {name} (brand): {hex_}" for name, hex_ in brand_kit.palette.swatches.items()]
    return lines


def scene_verifier_prompt(
    description: str,
    art: ArtDirection,
    *,
    brand_kit: BrandKit | None = None,
    rubric: str,
    anchors: Sequence[str] = (),
) -> str:
    """The judge's prompt for one scene-builder frame."""
    look = art.look
    parts = [
        "You verify a 3D render against the description it was built from.",
        "",
        "Description:",
        description.strip(),
        "",
        "Score adherence first: every object, material, light and camera the description "
        "names must be present and recognisable in the frame. Then score the craft: "
        "composition, lighting (key, fill, rim; no haze over the subject), physical "
        "materials, macro detail and legibility of any on-screen text.",
        "",
        "Palette (colours must stay within it):",
        *_palette(art, brand_kit),
    ]
    if art.materials:
        parts += ["", "Materials:", *[f"- {m}" for m in art.materials]]
    parts += [
        "",
        f"Look {look.id} v{look.version}: bloom <= {look.bloom}, grain <= {look.grain}, "
        f"depth of field {'on' if look.depth_of_field else 'off'}; stay within its thresholds.",
    ]
    if brand_kit is not None and brand_kit.forbidden_words:
        parts += ["", "Forbidden words (fail any frame that shows one):"]
        parts += [f"- {w}" for w in brand_kit.forbidden_words]
    parts += ["", "Rubric:", rubric.strip()]
    if anchors:
        parts += ["", "Taste anchors (the target quality):"]
        parts += [f"- {a.strip()}" for a in anchors if a.strip()]
    parts += [
        "",
        "Notes: list specific revisions for the builder, naming the object, light, "
        "material or camera to change and how (for example 'add a rim light behind the "
        "stack', 'move the camera closer so the readout fills a third of the frame').",
    ]
    return "\n".join(parts)


def scene_critic(
    vision: VisionJudge,
    *,
    profile: QualityProfile,
    description: str,
    art: ArtDirection,
    brand_kit: BrandKit | None = None,
) -> StudioCritic:
    """A :class:`StudioCritic` that judges scene-builder frames with ``vision`` only."""
    rubric = (PACK / "rubric.md").read_text(encoding="utf-8")
    anchors = tuple(p.read_text(encoding="utf-8") for p in sorted((PACK / "anchors").glob("*.md")))

    def rubric_for(_spec: NodeSpec) -> str:
        return scene_verifier_prompt(description, art, brand_kit=brand_kit, rubric=rubric)

    def anchors_for(_spec: NodeSpec) -> tuple[str, ...]:
        return anchors

    return StudioCritic(vision, rubric_for=rubric_for, anchors_for=anchors_for, profile=profile)


__all__ = ["scene_critic", "scene_verifier_prompt"]
