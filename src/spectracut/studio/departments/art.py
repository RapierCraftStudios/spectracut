# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pre-production: art direction, style frames, storyboard and the animatic.

- ``art-director`` (node kind ``art_direction``): mood board, palette and the Look
  from the creative brief.
- ``style-frame-artist`` (kind ``style_frames``): plans a few key stills. The
  :class:`PreproductionRunner` then explores each on an image provider (a local
  model unless ChatGPT is configured, `storyboard.explore.choose_exploration_provider`)
  and renders it at the **final** tier through an injected renderer. On the farm that
  is Blender on a render host; the control plane never runs Blender. The node waits at
  its human gate.
- ``storyboard`` (kind ``storyboard``): a board per shot with camera and action.
- ``animatic-editor`` (kind ``animatic``): cuts the boards into an
  :class:`~spectracut.edit.timeline.Edit` with temp VO (and music) and checks the
  timing is locked. Built in code, not by the text provider.

:func:`preproduction_nodes` declares the four nodes.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel

from spectracut.edit.timeline import Edit
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
from spectracut.studio.department import SKILLS_ROOT, CostEstimate, Department
from spectracut.studio.departments.discovery import BRIEF
from spectracut.studio.departments.writer import SCRIPT, Script
from spectracut.studio.gates import GateMode
from spectracut.studio.graph import RunContext, Runner, RunResult
from spectracut.studio.ledger import Usage
from spectracut.studio.node import NodeSpec, Tier

ART_DIRECTION = "art_direction"
STYLE_FRAMES = "style_frames"
STORYBOARD = "storyboard"
ANIMATIC = "animatic"

#: Gate defaults: people approve the style frames and the animatic.
PREPRODUCTION_GATES: dict[str, GateMode] = {
    ART_DIRECTION: GateMode.CRITIC,
    STYLE_FRAMES: GateMode.HUMAN,
    STORYBOARD: GateMode.CRITIC,
    ANIMATIC: GateMode.HUMAN,
}


class ArtDirector(Department):
    """Mood board, palette, materials and the Look."""

    name = "art-director"
    produces = frozenset({ART_DIRECTION})
    inputs: ClassVar[dict[str, type[BaseModel]]] = {BRIEF: CreativeBrief}
    output = ArtDirection
    cost = CostEstimate(ai_requests=1, ai_input_tokens=3_000)


class StyleFrameArtist(Department):
    """Plans the style frames; the runner explores and renders them."""

    name = "style-frame-artist"
    produces = frozenset({STYLE_FRAMES})
    inputs: ClassVar[dict[str, type[BaseModel]]] = {
        BRIEF: CreativeBrief,
        ART_DIRECTION: ArtDirection,
    }
    output = StyleFrames
    skill_pack = SKILLS_ROOT / "art-director"
    cost = CostEstimate(ai_requests=1, ai_input_tokens=3_000, gpu_minutes=6.0)


class StoryboardArtist(Department):
    """A board per shot, with camera and action."""

    name = "storyboard"
    produces = frozenset({STORYBOARD})
    inputs: ClassVar[dict[str, type[BaseModel]]] = {
        BRIEF: CreativeBrief,
        SCRIPT: Script,
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
    ArtDirector,
    StyleFrameArtist,
    StoryboardArtist,
    AnimaticEditor,
)

StyleFrameRenderer = Callable[[StyleFrame, ArtDirection, Path], ImageRecord]
"""Renders one style frame to ``path`` at the final tier (the farm, in production)."""
VoiceFn = Callable[[str, Path], TempAudio]
"""Local temp VO: ``voice(text, path) -> TempAudio``."""


def preproduction_nodes(
    brief_id: str = BRIEF, script_id: str = SCRIPT, *, tier: Tier = Tier.FINAL
) -> list[NodeSpec]:
    """The four pre-production nodes; style frames build at ``tier`` (final)."""
    return [
        NodeSpec(
            id=ART_DIRECTION,
            kind=ART_DIRECTION,
            department=ArtDirector.name,
            inputs=(brief_id,),
        ),
        NodeSpec(
            id=STYLE_FRAMES,
            kind=STYLE_FRAMES,
            department=StyleFrameArtist.name,
            inputs=(brief_id, ART_DIRECTION),
            tier=tier,
        ),
        NodeSpec(
            id=STORYBOARD,
            kind=STORYBOARD,
            department=StoryboardArtist.name,
            inputs=(brief_id, script_id, ART_DIRECTION),
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
    render (``renderer``); ``animatic`` is built in code from the script and boards.
    Other kinds pass through to ``inner``.
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
