# SPDX-License-Identifier: AGPL-3.0-or-later
"""Finishing: the edit, the graded master and the delivery set, built in code.

- ``edit`` (:class:`Editor`): conforms the animatic to the rendered shots. Every
  picture clip keeps the animatic's record position and length and points at its
  rendered shot (``edit.conform.replace_shot``); the ``vo`` track is rebuilt from the
  voiceover lines at the frames the mix places them, and the ``music`` track from the
  music bed when a music node is wired in. The edit is written as OTIO.
- ``render`` (:class:`Finisher`): re-renders every shot's base recipe at the final
  tier under the brand kit's Look (``templates.base.final_tier_steps``) and appends a
  brand end card carrying the brief's CTA when the edit has none. The CTA passes
  ``require_clean_copy`` and ``finishing.typography.copy_gate`` before any job is
  submitted.
- ``delivery`` (:class:`Delivery`): renders each shot again per delivery aspect (the
  template's vertical camera for 9:16, 1:1 and 4:5), cuts 15 s and 30 s cut-downs in
  16:9, 9:16, 1:1 and 4:5 from the master (``formats.cutdowns.cut_down``), writes
  every deliverable's OTIO and runs ``qa.report.run_qa`` on each. Every deliverable
  must pass QA or the node fails. Temp VO and non-commercial music are listed as temp
  items, and any temp item marks the report not passed, so it never ships.

Deliverables are OTIO timelines over approved per-aspect frame sequences; there is
no encoder in the control plane. Farm jobs go through the same signed
``JobQueue`` + Flamenco path :class:`~spectracut.studio.departments.shots.ShotRunner`
uses, and the control plane never imports ``bpy``. :func:`finishing_nodes`
declares the three nodes for the Producer (#262) to compose.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from spectracut.edit.conform import replace_shot
from spectracut.edit.otio_io import write_otio
from spectracut.edit.timeline import Clip, Edit, Gap, Track
from spectracut.finishing.color import ColorPipeline
from spectracut.finishing.looks import resolve_look
from spectracut.finishing.typography import CopyVerdict, TypeCard, copy_gate
from spectracut.formats.cutdowns import AspectRender, CutDownSpec, RenderLibrary, cut_down
from spectracut.formats.safe_areas import RESOLUTIONS
from spectracut.spec._base import Slug, StrictModel
from spectracut.spec.brand import BrandKit, Look
from spectracut.spec.brief import Brief
from spectracut.spec.recipe import Recipe, RecipeStep
from spectracut.spec.spec import Aspect
from spectracut.studio import device_shot as ds
from spectracut.studio.department import SKILLS_ROOT, CostEstimate, Department
from spectracut.studio.departments.art import ANIMATIC
from spectracut.studio.departments.shots import (
    SHOTS,
    TEMPLATE_SHOTS,
    RenderedShot,
    RenderedShots,
    ShotRunner,
    TemplateShot,
    _add,
    _kit,
    _rate_cost,
)
from spectracut.studio.departments.sound import (
    MIX,
    MUSIC,
    TEMP_VO,
    VOICEOVER,
    Mix,
    MusicBed,
    VoiceoverTrack,
    _vo_frames,
    is_temp_vo,
)
from spectracut.studio.estimate import BRIEF_PARAM
from spectracut.studio.graph import RunContext, Runner, RunResult
from spectracut.studio.ledger import Usage
from spectracut.studio.node import NodeSpec, Tier
from spectracut.templates import TEMPLATES
from spectracut.templates.base import camera_calls, final_tier_steps, require_clean_copy, steps
from spectracut.templates.end_card import END_CARD

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from spectracut.providers.base import VisionJudge
    from spectracut.qa.report import QAReport, QAReportStore
    from spectracut.queue.executors.flamenco import FlamencoApi

EDIT = "edit"
RENDER = "render"
DELIVERY = "delivery"

END_CARD_ID = "end-card"
"""Shot id of the end card the Finisher appends when the edit has none."""
END_CARD_TEMPLATES = frozenset({END_CARD.id, "end_card"})
CUTDOWN_LENGTHS: tuple[Literal[15, 30], ...] = (15, 30)
DELIVERY_ASPECTS: tuple[Aspect, ...] = ("16:9", "9:16", "1:1", "4:5")
PLATFORMS: dict[str, str] = {
    "16:9": "youtube",
    "9:16": "tiktok",
    "1:1": "meta_feed",
    "4:5": "meta_feed",
}
"""Target platform (safe areas, QA) of each delivery aspect."""
NON_COMMERCIAL = "non-commercial"

QARunner = Callable[..., "QAReport"]


# ------------------------------------------------------------------- outputs


class EditCut(StrictModel):
    """The conformed edit, its OTIO file and the rendered shots it points at."""

    edit: Edit
    otio_ref: str = Field(min_length=1)
    otio_sha256: str = Field(min_length=1)
    shots: list[RenderedShot] = Field(default_factory=list)
    """The rendered shots behind the picture clips (their recipes are re-rendered)."""


class EndCardRecord(StrictModel):
    """The end card's shot and the copy gate verdicts on its on-screen copy."""

    # ``copy`` shadows ``BaseModel.copy``, so the field is ``copy_lines`` serialised as
    # ``copy`` (``Department.validate_output`` dumps without ``by_alias``).
    model_config = ConfigDict(
        **StrictModel.model_config, validate_by_name=True, serialize_by_alias=True
    )

    shot_id: Slug
    copy_lines: list[str] = Field(default_factory=list, alias="copy")
    verdicts: list[CopyVerdict] = Field(default_factory=list)
    passed: bool


class GradedMaster(StrictModel):
    """Every shot of the edit re-rendered at the final tier under the kit's Look."""

    edit: Edit
    resolution: tuple[int, int]
    look_id: str
    shots: list[RenderedShot] = Field(min_length=1)
    end_card: EndCardRecord


class Deliverable(StrictModel):
    id: Slug
    kind: Literal["master", "cutdown"]
    length_s: float | None = None
    aspect: Aspect
    platform: str
    resolution: tuple[int, int]
    otio_ref: str
    otio_sha256: str
    frames: dict[str, str]
    qa_passed: bool
    qa_failures: list[str] = Field(default_factory=list)


class TempItem(StrictModel):
    node: str
    kind: str
    detail: str = ""


class DeliveryReport(StrictModel):
    master: Deliverable
    cutdowns: list[Deliverable] = Field(min_length=8, max_length=8)
    temp_items: list[TempItem] = Field(default_factory=list)
    passed: bool


# --------------------------------------------------------------- departments


class Editor(Department):
    """The animatic conformed to the rendered shots, with VO and music tracks."""

    name = "editor"
    produces = frozenset({EDIT})
    inputs: ClassVar[dict[str, type[BaseModel]]] = {
        ANIMATIC: Edit,
        TEMPLATE_SHOTS: RenderedShots,
        SHOTS: RenderedShots,
        VOICEOVER: VoiceoverTrack,
        MUSIC: MusicBed,
    }
    output = EditCut
    skill_pack = SKILLS_ROOT / "editor"
    cost = CostEstimate()


class Finisher(Department):
    """The graded master at the final tier, with the brand end card."""

    name = "finisher"
    produces = frozenset({RENDER})
    inputs: ClassVar[dict[str, type[BaseModel]]] = {EDIT: EditCut, MIX: Mix}
    output = GradedMaster
    skill_pack = SKILLS_ROOT / "compositor"
    uses_gpu = True
    cost = _rate_cost(RENDER)


class Delivery(Department):
    """The master plus 15 s and 30 s cut-downs in every delivery aspect, QA'd."""

    name = "delivery"
    produces = frozenset({DELIVERY})
    inputs: ClassVar[dict[str, type[BaseModel]]] = {
        RENDER: GradedMaster,
        VOICEOVER: VoiceoverTrack,
        MIX: Mix,
        MUSIC: MusicBed,
    }
    output = DeliveryReport
    skill_pack = SKILLS_ROOT / "editor"
    uses_gpu = True
    cost = _rate_cost(DELIVERY)


FINISHING_DEPARTMENTS: tuple[type[Department], ...] = (Editor, Finisher, Delivery)


def finishing_nodes(
    timing_id: str = ANIMATIC,
    shots_id: str = SHOTS,
    voiceover_id: str = VOICEOVER,
    music_id: str | None = MUSIC,
    mix_id: str | None = MIX,
    *,
    params: Mapping[str, JsonValue] | None = None,
) -> list[NodeSpec]:
    """The ``edit``, ``render`` and ``delivery`` nodes; music and mix are optional."""
    extra = dict(params or {})
    music = (music_id,) if music_id is not None else ()
    mix = (mix_id,) if mix_id is not None else ()
    return [
        NodeSpec(
            id=EDIT,
            kind=EDIT,
            department=Editor.name,
            inputs=(timing_id, shots_id, voiceover_id, *music),
            params=extra,
        ),
        NodeSpec(
            id=RENDER, kind=RENDER, department=Finisher.name, inputs=(EDIT, *mix), params=extra
        ),
        NodeSpec(
            id=DELIVERY,
            kind=DELIVERY,
            department=Delivery.name,
            inputs=(RENDER, voiceover_id, *mix, *music),
            params=extra,
        ),
    ]


# -------------------------------------------------------------------- helpers


def _input[M: BaseModel](ctx: RunContext, kind: str, model: type[M]) -> M | None:
    """The input of ``kind``: by id, else the last input that validates as ``model``."""
    if kind in ctx.inputs:
        return model.model_validate(ctx.inputs[kind])
    found: M | None = None
    for input_id in ctx.spec.inputs:
        if input_id not in ctx.inputs:
            continue
        try:
            found = model.model_validate(ctx.inputs[input_id])
        except ValidationError:
            continue
    return found


def _shot_hash(recipe_hash: str) -> str:
    return recipe_hash if recipe_hash.startswith("sha256:") else f"sha256:{recipe_hash}"


def _sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _write(edit: Edit, path: Path) -> tuple[str, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_otio(edit, path)
    return str(path), _sha256_file(path)


def _picture(edit: Edit) -> list[Clip]:
    """Picture clips with a shot, in record order of the first video track that has any."""
    for track in edit.tracks:
        clips = [c for c in track.clips() if c.shot_id is not None]
        if track.kind == "video" and clips:
            return clips
    return []


def _audio_track(track_id: str, placed: Sequence[tuple[int, Clip]], total: int) -> Track:
    """An audio track with each clip at its frame (later clips start after earlier ones)."""
    items: list[Clip | Gap] = []
    at = 0
    for frame, clip in sorted(placed, key=lambda p: p[0]):
        if frame > at:
            items.append(Gap(duration=frame - at))
            at = frame
        items.append(clip)
        at += clip.duration
    if at < total:
        items.append(Gap(duration=total - at))
    return Track(id=track_id, kind="audio", items=list(items))


def _with_track(edit: Edit, track: Track) -> Edit:
    tracks = [t for t in edit.tracks if t.id != track.id] + [track]
    return Edit.model_validate({**edit.model_dump(mode="json"), "tracks": tracks})


def aspect_recipe(shot: RenderedShot, look: Look, aspect: Aspect) -> Recipe:
    """``shot``'s base recipe through its template's camera for ``aspect`` at the final tier.

    16:9 keeps the template's main camera; 9:16, 1:1 and 4:5 use its vertical camera,
    rendered at ``formats.safe_areas.RESOLUTIONS[aspect]``.
    """
    base = Recipe.model_validate(shot.recipe)
    template_aspect: Literal["16:9", "9:16"] = "16:9" if aspect == "16:9" else "9:16"
    if template_aspect != shot.aspect and shot.template in TEMPLATES:
        template = TEMPLATES[shot.template]
        cam = template.variant(shot.variant) if shot.variant else template.variants[0]
        kept = [s for s in base.steps if not s.tool.startswith("camera.")]
        out = [s.model_copy(update={"index": n}) for n, s in enumerate(kept)]
        out += steps(camera_calls(cam, template_aspect), start=len(out))
        base = base.model_copy(update={"steps": out})
    graded = final_tier_steps(base, look, ColorPipeline(), aspect=template_aspect)
    width, height = RESOLUTIONS[aspect]
    sized: list[RecipeStep] = []
    for step in graded.steps:
        if step.tool == "render.set_resolution":
            step = step.model_copy(update={"args": {**step.args, "width": width, "height": height}})
        sized.append(step)
    return graded.model_copy(update={"steps": sized})


def _frames_of(shot: RenderedShot) -> int:
    """Frames the shot's job rendered (its template's animation length)."""
    if shot.template in TEMPLATES:
        return TEMPLATES[shot.template].frames
    return max(1, len(shot.frames))


def cutdown_shots(edit: Edit, end_card: str, length_s: float) -> list[str]:
    """Hook (first shot), middle shots in edit order until ``length_s`` is covered, end card.

    Raises ``ValueError`` when the whole master is shorter than ``length_s``.
    """
    clips = _picture(edit)
    target = edit.rate.frames(Fraction(length_s))
    if sum(c.duration for c in clips) < target:
        raise ValueError(
            f"master {edit.id!r} is {float(edit.seconds):.1f} s; "
            f"a {length_s:g} s cut-down needs a longer master"
        )
    by_id = {c.shot_id: c for c in clips}
    hook = clips[0].shot_id
    assert hook is not None
    picked = [hook] if hook != end_card else []
    covered = by_id[hook].duration if picked else 0
    if end_card in by_id:
        covered += by_id[end_card].duration
    for clip in clips[1:]:
        if covered >= target:
            break
        if clip.shot_id in (hook, end_card) or clip.shot_id is None:
            continue
        picked.append(clip.shot_id)
        covered += clip.duration
    if end_card in by_id:
        picked.append(end_card)
    return picked


# --------------------------------------------------------------------- runner


class FinishingRunner:
    """Builds the edit, render and delivery nodes; other kinds pass through to ``inner``."""

    def __init__(
        self,
        inner: Runner,
        departments: Mapping[str, type[Department]],
        *,
        config: ds.DeviceShotConfig,
        client: FlamencoApi,
        key: Ed25519PrivateKey,
        judge: VisionJudge | None,
        production_id: str,
        workdir: Path,
        options: ds.ExecutorOptions | None = None,
        qa_store: QAReportStore | None = None,
        qa: QARunner | None = None,
        brief: Brief | None = None,
    ) -> None:
        self.inner = inner
        self.departments = departments
        self.workdir = Path(workdir)
        self.production_id = production_id
        self.qa_store = qa_store
        self.brief = brief
        if qa is None:
            from spectracut.qa.report import run_qa

            qa = run_qa
        self.qa = qa
        self.farm = ShotRunner(
            inner,
            departments,
            config=config,
            client=client,
            key=key,
            judge=judge,
            production_id=production_id,
            options=options,
            qa_store=qa_store,
        )

    def __call__(self, ctx: RunContext) -> RunResult:
        if ctx.spec.kind == EDIT:
            return self._edit(ctx)
        if ctx.spec.kind == RENDER:
            return self._render(ctx)
        if ctx.spec.kind == DELIVERY:
            return self._delivery(ctx)
        return self.inner(ctx)

    def _validated(self, ctx: RunContext, output: BaseModel, usage: Usage) -> RunResult:
        dept = self.departments[ctx.spec.department]
        dumped = output.model_dump(mode="json", by_alias=True)
        return RunResult(output=dept.validate_output(ctx.spec.id, dumped), usage=usage)

    # ---- edit ----------------------------------------------------------------------

    def _edit(self, ctx: RunContext) -> RunResult:
        animatic = _input(ctx, ANIMATIC, Edit)
        if animatic is None:
            raise ValueError(
                f"node {ctx.spec.id!r} needs an animatic input (compose finishing_nodes "
                f"after the animatic and shot nodes)"
            )
        rendered = _input(ctx, SHOTS, RenderedShots) or _input(ctx, TEMPLATE_SHOTS, RenderedShots)
        if rendered is None:
            raise ValueError(f"node {ctx.spec.id!r} needs a shots or template_shots input")
        by_id = {s.shot_id: s for s in rendered.shots}
        edit = animatic.model_copy(update={"id": ctx.spec.id})
        used: list[RenderedShot] = []
        for clip in _picture(animatic):
            assert clip.shot_id is not None
            shot = by_id.get(clip.shot_id)
            if shot is None:
                raise ValueError(f"picture clip {clip.id!r}: shot {clip.shot_id!r} has no render")
            if shot not in used:
                used.append(shot)
                edit = replace_shot(
                    edit,
                    shot.shot_id,
                    media_ref=shot.output_dir,
                    shot_hash=_shot_hash(shot.recipe_hash),
                )
        total = animatic.duration
        vo = _input(ctx, VOICEOVER, VoiceoverTrack)
        if vo is not None:
            placed = [
                (
                    frame,
                    Clip(
                        id=f"vo-{line.id}",
                        media_ref=line.media_ref,
                        source_out=max(1, animatic.rate.frames(Fraction(line.duration_s))),
                    ),
                )
                for line, frame in zip(vo.lines, _vo_frames(animatic, vo), strict=True)
            ]
            edit = _with_track(edit, _audio_track("vo", placed, total))
        music = _input(ctx, MUSIC, MusicBed)
        if music is not None:
            frames = min(total, max(1, animatic.rate.frames(Fraction(music.duration_s))))
            bed = Clip(id="music-bed", media_ref=music.media_ref, source_out=frames)
            edit = _with_track(edit, _audio_track("music", [(0, bed)], total))
        ref, sha = _write(edit, self.workdir / f"{ctx.spec.id}.otio")
        cut = EditCut(edit=edit, otio_ref=ref, otio_sha256=sha, shots=used)
        return self._validated(ctx, cut, Usage())

    # ---- render --------------------------------------------------------------------

    def _brief(self, ctx: RunContext) -> Brief | None:
        raw = ctx.spec.params.get(BRIEF_PARAM)
        if raw is not None:
            try:
                return Brief.model_validate(raw)
            except ValidationError:
                pass
        return self.brief

    def _look(self, ctx: RunContext, kit: BrandKit) -> Look:
        look_id = ctx.spec.params.get(ds.LOOK_ID_PARAM)
        return resolve_look(kit, look_id if isinstance(look_id, str) else None)

    def _render(self, ctx: RunContext) -> RunResult:
        source = _input(ctx, EDIT, EditCut)
        if source is None:
            raise ValueError(f"node {ctx.spec.id!r} needs an edit input")
        if not source.shots:
            raise ValueError(f"edit {source.edit.id!r} has no rendered shots to grade")
        kit = _kit(ctx.spec)
        look = self._look(ctx, kit)
        builds: list[tuple[TemplateShot | RenderedShot, Recipe, Recipe, int]] = []
        for shot in source.shots:
            base = Recipe.model_validate(shot.recipe)
            graded = final_tier_steps(base, look, ColorPipeline(), aspect="16:9")
            builds.append((shot, base, graded, _frames_of(shot)))
        edit = source.edit
        if any(s.template in END_CARD_TEMPLATES for s in source.shots):
            card_id = next(s.shot_id for s in source.shots if s.template in END_CARD_TEMPLATES)
            record = EndCardRecord(shot_id=card_id, passed=True)
        else:
            record, build = self._end_card(ctx, kit, look, edit)
            builds.append(build)
            edit = self._append_card(edit, build[3])
        # Every gate has run: only now does anything go to the farm.
        usage = Usage()
        graded_shots: list[RenderedShot] = []
        for job, base, recipe, frames in builds:
            done, used = self.farm._render(ctx, job, base, recipe, Tier.FINAL, frames)
            graded_shots.append(done)
            usage = _add(usage, used)
        for done in graded_shots:
            edit = replace_shot(
                edit,
                done.shot_id,
                media_ref=done.output_dir,
                shot_hash=_shot_hash(done.recipe_hash),
            )
        master = GradedMaster(
            edit=edit,
            resolution=RESOLUTIONS["16:9"],
            look_id=look.id,
            shots=graded_shots,
            end_card=record,
        )
        return self._validated(ctx, master, usage)

    def _end_card(
        self, ctx: RunContext, kit: BrandKit, look: Look, edit: Edit
    ) -> tuple[EndCardRecord, tuple[TemplateShot, Recipe, Recipe, int]]:
        brief = self._brief(ctx)
        if brief is None:
            raise ValueError(f"node {ctx.spec.id!r} has no brief for the end card CTA")
        cta = brief.cta
        require_clean_copy(kit, cta)
        card = TypeCard(id=END_CARD_ID, preset="end-card", text=cta, start_frame=edit.duration)
        verdict = copy_gate(card, kit, brief)
        if not verdict.ok:
            raise ValueError(
                f"end card CTA {cta!r} fails the copy gate: {'; '.join(verdict.reasons)}"
            )
        slots: dict[str, JsonValue] = {"cta": cta}
        shot = TemplateShot(shot_id=END_CARD_ID, template=END_CARD.id, slots=slots)
        base = END_CARD.build(kit, slots, aspect="16:9")
        graded = final_tier_steps(base, look, ColorPipeline(), aspect="16:9")
        record = EndCardRecord.model_validate(
            {"shot_id": END_CARD_ID, "copy": [cta], "verdicts": [verdict], "passed": True}
        )
        return record, (shot, base, graded, END_CARD.frames)

    @staticmethod
    def _append_card(edit: Edit, frames: int) -> Edit:
        """``edit`` with the end card after the last picture clip (earlier clips stay put)."""
        tracks: list[Track] = []
        appended = False
        for track in edit.tracks:
            if not appended and track.kind == "video" and any(c.shot_id for c in track.clips()):
                clip = Clip(
                    id=f"pic-{END_CARD_ID}",
                    shot_id=END_CARD_ID,
                    media_ref=f"render:{END_CARD_ID}",
                    source_out=frames,
                )
                track = Track(id=track.id, kind=track.kind, items=[*track.items, clip])
                appended = True
            tracks.append(track)
        return Edit.model_validate({**edit.model_dump(mode="json"), "tracks": tracks})

    # ---- delivery ------------------------------------------------------------------

    def _delivery(self, ctx: RunContext) -> RunResult:
        master = _input(ctx, RENDER, GradedMaster)
        if master is None:
            raise ValueError(f"node {ctx.spec.id!r} needs a render input")
        kit = _kit(ctx.spec)
        look = self._look(ctx, kit)
        mix = _input(ctx, MIX, Mix)
        usage = Usage()
        renders: dict[Aspect, list[RenderedShot]] = {"16:9": list(master.shots)}
        for aspect in DELIVERY_ASPECTS[1:]:
            done: list[RenderedShot] = []
            for shot in master.shots:
                recipe = aspect_recipe(shot, look, aspect)
                base = Recipe.model_validate(shot.recipe)
                out, used = self.farm._render(ctx, shot, base, recipe, Tier.FINAL, _frames_of(shot))
                done.append(out)
                usage = _add(usage, used)
            renders[aspect] = done
        library = RenderLibrary(
            renders=tuple(
                AspectRender(
                    shot_id=src.shot_id,
                    shot_hash=_shot_hash(src.recipe_hash),
                    aspect=aspect,
                    media_ref=out.output_dir,
                    frames=_frames_of(src),
                    approved=out.qa.passed,
                )
                for aspect, outs in renders.items()
                for src, out in zip(master.shots, outs, strict=True)
            )
        )
        by_aspect = {a: {s.shot_id: s for s in outs} for a, outs in renders.items()}
        audio = Path(mix.master) if mix is not None else None
        workdir = self.workdir / ctx.spec.id
        main = self._deliverable(
            "master",
            "master",
            None,
            "16:9",
            master.edit,
            by_aspect["16:9"],
            workdir,
            kit,
            look,
            audio,
        )
        cutdowns: list[Deliverable] = []
        for length in CUTDOWN_LENGTHS:
            picked = cutdown_shots(master.edit, master.end_card.shot_id, length)
            for aspect in DELIVERY_ASPECTS:
                spec = CutDownSpec(
                    id=f"cutdown-{length}s-{aspect.replace(':', 'x')}",
                    length_s=length,
                    aspect=aspect,
                    platform=PLATFORMS[aspect],
                    shots=picked,
                )
                edit = cut_down(master.edit, library, spec)
                cutdowns.append(
                    self._deliverable(
                        spec.id,
                        "cutdown",
                        length,
                        aspect,
                        edit,
                        by_aspect[aspect],
                        workdir,
                        kit,
                        look,
                    )
                )
        failed = [d for d in (main, *cutdowns) if not d.qa_passed]
        if failed:
            detail = "; ".join(f"{d.id}: {', '.join(d.qa_failures) or 'failed'}" for d in failed)
            raise ValueError(f"delivery QA failed: {detail}")
        temp = self._temp_items(ctx)
        report = DeliveryReport(master=main, cutdowns=cutdowns, temp_items=temp, passed=not temp)
        return self._validated(ctx, report, usage)

    def _deliverable(
        self,
        id_: str,
        kind: Literal["master", "cutdown"],
        length: float | None,
        aspect: Aspect,
        edit: Edit,
        shots: Mapping[str, RenderedShot],
        workdir: Path,
        kit: BrandKit,
        look: Look,
        audio: Path | None = None,
    ) -> Deliverable:
        ref, sha = _write(edit, workdir / f"{id_}.otio")
        frames: dict[str, str] = {}
        data: list[bytes] = []
        for clip in _picture(edit):
            shot = shots[str(clip.shot_id)]
            for name, digest in sorted(shot.frames.items()):
                frames[f"{shot.shot_id}/{name}"] = digest
                path = Path(shot.output_dir) / name
                if path.is_file():
                    data.append(path.read_bytes())
        from spectracut.qa.copy_ocr import available_reader

        report = self.qa(
            f"{self.production_id}-{id_}"[:200],
            data,
            look,
            reader=available_reader(),
            brand_kit=kit,
            platform=PLATFORMS[aspect],
            aspect=aspect,
            audio=audio,
        )
        if self.qa_store is not None:
            self.qa_store.save(report)
        return Deliverable(
            id=id_,
            kind=kind,
            length_s=length,
            aspect=aspect,
            platform=PLATFORMS[aspect],
            resolution=RESOLUTIONS[aspect],
            otio_ref=ref,
            otio_sha256=sha,
            frames=frames,
            qa_passed=report.passed,
            qa_failures=[
                f"{c.name}: {c.detail}" if c.detail else c.name for c in report.failures()
            ],
        )

    @staticmethod
    def _temp_items(ctx: RunContext) -> list[TempItem]:
        """Temp VO and non-commercial sources among the delivery's inputs."""
        items: list[TempItem] = []
        vo = _input(ctx, VOICEOVER, VoiceoverTrack)
        if vo is not None and is_temp_vo(vo):
            items.append(
                TempItem(
                    node=VOICEOVER,
                    kind=TEMP_VO,
                    detail=f"{vo.provider} voiceover marked temp ({len(vo.lines)} lines)",
                )
            )
        music = _input(ctx, MUSIC, MusicBed)
        if music is not None and not music.provenance.commercial:
            items.append(
                TempItem(
                    node=MUSIC,
                    kind=NON_COMMERCIAL,
                    detail=f"{music.provenance.origin}: {music.provenance.licence}",
                )
            )
        return items
