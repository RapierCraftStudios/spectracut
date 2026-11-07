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
  is the scene builder (``SceneStyleFrameRenderer``: Claude builds the scene at the
  preview tier, the vision verifier scores it, the best recipe re-renders at the final
  tier) on a render host; the control plane never runs Blender. The node waits at
  its human gate, whose review page shows every rendered frame (``review_media``).
  Each frame names the concept beat or signature visual it shows
  (``concept_ref``, :meth:`StyleFrameArtist.check_output`).
- ``storyboard`` (kind ``storyboard``): a board per shot with camera and action. Each
  board's ``beat`` resolves to a script beat (:func:`~spectracut.storyboard.animatic.resolve_beats`)
  and every voiced script beat has a board (:meth:`StoryboardArtist.check_output`).
- ``animatic-editor`` (kind ``animatic``): cuts the boards into an
  :class:`~spectracut.edit.timeline.Edit` with temp VO (and music) and checks the
  timing is locked. Built in code, not by the text provider. The VO is the
  ``voiceover`` node's audio (else the local voice) with its real durations; each
  board is rendered to a PNG at the **preview** tier through an injected renderer
  and referenced by path; an injected preview writes a playable mp4 for review.

:func:`preproduction_nodes` declares the four nodes; :func:`brand_kit_node` the brand kit.

With a ``references`` input (:mod:`.references`) art direction and storyboard cite the
reference craft points they apply (``craft_refs``); the storyboard's average board
length and on-screen text are measured against the cited references
(:func:`~spectracut.critique.references.measure_storyboard`), and the style frames
record a reference-comparison score (``reference_comparison``) from the
``reference_judge``. No render may be a reference file (study for craft, never copy).
"""

from __future__ import annotations

import logging
import wave
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, JsonValue

from spectracut.audio.vo import VoiceoverTrack
from spectracut.critique.references import (
    ReferenceComparison,
    assert_not_reference,
    compare_frames,
    measure_storyboard,
)
from spectracut.edit.preview import AnimaticPreview
from spectracut.edit.timeline import Edit
from spectracut.providers.base import ProviderError, ReferenceJudge
from spectracut.providers.local.registry import get_provider, mock_mode, resolve_model
from spectracut.providers.local.runtime import ModelRuntime
from spectracut.providers.local.tts import LocalTts
from spectracut.spec.brand import BrandKit
from spectracut.spec.brief import CreativeBrief
from spectracut.spec.references import ReferenceSet
from spectracut.storyboard.animatic import (
    TempAudio,
    build_animatic,
    check_locked_timing,
    resolve_beats,
)
from spectracut.storyboard.explore import (
    CONFIG_KEY,
    ExploreProvider,
    choose_exploration_provider,
    explore,
)
from spectracut.storyboard.models import (
    DEVICE_RUNTIME_SHARE,
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
from spectracut.studio.departments.capture import capture_key
from spectracut.studio.departments.discovery import BRIEF, PRODUCT, ProductNotes
from spectracut.studio.departments.references import (
    REFERENCES,
    build_references,
    check_craft_refs,
    references_input,
)
from spectracut.studio.departments.writer import CONCEPT, SCRIPT, Concept, Script
from spectracut.studio.gates import GateMode
from spectracut.studio.graph import RunContext, Runner, RunResult
from spectracut.studio.ledger import Usage
from spectracut.studio.node import NodeSpec, Tier

log = logging.getLogger(__name__)

ART_DIRECTION = "art_direction"
STYLE_FRAMES = "style_frames"
STORYBOARD = "storyboard"
ANIMATIC = "animatic"
BRAND_KIT = "brand_kit"
VOICEOVER = "voiceover"
"""Kind of the sound department's voiceover node; the animatic reads its audio."""
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
        REFERENCES: ReferenceSet,
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
        """With a brand kit input: palette hexes are kit swatches, the Look is a kit Look.

        With a references input: ``craft_refs`` cites craft points of the set.
        """
        art = ArtDirection.model_validate(output)
        check_craft_refs(cls.name, node_id, art.craft_refs, inputs, kinds)
        raw = _input_of(inputs, kinds, BRAND_KIT)
        if raw is None:
            return
        kit = BrandKit.model_validate(raw)
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
        REFERENCES: ReferenceSet,
    }
    """``references`` is read by the runner's reference comparison (keyframes, not prompts)."""
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
        REFERENCES: ReferenceSet,
        PRODUCT: ProductNotes,
    }
    output = Storyboard
    cost = CostEstimate(ai_requests=1, ai_input_tokens=5_000)

    @classmethod
    def check_output(
        cls,
        node_id: str,
        output: JsonValue,
        inputs: Mapping[str, JsonValue],
        kinds: Mapping[str, str],
    ) -> None:
        """With a script input: every board covers a script beat; every VO beat has a board.

        With a references input: ``craft_refs`` cites craft points of the set, the
        average board length is within the cited references' pace range and no
        board's on-screen text exceeds the type bound (one idea per card).

        With a product input (#339): device boards cover at least
        ``DEVICE_RUNTIME_SHARE`` of the runtime, none sits on an interlude beat and
        each shows a captured (non-interlude) screen of the product's screen map.
        """
        storyboard = Storyboard.model_validate(output)
        product = _input_of(inputs, kinds, PRODUCT)
        if product is not None:
            _check_device_boards(cls.name, node_id, storyboard, ProductNotes.model_validate(product))
        refs = check_craft_refs(cls.name, node_id, storyboard.craft_refs, inputs, kinds)
        if refs is not None:
            measured = measure_storyboard(storyboard, refs)
            if measured.problems:
                raise DepartmentOutputError(cls.name, node_id, "; ".join(measured.problems))
        raw = _input_of(inputs, kinds, SCRIPT)
        if raw is None:
            return
        script = Script.model_validate(raw)
        beats = [line.beat for line in script.lines]
        resolved = resolve_beats(storyboard, beats)
        problems = []
        loose = [
            f"{b.shot_id}={b.beat!r}"
            for b, r in zip(storyboard.boards, resolved.boards, strict=True)
            if not r.beat
        ]
        if loose:
            problems.append(f"boards cover no script beat: {', '.join(loose)}")
        missing = _unboarded(script, resolved)
        if missing:
            problems.append(f"script beats with VO have no board: {missing}")
        if problems:
            raise DepartmentOutputError(cls.name, node_id, "; ".join(problems))


def _check_device_boards(
    name: str, node_id: str, storyboard: Storyboard, notes: ProductNotes
) -> None:
    """Device boards dominate the runtime and each shows a captured product screen."""
    problems = []
    share = storyboard.device_share()
    if share < DEVICE_RUNTIME_SHARE:
        abstract = [b.shot_id for b in storyboard.boards if b.device is None]
        problems.append(
            f"device boards cover {share:.0%} of the runtime, need at least "
            f"{DEVICE_RUNTIME_SHARE:.0%} (abstract boards: {', '.join(abstract)})"
        )
    interludes = {_norm(m.beat) for m in notes.screens if m.interlude}
    screens = {_norm(capture_key(m)) for m in notes.screens if not m.interlude}
    on_interlude = [
        f"{b.shot_id}={b.beat!r}"
        for b in storyboard.boards
        if b.device is not None and _norm(b.beat) in interludes
    ]
    if on_interlude:
        problems.append(f"device boards on interlude beats: {', '.join(on_interlude)}")
    unmapped = [
        f"{b.shot_id}={b.capture_key!r}"
        for b in storyboard.boards
        if b.device is not None
        and _norm(b.beat) not in interludes
        and _norm(b.capture_key) not in screens
    ]
    if unmapped:
        problems.append(
            f"device boards show no mapped product screen: {', '.join(unmapped)} "
            f"(screens: {', '.join(sorted(screens))})"
        )
    if problems:
        raise DepartmentOutputError(name, node_id, "; ".join(problems))


class AnimaticEditor(Department):
    """Cuts the rendered boards to temp VO on the edit timeline."""

    name = "animatic-editor"
    produces = frozenset({ANIMATIC})
    inputs: ClassVar[dict[str, type[BaseModel]]] = {
        SCRIPT: Script,
        STORYBOARD: Storyboard,
        VOICEOVER: VoiceoverTrack,
        ART_DIRECTION: ArtDirection,
    }
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

StyleFrameRenderer = Callable[
    [StyleFrame, ArtDirection, Path], ImageRecord | tuple[ImageRecord, Usage]
]
"""Renders one style frame to ``path`` at the final tier (the farm, in production).

It may also return the :class:`Usage` the render consumed (GPU minutes) for the ledger.
"""
VoiceFn = Callable[[str, Path], TempAudio]
"""Local temp VO: ``voice(text, path) -> TempAudio``."""


def _unboarded(script: Script, resolved: Storyboard) -> list[str]:
    """Script beats with VO that no board (beats already resolved) covers."""
    covered = {b.beat for b in resolved.boards}
    return [line.beat for line in script.lines if line.vo and line.beat not in covered]


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


def default_explorer(
    config: Mapping[str, Any] | None = None, *, runtime: ModelRuntime | None = None
) -> ExploreProvider | None:
    """The concept-image provider: the mock in mock mode, else the local ``image`` model.

    The local model needs a ``runtime`` (the control plane on a render host); with
    none there is no explorer and style frames render from the art direction alone.
    ``config`` may opt in to ChatGPT images (``choose_exploration_provider``). When
    the local image model is not installed on the model host
    (:func:`concept_skip_reason`) there is no explorer either.
    """
    if mock_mode():
        return get_provider("image")
    if runtime is None:
        return None
    reason = concept_skip_reason(config, runtime=runtime)
    if reason is not None:
        log.warning("%s", reason)
        return None
    return choose_exploration_provider(config, local=get_provider("image", runtime))


def concept_skip_reason(
    config: Mapping[str, Any] | None = None, *, runtime: ModelRuntime | None = None
) -> str | None:
    """Why concept images are skipped: the local image model is not installed, else ``None``.

    Only a runtime whose ``installed`` set is known (setup state recorded its host)
    can say so; mock mode, no runtime, an unknown set or ChatGPT images give ``None``.
    """
    if mock_mode() or runtime is None:
        return None
    installed = getattr(runtime, "installed", None)
    if installed is None:
        return None
    choice = str((config or {}).get(CONFIG_KEY, "local")).strip().lower() or "local"
    if choice != "local":
        return None
    spec = resolve_model("image")
    if spec.id in installed:
        return None
    host = getattr(runtime, "_host", "local")
    return (
        f"concept images skipped: image model {spec.id!r} is not installed on model "
        f"host {host!r} (`spectracut setup --models all` installs it)"
    )


def _wav_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as wav:
        rate = wav.getframerate()
        return wav.getnframes() / rate if rate else 0.0


def default_voice(*, runtime: ModelRuntime | None = None) -> VoiceFn | None:
    """Local temp VO through :class:`LocalTts` (the mock in mock mode), else ``None``."""
    if not mock_mode() and runtime is None:
        return None
    tts = LocalTts(runtime=runtime)

    def voice(text: str, path: Path) -> TempAudio:
        out = tts.synthesize(text, path)
        return TempAudio(media_ref=str(out), duration_s=_wav_seconds(out))

    return voice


def preproduction_nodes(
    brief_id: str = BRIEF,
    script_id: str = SCRIPT,
    *,
    tier: Tier = Tier.FINAL,
    concept_id: str | None = None,
    brand_kit_id: str | None = None,
    voiceover_id: str | None = None,
    references_id: str | None = None,
) -> list[NodeSpec]:
    """The four pre-production nodes; style frames build at ``tier`` (final).

    ``concept_id`` (the approved concept) and ``brand_kit_id`` are wired into art
    direction, style frames and storyboard when given, in the order each
    department declares its inputs. ``voiceover_id`` (the voiceover node) is wired
    into the animatic when given, so it cuts to that audio. ``references_id`` (the
    ``references`` node) is wired into art direction, style frames and storyboard when
    given.
    """

    def wired(*ids: str | None) -> tuple[str, ...]:
        return tuple(i for i in ids if i is not None)

    return [
        NodeSpec(
            id=ART_DIRECTION,
            kind=ART_DIRECTION,
            department=ArtDirector.name,
            inputs=wired(brief_id, concept_id, brand_kit_id, references_id),
        ),
        NodeSpec(
            id=STYLE_FRAMES,
            kind=STYLE_FRAMES,
            department=StyleFrameArtist.name,
            inputs=wired(brief_id, concept_id, brand_kit_id, ART_DIRECTION, references_id),
            tier=tier,
        ),
        NodeSpec(
            id=STORYBOARD,
            kind=STORYBOARD,
            department=StoryboardArtist.name,
            inputs=wired(
                brief_id, concept_id, script_id, brand_kit_id, ART_DIRECTION, references_id
            ),
        ),
        NodeSpec(
            id=ANIMATIC,
            kind=ANIMATIC,
            department=AnimaticEditor.name,
            inputs=wired(script_id, STORYBOARD, voiceover_id, ART_DIRECTION),
        ),
    ]


class PreproductionRunner:
    """Wraps a :class:`~spectracut.studio.runner.DepartmentRunner` for the art nodes.

    ``style_frames`` outputs gain a concept image (``explorer``) and a final-tier
    render (``renderer``; in production the scene builder re-rendered at the final
    tier), whose GPU minutes and AI usage go to the node's usage; with ``media_root``
    (the production dir) the output also carries ``review_media`` listing every frame
    render relative to it, so the gate's review page shows them; ``brand_kit`` is
    built from its param (:func:`build_brand_kit`). ``animatic`` is built in code: its
    VO is the ``voiceover`` input's audio (else ``voice``, the local TTS), each board
    is rendered to ``workdir/boards/<shot>.png`` by ``board_renderer`` (the farm at
    the preview tier) and referenced by path, and ``preview`` writes
    ``workdir/animatic/<node>.mp4``. A voiced script with no VO source, or a voiced
    beat with no board, is an error, never a silent gap. With no ``explorer`` frames
    and boards render without a concept, and ``concept_note`` (why concept images are
    skipped) is reported once per build through ``note(node_id, text)``. An explorer
    failure never fails the node: it is logged and noted, and the rest of that build
    renders without concepts. ``references`` is built from its param
    (:func:`build_references`); style frames record ``reference_comparison``: each
    render scored by ``reference_judge`` against the keyframe of the references the
    art direction cites (a null score with a note when there is no judge or no local
    keyframe), and no render may be a reference file. Other kinds pass through to
    ``inner``.
    """

    def __init__(
        self,
        inner: Runner,
        departments: Mapping[str, type[Department]],
        *,
        explorer: ExploreProvider | None,
        renderer: StyleFrameRenderer,
        voice: VoiceFn | None,
        workdir: Path,
        board_renderer: StyleFrameRenderer,
        preview: AnimaticPreview,
        music: TempAudio | None = None,
        media_root: Path | None = None,
        concept_note: str | None = None,
        note: Callable[[str, str], None] | None = None,
        reference_judge: ReferenceJudge | None = None,
    ) -> None:
        self.inner = inner
        self.reference_judge = reference_judge
        self.concept_note = concept_note
        self.note = note
        self.media_root = None if media_root is None else Path(media_root)
        self.departments = departments
        self.explorer = explorer
        self.renderer = renderer
        self.voice = voice
        self.workdir = Path(workdir)
        self.music = music
        self.board_renderer = board_renderer
        self.preview = preview

    def __call__(self, ctx: RunContext) -> RunResult:
        if ctx.spec.kind == STYLE_FRAMES:
            return self._style_frames(ctx)
        if ctx.spec.kind == ANIMATIC:
            return self._animatic(ctx)
        if ctx.spec.kind == BRAND_KIT:
            return build_brand_kit(ctx)
        if ctx.spec.kind == REFERENCES:
            return build_references(ctx)
        return self.inner(ctx)

    def _note(self, node_id: str, text: str) -> None:
        if self.note is not None:
            self.note(node_id, text)

    def _skip_concepts(self, node_id: str) -> list[bool]:
        """Start one build's concepts: note a skip; the flag turns on once the explorer fails."""
        if self.explorer is None and self.concept_note:
            self._note(node_id, self.concept_note)
        return [self.explorer is None]

    def _concept(
        self, node_id: str, prompt: str, path: Path, skipped: list[bool]
    ) -> ImageRecord | None:
        """A concept image, or ``None``: skipped, or the explorer failed (logged and noted)."""
        if skipped[0] or self.explorer is None:
            return None
        try:
            return explore(self.explorer, prompt, path)
        except (ProviderError, OSError) as exc:
            skipped[0] = True
            text = f"concept images skipped: the image explorer failed: {exc}"
            log.warning("%s: %s", node_id, text)
            self._note(node_id, text)
            return None

    def _style_frames(self, ctx: RunContext) -> RunResult:
        if ctx.tier not in (Tier.FINAL, Tier.HERO):
            raise ValueError(f"style frames render at the final tier, got {ctx.tier.value!r}")
        planned = self.inner(ctx)
        art = ArtDirection.model_validate(ctx.inputs[ART_DIRECTION])
        frames = StyleFrames.model_validate(planned.output)
        done: list[StyleFrame] = []
        gpu = 0.0
        render_ai: dict[str, int] = {}
        explored = 0
        skipped = self._skip_concepts(ctx.spec.id)
        for frame in frames.frames:
            path = self.workdir / f"{frame.id}-concept.png"
            concept = self._concept(ctx.spec.id, frame.prompt, path, skipped)
            if concept is not None:
                explored += 1
            rendered = self.renderer(
                frame.model_copy(update={"concept": concept}), art, self.workdir / f"{frame.id}.png"
            )
            if isinstance(rendered, tuple):
                render, used = rendered
                gpu += used.gpu_minutes
                for key, value in used.ai_usage.items():
                    render_ai[key] = render_ai.get(key, 0) + value
            else:
                render = rendered
            done.append(
                frame.model_copy(update={"concept": concept, "render": render, "tier": "final"})
            )
        dept = self.departments[ctx.spec.department]
        result = StyleFrames(
            frames=done,
            review_media=self._review_media(done),
            reference_comparison=self._reference_comparison(ctx, art, done),
        )
        output = dept.validate_output(ctx.spec.id, result.model_dump(mode="json"))
        ai = dict(planned.usage.ai_usage)
        for key, value in render_ai.items():
            ai[key] = ai.get(key, 0) + value
        if explored:
            ai["image_exploration_requests"] = explored
        usage = planned.usage.model_copy(
            update={"ai_usage": ai, "gpu_minutes": planned.usage.gpu_minutes + gpu}
        )
        return RunResult(output=output, usage=usage)

    def _reference_comparison(
        self, ctx: RunContext, art: ArtDirection, frames: list[StyleFrame]
    ) -> ReferenceComparison | None:
        """Renders vs the cited references' keyframes; ``None`` with no references input.

        Every render path first passes the no-copy guard. Pace does not apply to stills.
        """
        refs = references_input(ctx.inputs, {})
        renders = [Path(f.render.path) for f in frames if f.render is not None]
        for path in renders:
            assert_not_reference(path, refs)
        if refs is None:
            return None
        return compare_frames(renders, refs, art.craft_refs, self.reference_judge)

    def _review_media(self, frames: list[StyleFrame]) -> dict[str, JsonValue] | None:
        """The gate's player descriptor: every frame render relative to ``media_root``."""
        if self.media_root is None:
            return None
        root = self.media_root.resolve()
        images: list[JsonValue] = []
        for frame in frames:
            if frame.render is None:
                continue
            path = Path(frame.render.path).resolve()
            if not path.is_relative_to(root):
                raise ValueError(f"style frame {frame.id!r} render {path} is outside {root}")
            images.append(path.relative_to(root).as_posix())
        if not images:
            return None
        return {"kind": "style_frame", "video": images[0], "images": images, "fps": 24}

    def _temp_vo(self, ctx: RunContext, script: Script) -> dict[str, TempAudio]:
        """Temp VO keyed by script beat: the voiceover input, else the local voice."""
        raw = _input_of(ctx.inputs, {}, VOICEOVER)
        if raw is not None:
            track = VoiceoverTrack.model_validate(raw)
            return {
                line.beat: TempAudio(media_ref=line.media_ref, duration_s=line.duration_s)
                for line in track.lines
            }
        voiced = [line for line in script.lines if line.vo]
        if not voiced:
            return {}
        if self.voice is None:
            raise ValueError("animatic needs temp VO: no voiceover input and no local voice")
        return {
            line.beat: self.voice(line.vo, self.workdir / f"vo-{n}.wav")
            for n, line in enumerate(script.lines)
            if line.vo
        }

    def _boards(
        self, node_id: str, storyboard: Storyboard, art: ArtDirection
    ) -> tuple[Storyboard, float, int]:
        """Each board rendered to ``workdir/boards/<shot>.png``; GPU minutes, explorations."""
        out = self.workdir / "boards"
        done = []
        gpu = 0.0
        explored = 0
        skipped = self._skip_concepts(node_id)
        for board in storyboard.boards:
            prompt = " ".join(p for p in (board.action, board.screen_content, board.text) if p)
            concept_path = out / f"{board.shot_id}-concept.png"
            concept = self._concept(node_id, prompt, concept_path, skipped)
            if concept is not None:
                explored += 1
            frame = StyleFrame(
                id=board.shot_id, description=board.action, prompt=prompt, concept=concept
            )
            path = (out / f"{board.shot_id}.png").absolute()
            rendered = self.board_renderer(frame, art, path)
            if isinstance(rendered, tuple):
                gpu += rendered[1].gpu_minutes
            if not path.is_file():
                raise ValueError(f"board {board.shot_id!r} was not rendered to {path}")
            done.append(board.model_copy(update={"image": str(path)}))
        return storyboard.model_copy(update={"boards": done}), gpu, explored

    def _animatic(self, ctx: RunContext) -> RunResult:
        dept = self.departments[ctx.spec.department]
        script = Script.model_validate(_input_of(ctx.inputs, {}, SCRIPT))
        storyboard = Storyboard.model_validate(_input_of(ctx.inputs, {}, STORYBOARD))
        raw_art = _input_of(ctx.inputs, {}, ART_DIRECTION)
        if raw_art is None:
            raise ValueError(
                f"animatic {ctx.spec.id!r} needs an {ART_DIRECTION!r} input to render its boards"
            )
        art = ArtDirection.model_validate(raw_art)
        vo = self._temp_vo(ctx, script)
        storyboard = resolve_beats(storyboard, [line.beat for line in script.lines])
        missing = _unboarded(script, storyboard)
        if missing:
            raise ValueError(f"animatic: script beats with VO have no board: {missing}")
        storyboard, gpu, explored = self._boards(ctx.spec.id, storyboard, art)
        edit = build_animatic(ctx.spec.id, storyboard, vo, self.music)
        problems = check_locked_timing(edit, storyboard)
        if problems:
            raise ValueError("animatic timing is not locked: " + "; ".join(problems))
        movie = self.workdir / "animatic" / f"{ctx.spec.id}.mp4"
        movie.parent.mkdir(parents=True, exist_ok=True)
        self.preview(edit, movie)
        if not movie.is_file() or movie.stat().st_size == 0:
            raise ValueError(f"animatic preview was not written to {movie}")
        ai: dict[str, int] = {"image_exploration_requests": explored} if explored else {}
        usage = Usage(gpu_minutes=gpu, ai_usage=ai)
        return RunResult(
            output=dept.validate_output(ctx.spec.id, edit.model_dump(mode="json")), usage=usage
        )
