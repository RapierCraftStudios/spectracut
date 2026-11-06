# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pre-production: the brand kit, art direction, style frames, storyboard and the animatic.

- ``brand-kit`` (node kind ``brand_kit``): the production's brand kit (``--brand-kit``)
  as a graph node, built in code from the node's ``brand_kit`` param by
  :func:`build_brand_kit`, so the art nodes read it as a declared input.
- ``art-director`` (node kind ``art_direction``): mood board, palette and the Look
  from the creative brief, the approved concept and the brand kit. Its palette must
  use the kit's swatches and its Look must be one of the kit's Looks
  (:meth:`ArtDirector.check_output`).
- ``style-frame-artist`` (kind ``style_frames``): plans a few key stills. The
  :class:`PreproductionRunner` then explores each on an image provider (a local
  model unless ChatGPT is configured, `storyboard.explore.choose_exploration_provider`)
  and renders it at the **final** tier through an injected renderer. On the farm that
  is Blender on a render host; the control plane never runs Blender. The node waits at
  its human gate. Each frame names the concept beat or signature visual it shows
  (``concept_ref``, :meth:`StyleFrameArtist.check_output`).
- ``storyboard`` (kind ``storyboard``): a board per shot with camera and action.
- ``animatic-editor`` (kind ``animatic``): cuts the boards into an
  :class:`~spectracut.edit.timeline.Edit` with temp VO (and music) and checks the
  timing is locked. Built in code, not by the text provider.

:func:`preproduction_nodes` declares the four nodes; :func:`brand_kit_node` the brand kit.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, JsonValue

from spectracut.edit.timeline import Edit
from spectracut.spec.brand import BrandKit
from spectracut.spec.brief import CreativeBrief
from spectracut.storyboard.animatic import TempAudio, build_animatic, check_locked_timing
from spectracut.storyboard.explore import ExploreProvider, explore
from spectracut.storyboard.models import (
    ArtDirection,
    ImageRecord,
    Storyboard,
    StyleFrame,
    StyleFrames,
)
from spectracut.studio import device_shot as ds
from spectracut.studio.department import (
    SKILLS_ROOT,
    CostEstimate,
    Department,
    DepartmentOutputError,
)
from spectracut.studio.departments.discovery import BRIEF
from spectracut.studio.departments.writer import CONCEPT, SCRIPT, Concept, Script
from spectracut.studio.gates import GateMode
from spectracut.studio.graph import RunContext, Runner, RunResult
from spectracut.studio.ledger import Usage
from spectracut.studio.node import NodeSpec, Tier

ART_DIRECTION = "art_direction"
STYLE_FRAMES = "style_frames"
STORYBOARD = "storyboard"
ANIMATIC = "animatic"
BRAND_KIT = "brand_kit"
BRAND_KIT_PARAM = ds.BRAND_KIT_PARAM
"""Param of the ``brand_kit`` node holding the kit's JSON (the shot nodes use the same)."""

#: Gate defaults: people approve the style frames and the animatic.
PREPRODUCTION_GATES: dict[str, GateMode] = {
    ART_DIRECTION: GateMode.CRITIC,
    STYLE_FRAMES: GateMode.HUMAN,
    STORYBOARD: GateMode.CRITIC,
    ANIMATIC: GateMode.HUMAN,
}


def _input_of(
    inputs: Mapping[str, JsonValue], kinds: Mapping[str, str], kind: str
) -> JsonValue | None:
    """The first input whose kind is ``kind`` (an id missing from ``kinds`` is its kind)."""
    for input_id, value in inputs.items():
        if kinds.get(input_id, input_id) == kind:
            return value
    return None


def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()


class BrandKitDepartment(Department):
    """The production's brand kit as a node; built in code by :func:`build_brand_kit`."""

    name = "brand-kit"
    produces = frozenset({BRAND_KIT})
    output = BrandKit
    skill_pack = SKILLS_ROOT / "art-director"
    cost = CostEstimate()


class ArtDirector(Department):
    """Mood board, palette, materials and the Look, from the concept and the brand kit."""

    name = "art-director"
    produces = frozenset({ART_DIRECTION})
    inputs: ClassVar[dict[str, type[BaseModel]]] = {
        BRIEF: CreativeBrief,
        CONCEPT: Concept,
        BRAND_KIT: BrandKit,
    }
    output = ArtDirection
    cost = CostEstimate(ai_requests=1, ai_input_tokens=3_000)

    @classmethod
    def check_output(
        cls,
        node_id: str,
        output: JsonValue,
        inputs: Mapping[str, JsonValue],
        kinds: Mapping[str, str],
    ) -> None:
        """With a brand kit input: palette hexes are kit swatches, the Look is a kit Look."""
        raw = _input_of(inputs, kinds, BRAND_KIT)
        if raw is None:
            return
        kit = BrandKit.model_validate(raw)
        art = ArtDirection.model_validate(output)
        allowed = {hex_.casefold() for hex_ in kit.palette.swatches.values()}
        off = [
            f"{name}={hex_}"
            for name, hex_ in art.palette.swatches.items()
            if hex_.casefold() not in allowed
        ]
        problems = []
        if off:
            problems.append(f"palette swatches not in brand kit {kit.id!r}: {', '.join(off)}")
        looks = [look.id for look in kit.looks]
        if art.look.id not in looks:
            problems.append(
                f"look {art.look.id!r} is not one of brand kit {kit.id!r}'s looks {looks}"
            )
        if problems:
            raise DepartmentOutputError(cls.name, node_id, "; ".join(problems))


class StyleFrameArtist(Department):
    """Plans the style frames from the concept; the runner explores and renders them."""

    name = "style-frame-artist"
    produces = frozenset({STYLE_FRAMES})
    inputs: ClassVar[dict[str, type[BaseModel]]] = {
        BRIEF: CreativeBrief,
        CONCEPT: Concept,
        BRAND_KIT: BrandKit,
        ART_DIRECTION: ArtDirection,
    }
    output = StyleFrames
    skill_pack = SKILLS_ROOT / "art-director"
    cost = CostEstimate(ai_requests=1, ai_input_tokens=3_000, gpu_minutes=6.0)

    @classmethod
    def check_output(
        cls,
        node_id: str,
        output: JsonValue,
        inputs: Mapping[str, JsonValue],
        kinds: Mapping[str, str],
    ) -> None:
        """With a concept input: each frame's ``concept_ref`` is a concept beat or visual."""
        raw = _input_of(inputs, kinds, CONCEPT)
        if raw is None:
            return
        concept = Concept.model_validate(raw)
        refs = {_norm(r) for r in (*concept.structure, *concept.visuals)}
        frames = StyleFrames.model_validate(output)
        off = [
            f"{f.id}={f.concept_ref!r}" for f in frames.frames if _norm(f.concept_ref) not in refs
        ]
        if off:
            raise DepartmentOutputError(
                cls.name,
                node_id,
                f"style frames reference no beat or signature visual of concept "
                f"{concept.title!r}: {', '.join(off)}",
            )


class StoryboardArtist(Department):
    """A board per shot, with camera and action."""

    name = "storyboard"
    produces = frozenset({STORYBOARD})
    inputs: ClassVar[dict[str, type[BaseModel]]] = {
        BRIEF: CreativeBrief,
        CONCEPT: Concept,
        SCRIPT: Script,
        BRAND_KIT: BrandKit,
        ART_DIRECTION: ArtDirection,
    }
    output = Storyboard
    cost = CostEstimate(ai_requests=1, ai_input_tokens=5_000)


class AnimaticEditor(Department):
    """Cuts the boards to temp VO on the edit timeline."""

    name = "animatic-editor"
    produces = frozenset({ANIMATIC})
    inputs: ClassVar[dict[str, type[BaseModel]]] = {SCRIPT: Script, STORYBOARD: Storyboard}
    output = Edit
    skill_pack = SKILLS_ROOT / "editor"
    cost = CostEstimate()


ART_DEPARTMENTS: tuple[type[Department], ...] = (
    BrandKitDepartment,
    ArtDirector,
    StyleFrameArtist,
    StoryboardArtist,
    AnimaticEditor,
)

StyleFrameRenderer = Callable[[StyleFrame, ArtDirection, Path], ImageRecord]
"""Renders one style frame to ``path`` at the final tier (the farm, in production)."""
VoiceFn = Callable[[str, Path], TempAudio]
"""Local temp VO: ``voice(text, path) -> TempAudio``."""


def brand_kit_node(kit: BrandKit) -> NodeSpec:
    """The ``brand_kit`` node carrying ``kit``; the art nodes read it as an input."""
    return NodeSpec(
        id=BRAND_KIT,
        kind=BRAND_KIT,
        department=BrandKitDepartment.name,
        params={BRAND_KIT_PARAM: kit.model_dump(mode="json")},
    )


def build_brand_kit(ctx: RunContext) -> RunResult:
    """Build a ``brand_kit`` node in code: its ``brand_kit`` param, validated."""
    raw = ctx.spec.params.get(BRAND_KIT_PARAM)
    if raw is None:
        raise ValueError(
            f"node {ctx.spec.id!r} has no {BRAND_KIT_PARAM!r} param "
            "(recreate the production with --brand-kit)"
        )
    kit = BrandKit.model_validate(raw)
    return RunResult(output=kit.model_dump(mode="json"), usage=Usage())


def preproduction_nodes(
    brief_id: str = BRIEF,
    script_id: str = SCRIPT,
    *,
    tier: Tier = Tier.FINAL,
    concept_id: str | None = None,
    brand_kit_id: str | None = None,
) -> list[NodeSpec]:
    """The four pre-production nodes; style frames build at ``tier`` (final).

    ``concept_id`` (the approved concept) and ``brand_kit_id`` are wired into art
    direction, style frames and storyboard when given, in the order each
    department declares its inputs.
    """

    def wired(*ids: str | None) -> tuple[str, ...]:
        return tuple(i for i in ids if i is not None)

    return [
        NodeSpec(
            id=ART_DIRECTION,
            kind=ART_DIRECTION,
            department=ArtDirector.name,
            inputs=wired(brief_id, concept_id, brand_kit_id),
        ),
        NodeSpec(
            id=STYLE_FRAMES,
            kind=STYLE_FRAMES,
            department=StyleFrameArtist.name,
            inputs=wired(brief_id, concept_id, brand_kit_id, ART_DIRECTION),
            tier=tier,
        ),
        NodeSpec(
            id=STORYBOARD,
            kind=STORYBOARD,
            department=StoryboardArtist.name,
            inputs=wired(brief_id, concept_id, script_id, brand_kit_id, ART_DIRECTION),
        ),
        NodeSpec(
            id=ANIMATIC,
            kind=ANIMATIC,
            department=AnimaticEditor.name,
            inputs=(script_id, STORYBOARD),
        ),
    ]


class PreproductionRunner:
    """Wraps a :class:`~spectracut.studio.runner.DepartmentRunner` for the art nodes.

    ``style_frames`` outputs gain a concept image (``explorer``) and a final-tier
    render (``renderer``); ``animatic`` is built in code from the script and boards,
    and ``brand_kit`` from its param (:func:`build_brand_kit`). Other kinds pass
    through to ``inner``.
    """

    def __init__(
        self,
        inner: Runner,
        departments: Mapping[str, type[Department]],
        *,
        explorer: ExploreProvider,
        renderer: StyleFrameRenderer,
        voice: VoiceFn,
        workdir: Path,
        music: TempAudio | None = None,
    ) -> None:
        self.inner = inner
        self.departments = departments
        self.explorer = explorer
        self.renderer = renderer
        self.voice = voice
        self.workdir = Path(workdir)
        self.music = music

    def __call__(self, ctx: RunContext) -> RunResult:
        if ctx.spec.kind == STYLE_FRAMES:
            return self._style_frames(ctx)
        if ctx.spec.kind == ANIMATIC:
            return self._animatic(ctx)
        if ctx.spec.kind == BRAND_KIT:
            return build_brand_kit(ctx)
        return self.inner(ctx)

    def _style_frames(self, ctx: RunContext) -> RunResult:
        if ctx.tier not in (Tier.FINAL, Tier.HERO):
            raise ValueError(f"style frames render at the final tier, got {ctx.tier.value!r}")
        planned = self.inner(ctx)
        art = ArtDirection.model_validate(ctx.inputs[ART_DIRECTION])
        frames = StyleFrames.model_validate(planned.output)
        done: list[StyleFrame] = []
        for frame in frames.frames:
            concept = explore(self.explorer, frame.prompt, self.workdir / f"{frame.id}-concept.png")
            render = self.renderer(
                frame.model_copy(update={"concept": concept}), art, self.workdir / f"{frame.id}.png"
            )
            done.append(
                frame.model_copy(update={"concept": concept, "render": render, "tier": "final"})
            )
        dept = self.departments[ctx.spec.department]
        output = dept.validate_output(ctx.spec.id, StyleFrames(frames=done).model_dump(mode="json"))
        ai = {**planned.usage.ai_usage, "image_exploration_requests": len(done)}
        return RunResult(output=output, usage=planned.usage.model_copy(update={"ai_usage": ai}))

    def _animatic(self, ctx: RunContext) -> RunResult:
        dept = self.departments[ctx.spec.department]
        script = Script.model_validate(ctx.inputs[SCRIPT])
        storyboard = Storyboard.model_validate(ctx.inputs[STORYBOARD])
        vo = {
            line.beat: self.voice(line.vo, self.workdir / f"vo-{n}.wav")
            for n, line in enumerate(script.lines)
            if line.vo
        }
        edit = build_animatic(ctx.spec.id, storyboard, vo, self.music)
        problems = check_locked_timing(edit, storyboard)
        if problems:
            raise ValueError("animatic timing is not locked: " + "; ".join(problems))
        return RunResult(
            output=dept.validate_output(ctx.spec.id, edit.model_dump(mode="json")), usage=Usage()
        )
