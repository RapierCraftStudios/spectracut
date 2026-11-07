# SPDX-License-Identifier: AGPL-3.0-or-later
"""The scene builder's verifier: does the frame show the description, with craft?

:func:`scene_critic` is a :class:`~spectracut.critique.critic.StudioCritic` whose
judging prompt (:func:`scene_verifier_prompt`) asks a :class:`VisionJudge` to
score a rendered frame against the style-frame description, the CGI craft
rubric of the ``scene-builder`` skill pack, the palette and the Look, and to
write specific revision notes. Only the vision judge is ever called; the
builder's text model never scores its own work.

:func:`style_frames_critic` is the Producer's gate reviewer for ``style_frames``
outputs: a blank frame (luma standard deviation below :data:`BLANK_STDDEV`, see
:func:`is_blank`) fails without a judge call; every other frame is scored against its
own description with :func:`scene_verifier_prompt` and fails below the profile's
``pass_score``. A flat plane or an empty render never passes the gate.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import cast

from pydantic import JsonValue, ValidationError

from spectracut.critique.critic import StudioCritic, _add_usage
from spectracut.providers.base import VisionJudge, judge_usage
from spectracut.qa.common import load_gray
from spectracut.skills import pack_dir
from spectracut.spec.brand import BrandKit
from spectracut.storyboard.models import ArtDirection, StyleFrames
from spectracut.studio.gates import Critic, CriticVerdict, Finding, Severity
from spectracut.studio.node import NodeSpec
from spectracut.studio.profiles import QualityProfile

PACK = pack_dir("scene-builder")
STYLE_FRAMES_KIND = "style_frames"
REVIEWER = "scene-verifier"
BLANK_STDDEV = 0.02
"""A frame whose luma standard deviation (0..1) is below this shows nothing: blank."""


def is_blank(path: Path | str) -> bool:
    """True when the PNG at ``path`` is flat (luma stddev below :data:`BLANK_STDDEV`).

    A missing frame counts as blank (there is nothing to look at); a file that is not
    an 8-bit PNG (EXR, 16-bit) cannot be measured here and is left to the judge.
    """
    try:
        gray = load_gray(Path(path).read_bytes())
    except OSError:
        return True
    if gray is None:
        return False
    if not gray.pixels:
        return True
    px = gray.pixels
    mu = sum(px) / len(px)
    return math.sqrt(sum((p - mu) ** 2 for p in px) / len(px)) < BLANK_STDDEV


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


def _fail(frame_id: str, text: str, key: str) -> Finding:
    return Finding(
        reviewer=REVIEWER, severity=Severity.HIGH, text=f"style frame {frame_id!r}: {text}", key=key
    )


def style_frames_critic(
    vision: VisionJudge | Callable[[], VisionJudge],
    *,
    profile: QualityProfile,
    art_for: Callable[[NodeSpec], ArtDirection | None],
    inner: Critic | None = None,
) -> Critic:
    """The gate Critic for ``style_frames``: blank or off-description frames fail.

    ``vision`` may be a judge or a zero-argument factory (called on the first frame
    that needs a score, so text-only runs never build one). ``art_for`` returns the
    node's art direction (palette, Look) for the prompt. Each blank frame (no judge
    call) and each frame scored below ``profile.critique.pass_score`` is one HIGH
    finding; an unrendered frame fails the output. Other kinds go to ``inner``
    (or pass).
    """
    rubric = (PACK / "rubric.md").read_text(encoding="utf-8")
    pass_score = profile.critique.pass_score
    judge: list[VisionJudge] = []

    def get_judge() -> VisionJudge:
        if not judge:
            judge.append(cast(VisionJudge, vision) if hasattr(vision, "score") else vision())
        return judge[0]

    def review(spec: NodeSpec, output: JsonValue) -> CriticVerdict:
        if spec.kind != STYLE_FRAMES_KIND:
            if inner is not None:
                return inner(spec, output)
            return CriticVerdict(passed=True, comment=f"{spec.kind}: no style-frame review")
        try:
            frames = StyleFrames.model_validate(output)
        except ValidationError as exc:
            return CriticVerdict(
                passed=False,
                comment=f"style_frames output is invalid: {exc.error_count()} error(s)",
            )
        art = art_for(spec)
        findings: list[Finding] = []
        failed: list[str] = []
        usage: dict[str, int] = {}
        scores: dict[str, float] = {}

        def flag(frame_id: str, text: str, key: str) -> None:
            findings.append(_fail(frame_id, text, key))
            failed.append(frame_id)
            scores.setdefault(frame_id, 0.0)

        for frame in frames.frames:
            if frame.render is None:
                flag(frame.id, "was not rendered", "style-frame-unrendered")
            elif is_blank(frame.render.path):
                blank = "the render is blank (a flat frame shows nothing of its description)"
                flag(frame.id, blank, "style-frame-blank")
            elif art is None:
                flag(frame.id, "no art direction to verify it against", "style-frame-no-art")
            else:
                j = get_judge()
                prompt = scene_verifier_prompt(frame.description, art, rubric=rubric)
                verdict = j.score(Path(frame.render.path), prompt)
                _add_usage(usage, judge_usage(j))
                scores[frame.id] = float(verdict.score)
                if verdict.score < pass_score:
                    notes = verdict.notes or "does not depict it"
                    text = f"scored {verdict.score:.2f} < {pass_score:.2f} "
                    flag(frame.id, text + f"against its description: {notes}", "style-frame-off")
        overall = min(scores.values()) if scores else 0.0
        comment = (
            f"style frames failing review: {', '.join(failed)}"
            if findings
            else f"every style frame depicts its description (min score {overall:.2f})"
        )
        return CriticVerdict(
            passed=not findings,
            scores={"overall": overall},
            comment=comment,
            findings=findings,
            usage=usage,
        )

    return review


__all__ = [
    "BLANK_STDDEV",
    "is_blank",
    "scene_critic",
    "scene_verifier_prompt",
    "style_frames_critic",
]
