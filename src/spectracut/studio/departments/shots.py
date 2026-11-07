# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shots: storyboard boards rendered on the farm from the shot template library.

- ``template-shots`` (node kind ``template_shots``): a :class:`ShotPlan` picks a
  template (devices, hero object, end card, typography), camera variant, aspect and
  slots for every storyboard board. The plan's schema enumerates the registered
  template ids; a board that fits no template sets ``scene_builder`` (a shot
  description) instead. With a ``scene_builder`` factory the :class:`ShotRunner`
  builds it (:func:`motion_steps` keyframes the board's lens, move and duration onto
  the builder's best recipe) and renders it as an animated sequence; without one it is
  recorded, not rendered, for the scene builder. The
  :class:`ShotRunner` builds each template board's recipe with
  ``TEMPLATES[t].preview(...)`` from the node's brand kit, stages the kit's assets,
  signs the job to shared storage and renders it as one Blender job
  through ``JobQueue`` + :class:`~spectracut.queue.executors.flamenco.FlamencoExecutor`,
  pinned to a registered GPU host by the config's worker tag.
- ``shots`` (kind ``shots``): re-renders every template and scene-built shot's recipe
  at the node's tier (``device_shot.tier_steps``).

Every rendered shot records its frames' sha256s, render QA and a vision-QA score of
its first frame; GPU minutes and ``vision_qa_*`` AI usage go to the node's ledger
entry. The node output is content-hashed by the graph, so a re-run with nothing
changed is a cache hit and submits no jobs. :func:`shot_critic` is the Critic gate on
those records; it fails any shot whose first frame is blank. :func:`shot_nodes`
declares the two nodes for the Producer (#262) to compose; the control plane only
builds declarative recipes and never imports ``bpy``.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Literal, Self, Union

from pydantic import (
    AfterValidator,
    BaseModel,
    Discriminator,
    Field,
    JsonValue,
    Tag,
    ValidationError,
    WithJsonSchema,
    create_model,
    model_validator,
)

from spectracut.critique.scene_verifier import PACK as SCENE_PACK
from spectracut.critique.scene_verifier import is_blank, scene_verifier_prompt
from spectracut.edit.timeline import Edit
from spectracut.finishing.color import BRAND_PIPELINE, ColorPipeline
from spectracut.finishing.looks import render_rules
from spectracut.queue.queue import Job, JobQueue
from spectracut.spec._base import Slug, StrictModel
from spectracut.spec.brand import BrandKit, Look
from spectracut.spec.recipe import Recipe, RecipeStep
from spectracut.storyboard.models import (
    DEVICE_RUNTIME_SHARE,
    ArtDirection,
    Board,
    MoodEntry,
    Storyboard,
)
from spectracut.studio import device_shot as ds
from spectracut.studio.department import SKILLS_ROOT, CostEstimate, Department
from spectracut.studio.departments import choreography as ch
from spectracut.studio.departments.art import ANIMATIC, STORYBOARD
from spectracut.studio.departments.capture import (
    UI_CAPTURE,
    CaptureFrame,
    ScreenCapture,
    UICaptures,
)
from spectracut.studio.estimate import STAGE_RATES
from spectracut.studio.gates import Critic, CriticVerdict, Finding, Severity
from spectracut.studio.graph import RunContext, Runner, RunResult
from spectracut.studio.ledger import Usage
from spectracut.studio.node import NodeSpec, Tier
from spectracut.templates import TEMPLATES
from spectracut.templates.base import (
    RESOLUTIONS,
    Call,
    PinnedAsset,
    Template,
    TemplateAspect,
    require_clean_copy,
)
from spectracut.templates.device_kit import (
    KIT_LENS,
    DeviceAsset,
    by_sha256,
    ensure_device_asset,
)
from spectracut.templates.devices import kit_entry, set_for

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from spectracut.providers.base import VisionJudge
    from spectracut.qa.report import QAReportStore
    from spectracut.queue.executors.flamenco import FlamencoApi
    from spectracut.studio.scene_builder import SceneBrief, SceneBuild

TEMPLATE_SHOTS = "template_shots"
SHOTS = "shots"
SHOT_PLAN = "shot_plan"
"""Kind of the planning call :class:`ShotRunner` makes through the text runner."""

SHOT_PARAMS = (ds.BRAND_KIT_PARAM, ds.ASSETS_DIR_PARAM)
"""Node params :func:`shot_nodes` applies to both shot nodes."""

_FILE = PurePosixPath


def _known_template(v: str) -> str:
    if v not in TEMPLATES:
        raise ValueError(f"unknown template {v!r}; known: {', '.join(sorted(TEMPLATES))}")
    return v


TemplateId = Annotated[
    str,
    AfterValidator(_known_template),
    WithJsonSchema({"type": "string", "enum": sorted(TEMPLATES)}),
]
"""A registered template id; the JSON schema sent to the planner enumerates them."""


class TemplateShot(StrictModel):
    """One board's template, camera variant, aspect and slot values.

    A board that fits no template sets ``scene_builder`` (a concrete description of
    the shot) instead of ``template``; exactly one of the two is set.
    """

    shot_id: Slug
    template: TemplateId | None = None
    scene_builder: str | None = Field(default=None, min_length=1)
    variant: str | None = None
    aspect: TemplateAspect = "16:9"
    slots: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _one_route(self) -> Self:
        if (self.template is None) == (self.scene_builder is None):
            raise ValueError(
                f"shot {self.shot_id!r} needs exactly one of template (a registered id) "
                f"or scene_builder (a shot description for the scene builder)"
            )
        return self


SCENE_BUILDER = "scene_builder"
"""Tag of the planner branch for a board no template fits."""


class SceneBuilderPlan(StrictModel):
    """A planned board no template fits: a concrete shot description for the scene builder."""

    shot_id: Slug
    template: None = Field(default=None, description="Unset: no template fits this board.")
    scene_builder: str = Field(
        min_length=1,
        description="The shot for the scene builder: subject, setting, camera, motion and mood.",
    )
    variant: None = None
    aspect: TemplateAspect = "16:9"
    slots: dict[str, JsonValue] = Field(default_factory=dict, max_length=0)


def _literal(values: tuple[str, ...]) -> Any:
    return Literal.__getitem__(values)


def _model_name(template_id: str) -> str:
    return "".join(p.capitalize() for p in template_id.replace("_", "-").split("-")) + "Plan"


def _template_plan(template: Template[Any]) -> type[StrictModel]:
    """The planner branch for ``template``: its id, its variants and its slot model."""
    variants = tuple(v.id for v in template.variants)
    if template.id.startswith("device-"):
        variants += tuple(ch.SHOT_PATHS)
    model: type[StrictModel] = create_model(
        _model_name(template.id),
        __base__=StrictModel,
        __doc__=f"A board rendered from the `{template.id}` template; slots follow its schema.",
        shot_id=(Slug, ...),
        template=(_literal((template.id,)), ...),
        variant=(_literal(variants) | None, Field(default=None, description="Camera variant.")),
        aspect=(TemplateAspect, "16:9"),
        slots=(template.slots, ...),
    )
    return model


def _route(value: Any) -> str:
    """The planner branch a shot belongs to: its template id, else the scene builder."""
    raw = value.get("template") if isinstance(value, Mapping) else getattr(value, "template", None)
    return SCENE_BUILDER if raw is None else str(raw)


TEMPLATE_PLANS: dict[str, type[StrictModel]] = {
    tid: _template_plan(t) for tid, t in sorted(TEMPLATES.items())
}
"""One planner branch per registered template, keyed by template id."""

PlannedShot: Any = Annotated[
    Union[  # noqa: UP007 - built from the registry at import time
        tuple(
            [Annotated[m, Tag(tid)] for tid, m in TEMPLATE_PLANS.items()]
            + [Annotated[SceneBuilderPlan, Tag(SCENE_BUILDER)]]
        )
    ],
    Discriminator(
        _route,
        custom_error_type="unknown_template",
        custom_error_message=(
            f"unknown template; known: {', '.join(sorted(TEMPLATES))} "
            f"(or leave template unset and set scene_builder)"
        ),
    ),
]
"""One planned board: a branch per registered template (``template`` -> its slot model),
or the scene builder. The JSON schema sent to the planner carries every slot schema."""


class ShotPlan(StrictModel):
    """The template shot, or scene-builder description, for every storyboard board."""

    shots: list[PlannedShot] = Field(min_length=1)


def _planned_shot(entry: BaseModel) -> TemplateShot:
    """``entry`` as the runner's :class:`TemplateShot` (only the values the planner set)."""
    return TemplateShot.model_validate(entry.model_dump(mode="json", exclude_unset=True))


def _plan_errors(exc: ValidationError, raw: JsonValue) -> str:
    """The plan's validation errors, each named by its shot id and branch."""
    shots = raw.get("shots") if isinstance(raw, dict) else None
    lines: list[str] = []
    for err in exc.errors():
        loc = list(err["loc"])
        where = ".".join(str(p) for p in loc)
        if len(loc) >= 2 and loc[0] == "shots" and isinstance(loc[1], int):
            shot = shots[loc[1]] if isinstance(shots, list) and loc[1] < len(shots) else None
            sid = shot.get("shot_id") if isinstance(shot, dict) else None
            field = ".".join(str(p) for p in loc[3:]) or ".".join(str(p) for p in loc[2:])
            where = f"shot {sid!r} ({loc[2] if len(loc) > 2 else '?'}): {field}"
        got = err.get("input")
        shown = "" if isinstance(got, (dict, list)) else f" (got {got!r})"
        lines.append(f"{where}: {err['msg']}{shown}")
    return "; ".join(lines)


class ShotQA(StrictModel):
    passed: bool
    failures: list[str] = Field(default_factory=list)


class VisionQA(StrictModel):
    score: float = Field(ge=0.0, le=1.0)
    notes: str = ""


class RenderedShot(StrictModel):
    """One board rendered on the farm."""

    shot_id: Slug
    template: str
    recipe: dict[str, JsonValue]
    """The template's scene recipe without tier steps (re-rendered by ``shots``)."""
    recipe_hash: str
    """Content hash of the recipe the job rendered (with tier steps)."""
    tier: Tier
    scene_graph_hash: str
    frames: dict[str, str]
    output_dir: str
    job_id: Slug
    worker_tag: str
    qa: ShotQA
    vision_qa: VisionQA
    variant: str | None = None
    aspect: TemplateAspect = "16:9"
    frame_end: int | None = Field(default=None, ge=1)
    """Last frame of a scene-built shot (``round(duration_s * fps)``); templates use theirs."""
    description: str | None = None
    """The brief a scene-built shot was built and vision-scored against."""
    capture: str | None = Field(default=None, exclude_if=lambda v: v is None)
    """A device shot's UI capture key (``beat:<beat>`` / ``proof:<claim>``); omitted otherwise."""
    screen_state: dict[str, str] | None = Field(default=None, exclude_if=lambda v: v is None)
    """The designed screen state a device shot shows: ``{state, beat, chrome, clip_sha256}``
    (see :func:`screen_state_record`); omitted when the capture has none."""
    cuts: list[dict[str, JsonValue]] | None = Field(default=None, exclude_if=lambda v: v is None)
    """A camera-path device shot's cut timing, ``[{name, start, end}]`` (#369)."""
    anchors: dict[str, dict[str, JsonValue]] | None = Field(
        default=None, exclude_if=lambda v: v is None
    )
    """A camera-path shot's resolved roles: ``{role: {selector, uv}}`` from the capture."""
    gate_stills: dict[str, str] | None = Field(default=None, exclude_if=lambda v: v is None)
    """The preview gate's 16 spp stills of each cut's first and last frame,
    ``{"<cut>:<frame>": path}``."""


class SceneBuilderShot(StrictModel):
    """A board no template fits: planned for the scene builder, not rendered here."""

    shot_id: Slug
    description: str = Field(min_length=1)
    aspect: TemplateAspect = "16:9"


class RenderedShots(StrictModel):
    shots: list[RenderedShot] = Field(default_factory=list)
    scene_builder: list[SceneBuilderShot] = Field(default_factory=list)
    """Boards planned for the scene builder; they have no render yet."""

    @model_validator(mode="after")
    def _not_empty(self) -> Self:
        if not self.shots and not self.scene_builder:
            raise ValueError("needs at least one rendered or scene-builder shot")
        return self


def _rate_cost(stage: str) -> CostEstimate:
    rate = STAGE_RATES[stage]
    return CostEstimate(
        ai_requests=rate.ai_requests,
        ai_input_tokens=rate.ai_input_tokens,
        gpu_minutes=rate.gpu_minutes_per_shot,
    )


class ShotPlanner(Department):
    """Picks a template, variant and slots per board; the runner renders them."""

    name = "shot-planner"
    produces = frozenset({SHOT_PLAN})
    inputs: ClassVar[dict[str, type[BaseModel]]] = {STORYBOARD: Storyboard, ANIMATIC: Edit}
    output = ShotPlan
    skill_pack = SKILLS_ROOT / "motion-designer"
    cost = CostEstimate()


class TemplateShotsDepartment(Department):
    """Storyboard boards rendered from the shot template library at the preview tier."""

    name = "template-shots"
    produces = frozenset({TEMPLATE_SHOTS})
    inputs: ClassVar[dict[str, type[BaseModel]]] = {
        STORYBOARD: Storyboard,
        ANIMATIC: Edit,
        UI_CAPTURE: UICaptures,
    }
    """Boards and animatic, and the product's UI captures for device shots (``--product``)."""
    output = RenderedShots
    skill_pack = SKILLS_ROOT / "motion-designer"
    uses_gpu = True
    cost = _rate_cost(TEMPLATE_SHOTS)


class ShotsDepartment(Department):
    """Every template shot re-rendered at the node's tier."""

    name = "shots"
    produces = frozenset({SHOTS})
    inputs: ClassVar[dict[str, type[BaseModel]]] = {
        TEMPLATE_SHOTS: RenderedShots,
        UI_CAPTURE: UICaptures,
    }
    """Template shots to re-render, and the product's UI captures (``--product`` only)."""
    output = RenderedShots
    skill_pack = SKILLS_ROOT / "motion-designer"
    uses_gpu = True
    cost = CostEstimate(gpu_minutes=STAGE_RATES[SHOTS].gpu_minutes_per_shot)


SHOT_DEPARTMENTS: tuple[type[Department], ...] = (
    ShotPlanner,
    TemplateShotsDepartment,
    ShotsDepartment,
)


def shot_nodes(
    storyboard_id: str = STORYBOARD,
    animatic_id: str = ANIMATIC,
    *,
    params: Mapping[str, JsonValue] | None = None,
) -> list[NodeSpec]:
    """The ``template_shots`` and ``shots`` nodes; ``params`` (brand kit, assets) on both."""
    extra = dict(params or {})
    return [
        NodeSpec(
            id=TEMPLATE_SHOTS,
            kind=TEMPLATE_SHOTS,
            department=TemplateShotsDepartment.name,
            inputs=(storyboard_id, animatic_id),
            params=extra,
        ),
        NodeSpec(
            id=SHOTS,
            kind=SHOTS,
            department=ShotsDepartment.name,
            inputs=(TEMPLATE_SHOTS,),
            params=extra,
        ),
    ]


def _render_tier(tier: Tier) -> Tier:
    """The ``device_shot.tier_steps`` tier a node tier renders with."""
    return Tier.PREVIEW if tier in (Tier.PREVIEW, Tier.REVIEW) else Tier.FINAL


def tier_recipe(
    base: Recipe,
    tier: Tier,
    aspect: TemplateAspect = "16:9",
    pipeline: ColorPipeline = BRAND_PIPELINE,
) -> Recipe:
    """``base`` plus ``device_shot`` tier steps at ``tier`` (at ``aspect``), to PNG.

    The colour ``pipeline`` steps come last but one, so they override any view
    transform or exposure the scene set; the default :data:`BRAND_PIPELINE`
    (Standard / sRGB / exposure 0) keeps linearised brand swatches on their hex.
    """
    width, height = RESOLUTIONS[aspect]
    out: list[RecipeStep] = list(base.steps)
    for step in ds.tier_steps(_render_tier(tier), start=len(out)):
        if step.tool == "render.set_resolution":
            step = step.model_copy(update={"args": {**step.args, "width": width, "height": height}})
        out.append(step)
    out += pipeline.render_steps(start=len(out))
    out.append(RecipeStep(index=len(out), tool="render.set_output", args={"format": "PNG"}))
    return base.model_copy(update={"steps": out})


FPS = 24
"""Frame rate of scene-built shots (the timeline default)."""
_STEP = 0.2
"""A camera move travels this fraction of the camera's distance from the origin."""


def _vec(value: JsonValue, default: list[float]) -> list[float]:
    if isinstance(value, list) and len(value) == 3:
        nums = [float(v) for v in value if isinstance(v, int | float)]
        if len(nums) == 3:
            return nums
    return list(default)


def _camera_state(recipe: Recipe) -> tuple[str, list[float], list[float]] | None:
    """The active camera's name, location and rotation at the end of ``recipe``."""
    cam: tuple[str, list[float], list[float]] | None = None
    for step in recipe.steps:
        args = step.args
        if step.tool == "camera.add":
            name = args.get("name")
            cam = (
                str(name) if isinstance(name, str) else "Camera",
                _vec(args.get("location"), [0.0, 0.0, 0.0]),
                _vec(args.get("rotation"), [0.0, 0.0, 0.0]),
            )
        elif step.tool == "camera.set_transform" and cam is not None:
            loc = _vec(args.get("location"), cam[1])
            rot = _vec(args.get("rotation"), cam[2])
            cam = (cam[0], loc, rot)
    return cam


def _animated(recipe: Recipe, camera: str) -> bool:
    """True when ``recipe`` already keys the camera (``camera.move`` or ``object.keyframe``)."""
    return any(
        step.tool == "camera.move"
        or (step.tool == "object.keyframe" and step.args.get("object") == camera)
        for step in recipe.steps
    )


def _move_end(move: str, loc: list[float], rot: list[float]) -> tuple[list[float], list[float]]:
    """Where a ``move`` ends, from the camera's start (Blender XYZ Euler, radians)."""
    x, y, z = rot
    forward = [
        -math.cos(x) * math.sin(y) * math.cos(z) - math.sin(x) * math.sin(z),
        -math.cos(x) * math.sin(y) * math.sin(z) + math.sin(x) * math.cos(z),
        -math.cos(x) * math.cos(y),
    ]
    right = [math.cos(y) * math.cos(z), math.cos(y) * math.sin(z), -math.sin(y)]
    d = max(0.3, _STEP * math.sqrt(sum(c * c for c in loc)))
    end_loc, end_rot = list(loc), list(rot)
    if move in ("push", "pull"):
        sign = 1.0 if move == "push" else -1.0
        end_loc = [c + sign * d * f for c, f in zip(loc, forward, strict=True)]
    elif move == "dolly":
        end_loc = [c + d * r for c, r in zip(loc, right, strict=True)]
    elif move == "pan":
        end_rot[2] += 0.25
    elif move == "tilt":
        end_rot[0] += 0.15
    elif move == "orbit":
        a = 0.5
        end_loc = [
            loc[0] * math.cos(a) - loc[1] * math.sin(a),
            loc[0] * math.sin(a) + loc[1] * math.cos(a),
            loc[2],
        ]
        end_rot[2] += a
    elif move == "crane":
        end_loc[2] += d
    return [round(c, 4) for c in end_loc], [round(c, 4) for c in end_rot]


def _json(values: list[float]) -> list[JsonValue]:
    return [round(v, 4) for v in values]


def shot_frames(board: Board, fps: int = FPS) -> int:
    """Frames a board's shot runs: ``round(duration_s * fps)``, at least two."""
    return max(2, round(board.duration_s * fps))


def motion_steps(board: Board, recipe: Recipe | None = None, *, fps: int = FPS) -> list[Call]:
    """The board's lens, camera move and frame range as harness calls on ``recipe``.

    ``camera.set_lens`` to ``board.lens_mm``; a ``camera.move`` keyframing the board's
    move from frame 1 to the last frame when the move is not ``static`` and the recipe
    does not already key its camera (``handheld`` is a small deterministic shake);
    ``render.set_frame_range`` 1..:func:`shot_frames`. With no camera in the recipe only
    the frame range is set.
    """
    end = shot_frames(board, fps)
    cam = _camera_state(recipe) if recipe is not None else None
    calls: list[Call] = []
    if recipe is None or cam is not None:
        calls.append(("camera.set_lens", {"mm": board.lens_mm}))
    keyed = recipe is not None and cam is not None and _animated(recipe, cam[0])
    if cam is not None and board.move != "static" and not keyed:
        name, loc, rot = cam
        keys: list[JsonValue]
        if board.move == "handheld":
            keys = []
            for n, frame in enumerate(range(1, end + 1, 6)):
                jitter = [0.01 * math.sin(n * k) for k in (1.3, 2.1, 0.7)]
                keys.append(
                    {
                        "frame": frame,
                        "location": _json([c + j for c, j in zip(loc, jitter, strict=True)]),
                        "rotation": _json(rot),
                    }
                )
            easing = "linear"
        else:
            end_loc, end_rot = _move_end(board.move, loc, rot)
            keys = [
                {"frame": 1, "location": _json(loc), "rotation": _json(rot)},
                {"frame": end, "location": _json(end_loc), "rotation": _json(end_rot)},
            ]
            easing = "ease_in_out"
        if len(keys) >= 2:
            calls.append(("camera.move", {"keys": keys, "easing": easing}))
    calls.append(("render.set_frame_range", {"frame_start": 1, "frame_end": end}))
    return calls


def with_motion(recipe: Recipe, board: Board, *, fps: int = FPS) -> Recipe:
    """``recipe`` plus :func:`motion_steps` for ``board``."""
    out = list(recipe.steps)
    for tool, args in motion_steps(board, recipe, fps=fps):
        out.append(RecipeStep(index=len(out), tool=tool, args=args))
    return recipe.model_copy(update={"steps": out})


# ---- device shots (#339) -----------------------------------------------------------------

DEVICE_VARIANTS = ("front", "three-quarter", "wide", "close", *ch.SHOT_PATHS)
"""Kit framings plus the camera paths (:data:`choreography.SHOT_PATHS`, #369)."""
GATE_SAMPLES = 16
"""Cycles samples of the preview gate's per-cut stills."""
DEVICE_FSTOP = 2.8
DEVICE_PIPELINE = ColorPipeline(
    view_transform="AgX", display="sRGB", look="AgX - Medium High Contrast"
)
"""The kit devices' colour pipeline (#362): AgX Medium High Contrast on sRGB."""
_HDRI_SUFFIXES = (".hdr", ".exr")


def board_capture(captures: UICaptures, board: Board) -> ScreenCapture:
    """The capture a device board shows: its capture key on its device kind."""
    key = board.capture_key
    for capture in captures.captures:
        if capture.key == key and capture.device == board.device:
            return capture
    raise ValueError(
        f"board {board.shot_id!r}: no {board.device} capture for {key!r} "
        f"(captured: {', '.join(sorted({f'{c.key}@{c.device}' for c in captures.captures}))})"
    )


def capture_frames(
    capture: ScreenCapture, frames: int, fps: int = FPS
) -> tuple[list[PinnedAsset], dict[str, Path]]:
    """The capture clip resampled to ``frames`` frames at ``fps``, last frame held.

    Returns the screen frames pinned as ``captures/<clip sha256>/<NNNN>.png`` (the
    host plays numbered sequences) and the staging map relpath -> capture file.
    A clip that holds one image for the whole shot is a single still frame.
    """
    clip = capture.screen.clip if capture.screen is not None else capture.clip
    picks = [clip[min(i * capture.fps // fps, len(clip) - 1)] for i in range(frames)]
    if len({f.sha256 for f in picks}) == 1:
        picks = picks[:1]
    return pin_frames(picks)


def screen_state_record(capture: ScreenCapture) -> dict[str, str] | None:
    """The beat -> screen state mapping of a composited capture, or ``None``.

    ``clip_sha256`` is the sha256 of the newline-joined frame sha256s of the
    composited clip, in order.
    """
    screen = capture.screen
    if screen is None:
        return None
    digest = hashlib.sha256("\n".join(f.sha256 for f in screen.clip).encode()).hexdigest()
    return {
        "state": screen.state,
        "beat": screen.beat,
        "chrome": screen.chrome,
        "clip_sha256": digest,
    }


def device_lighting(kit: BrandKit, assets: str | None) -> dict[str, JsonValue]:
    """Set slots from the kit: the ``dark`` set for a dark surface swatch, else
    ``light``; the kit's HDRI (first ``.hdr``/``.exr`` in its assets dir) only when it
    pins one."""
    slots: dict[str, JsonValue] = {"set": set_for(kit)}
    root = Path(assets) if assets else None
    if root is not None and root.is_dir():
        hdris = sorted(
            p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in _HDRI_SUFFIXES
        )
        if hdris:
            sha = hashlib.sha256(hdris[0].read_bytes()).hexdigest()
            slots["hdri"] = {"path": hdris[0].relative_to(root).as_posix(), "sha256": sha}
    return slots


def device_entry(recipe: Recipe) -> tuple[DeviceAsset, tuple[float, float, float]] | None:
    """The kit entry of ``recipe``'s first ``device.add`` and the device location."""
    for step in recipe.steps:
        if step.tool != "device.add":
            continue
        model = step.args.get("model")
        sha = model.get("sha256") if isinstance(model, dict) else None
        entry = by_sha256(sha) if isinstance(sha, str) else None
        kind = step.args.get("kind")
        if entry is None and kind in ("laptop", "phone"):
            entry = kit_entry(kind)  # type: ignore[arg-type]
        if entry is None:
            return None
        at = _vec(step.args.get("location"), [0.0, 0.0, 0.0])
        return entry, (at[0], at[1], at[2])
    return None


def screen_centre(recipe: Recipe) -> tuple[float, float, float]:
    """The world centre of the kit device's screen in ``recipe`` (its ``device.add``
    model's measured frame, moved by the device location)."""
    found = device_entry(recipe)
    if found is None:
        return (0.0, 0.0, 0.0)
    entry, at = found
    c = entry.frame.centre
    return (c[0] + at[0], c[1] + at[1], c[2] + at[2])


def focus_steps(recipe: Recipe, board: Board, look: Look, *, fps: int = FPS) -> list[Call]:
    """DoF on the screen with a focus pull, then the Look's render rules.

    ``camera.set_dof`` focuses on the screen centre (:func:`screen_centre`) at
    ``DEVICE_FSTOP``; a ``camera.move`` keys ``focus_distance`` from half the screen
    distance to the screen by 40% of the shot (following the board's move), so the
    pull ends on the screen; the Look's rules set motion blur and turn DoF off when
    the Look forbids it.
    """
    calls: list[Call] = []
    cam = _camera_state(recipe)
    centre = screen_centre(recipe)
    if cam is not None:
        _name, loc, rot = cam
        end = shot_frames(board, fps)
        pull = max(2, min(end, round(end * 0.4)))
        at = loc
        if board.move not in ("static", "handheld"):
            end_loc, _rot = _move_end(board.move, loc, rot)
            t = (pull - 1) / max(1, end - 1)
            at = [a + (b - a) * t for a, b in zip(loc, end_loc, strict=True)]
        near = math.dist(at, centre)
        start = math.dist(loc, centre)
        calls.append(("camera.set_dof", {"focus_distance": round(start, 4), "fstop": DEVICE_FSTOP}))
        keys: list[JsonValue] = [
            {"frame": 1, "focus_distance": round(near * 0.5, 4)},
            {"frame": pull, "focus_distance": round(near, 4)},
        ]
        calls.append(("camera.move", {"keys": keys, "easing": "ease_in_out"}))
    calls += [(step.tool, step.args) for step in render_rules(look)]
    return calls


def device_recipe(
    kit: BrandKit,
    board: Board,
    capture: ScreenCapture,
    *,
    variant: str | None,
    aspect: TemplateAspect,
    look: Look,
    assets: str | None = None,
) -> tuple[Recipe, dict[str, JsonValue], dict[str, Path]]:
    """A device board's recipe: the device template (the kit model, its camera
    derived from the measured screen frame at ``KIT_LENS`` mm) with the capture on
    its screen, the board's camera move and frame range, a focus pull onto the screen
    and the Look's rules.

    Pure: returns ``(recipe, slots, staging map of the captures)``; the department
    stages the model glb (:meth:`ShotRunner._device_shot`).
    """
    assert board.device is not None
    if variant in ch.SHOT_PATHS:
        built = device_path(
            kit, board, capture, ch.SHOT_PATHS[variant], aspect=aspect, look=look, assets=assets
        )
        return built.recipe, built.slots, built.extra
    screen, extra = capture_frames(capture, shot_frames(board))
    slots: dict[str, JsonValue] = {
        "screen_frames": [p.model_dump(mode="json") for p in screen],
        **device_lighting(kit, assets),
    }
    template = TEMPLATES[f"device-{board.device}"]
    variant = variant if variant in DEVICE_VARIANTS else "three-quarter"
    # The kit camera's lens holds: the board's move and range, not its lens.
    framed = board.model_copy(update={"lens_mm": KIT_LENS})
    base = with_motion(template.build(kit, slots, variant=variant, aspect=aspect), framed)
    out = list(base.steps)
    for tool, args in focus_steps(base, board, look):
        out.append(RecipeStep(index=len(out), tool=tool, args=args))
    return base.model_copy(update={"steps": out}), slots, extra


@dataclass(frozen=True)
class DevicePath:
    """A camera-path device shot (#369): its recipe, slots, staging map, the resolved
    anchors ``{role: {selector, uv}}`` and the cut timing ``[{name, start, end}]``."""

    recipe: Recipe
    slots: dict[str, JsonValue]
    extra: dict[str, Path]
    anchors: dict[str, dict[str, JsonValue]]
    cuts: list[dict[str, JsonValue]]
    frame_end: int


_ASPECT_RATIO = {"16:9": 16 / 9, "9:16": 9 / 16}


def capture_event_log(capture: ScreenCapture) -> list[object]:
    """The capture's ``events.json`` records; :class:`ValueError` when unreadable."""
    try:
        raw = json.loads(Path(capture.events).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(
            f"capture {capture.key!r}: cannot read events {capture.events}: {exc}"
        ) from None
    if not isinstance(raw, list):
        raise ValueError(f"capture {capture.key!r}: events {capture.events} is not a list")
    return raw


def pin_frames(frames: list[CaptureFrame]) -> tuple[list[PinnedAsset], dict[str, Path]]:
    """``frames`` pinned as ``captures/<digest>/<NNNN>.png`` and their staging map."""
    digest = hashlib.sha256("".join(f.sha256 for f in frames).encode()).hexdigest()
    pinned: list[PinnedAsset] = []
    extra: dict[str, Path] = {}
    for n, frame in enumerate(frames, start=1):
        rel = f"captures/{digest}/{n:04d}.png"
        pinned.append(PinnedAsset(path=rel, sha256=frame.sha256))
        extra[rel] = Path(frame.path)
    return pinned, extra


def device_path(
    kit: BrandKit,
    board: Board,
    capture: ScreenCapture,
    path: ch.ShotPath,
    *,
    aspect: TemplateAspect,
    look: Look,
    assets: str | None = None,
) -> DevicePath:
    """A device board on camera ``path``: the template at the three-quarter camera, one
    eased ``camera.move`` over the path keys (cut keys one frame apart hold, so cuts are
    hard), frame range 1..``path.frames``, the Look's rules, and the cut-aligned screen
    timeline on the screen. Anchors resolve from the capture's DOM anchors; a key whose
    aim or focus leaves the frame raises :class:`ValueError` before any farm job."""
    assert board.device is not None
    anchors = ch.resolve_anchors(path, capture)
    screen, extra = pin_frames(ch.screen_timeline(path, capture, capture_event_log(capture)))
    slots: dict[str, JsonValue] = {
        "screen_frames": [p.model_dump(mode="json") for p in screen],
        **device_lighting(kit, assets),
    }
    template = TEMPLATES[f"device-{board.device}"]
    base = template.build(kit, slots, variant="three-quarter", aspect=aspect)
    found = device_entry(base)
    if found is None:
        raise ValueError(f"device-{board.device} recipe has no kit device")
    entry, at = found
    basis = ch.screen_basis(entry, at)
    world = ch.anchors_world(path, capture, entry, at, anchors)
    problems = ch.framing_problems(path, world, basis, _ASPECT_RATIO[aspect])
    if problems:
        raise ValueError(f"path {path.id!r} framing: " + "; ".join(problems))
    calls: list[Call] = [
        ("camera.move", {"keys": ch.path_keys(path, world, basis), "easing": "ease_in_out"}),
        ("render.set_frame_range", {"frame_start": 1, "frame_end": path.frames}),
    ]
    calls += [(step.tool, step.args) for step in render_rules(look)]
    out = list(base.steps)
    for tool, args in calls:
        out.append(RecipeStep(index=len(out), tool=tool, args=args))
    recorded: dict[str, dict[str, JsonValue]] = {
        role: {"selector": sel, "uv": [u, v]} for role, (sel, (u, v)) in anchors.items()
    }
    return DevicePath(
        recipe=base.model_copy(update={"steps": out}),
        slots=slots,
        extra=extra,
        anchors=recorded,
        cuts=path.timing(),
        frame_end=path.frames,
    )


_DeviceBuild = tuple[
    TemplateShot,
    Recipe,
    Recipe,
    int,
    str,
    dict[str, Path],
    dict[str, str] | None,
    DevicePath | None,
]


def _captures(ctx: RunContext) -> UICaptures | None:
    raw = ctx.inputs.get(UI_CAPTURE)
    return UICaptures.model_validate(raw) if raw is not None else None


def shot_brief_text(description: str, board: Board) -> str:
    """The scene brief of a scene-built shot: the plan's description plus its board."""
    lines = [
        description.strip(),
        "",
        f"Board {board.shot_id}: {board.size} shot, {board.lens_mm:g} mm lens, "
        f"camera move {board.move}, {board.duration_s:g} s.",
        f"Action: {board.action}",
    ]
    if board.screen_content:
        lines.append(f"Screen content: {board.screen_content}")
    return "\n".join(lines)


def _kit_art(kit: BrandKit) -> ArtDirection:
    """An art direction from the brand kit alone (palette, default Look)."""
    look = next(lk for lk in kit.looks if lk.id == kit.default_look_id)
    return ArtDirection(
        mood_board=[MoodEntry(ref=kit.name, take=kit.tone)], palette=kit.palette, look=look
    )


def _is_file(value: str) -> bool:
    name = _FILE(value).name
    return "/" in value or ("." in name and not name.startswith(".") and " " not in value)


def _check_copy(kit: BrandKit, slots: Mapping[str, JsonValue]) -> None:
    """Every text slot (a string slot that is not an asset path) must use clean copy."""
    for value in slots.values():
        if isinstance(value, str) and not _is_file(value):
            require_clean_copy(kit, value)


def _kit(spec: NodeSpec) -> BrandKit:
    raw = spec.params.get(ds.BRAND_KIT_PARAM)
    if raw is None:
        raise ValueError(
            f"node {spec.id!r} has no brand kit (recreate the production with --brand-kit)"
        )
    return BrandKit.model_validate(raw)


def _assets(spec: NodeSpec) -> str | None:
    source = spec.params.get(ds.ASSETS_DIR_PARAM)
    return source if isinstance(source, str) and source else None


class ShotRunner:
    """Wraps the text runner for the shot nodes; other kinds pass through to ``inner``.

    ``scene_builder`` (``brief -> SceneBuild``, the scene builder at the preview tier)
    builds the boards no template fits; ``art_for`` gives their briefs the production's
    art direction (else the brand kit's palette and default Look). Without
    ``scene_builder`` those boards stay in ``RenderedShots.scene_builder``, unrendered.
    """

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
        options: ds.ExecutorOptions | None = None,
        qa_store: QAReportStore | None = None,
        scene_builder: Callable[[SceneBrief], SceneBuild] | None = None,
        art_for: Callable[[NodeSpec], ArtDirection | None] | None = None,
    ) -> None:
        self.scene_builder = scene_builder
        self.art_for = art_for
        self.inner = inner
        self.departments = departments
        self.config = config
        self.client = client
        self.key = key
        self.judge = judge
        self.production_id = production_id
        self.options = options or ds.ExecutorOptions()
        self.qa_store = qa_store

    def __call__(self, ctx: RunContext) -> RunResult:
        if ctx.spec.kind == TEMPLATE_SHOTS:
            return self._template_shots(ctx)
        if ctx.spec.kind == SHOTS:
            return self._shots(ctx)
        return self.inner(ctx)

    # ---- template_shots ------------------------------------------------------------

    def plan(self, ctx: RunContext) -> tuple[list[TemplateShot], Usage]:
        """Ask the text runner for a :class:`ShotPlan` covering every board, in board order."""
        if STORYBOARD not in ctx.inputs:
            raise ValueError(
                f"node {ctx.spec.id!r} needs a storyboard input (compose shot_nodes after "
                f"the storyboard and animatic nodes)"
            )
        storyboard = Storyboard.model_validate(ctx.inputs[STORYBOARD])
        planning = ctx.spec.model_copy(update={"kind": SHOT_PLAN, "department": ShotPlanner.name})
        result = self.inner(ctx.model_copy(update={"spec": planning}))
        try:
            plan = ShotPlan.model_validate(result.output)
        except ValidationError as exc:
            detail = _plan_errors(exc, result.output)
            raise ValueError(f"node {ctx.spec.id!r}: shot plan is invalid: {detail}") from None
        shots = [_planned_shot(entry) for entry in plan.shots]
        boards = [b.shot_id for b in storyboard.boards]
        planned = [s.shot_id for s in shots]
        unknown = sorted(set(planned) - set(boards))
        if unknown:
            raise ValueError(f"shot plan has shots not on the storyboard: {', '.join(unknown)}")
        missing = [b for b in boards if b not in planned]
        if missing:
            raise ValueError(f"shot plan leaves boards unplanned: {', '.join(missing)}")
        if len(set(planned)) != len(planned):
            raise ValueError("shot plan has more than one shot per board")
        order = {b: n for n, b in enumerate(boards)}
        return sorted(shots, key=lambda s: order[s.shot_id]), result.usage

    def _template_shots(self, ctx: RunContext) -> RunResult:
        shots, usage = self.plan(ctx)
        kit = _kit(ctx.spec)
        builds: list[tuple[TemplateShot, Recipe, Recipe]] = []
        pending: list[SceneBuilderShot] = []
        scene: list[tuple[TemplateShot, Board]] = []
        storyboard = Storyboard.model_validate(ctx.inputs[STORYBOARD])
        boards = {b.shot_id: b for b in storyboard.boards}
        captures = _captures(ctx)
        devices: list[_DeviceBuild] = []
        if captures is not None:
            share = storyboard.device_share()
            if share < DEVICE_RUNTIME_SHARE:
                abstract = [b.shot_id for b in storyboard.boards if b.device is None]
                raise ValueError(
                    f"node {ctx.spec.id!r}: device shots cover {share:.0%} of the storyboard "
                    f"runtime, need at least {DEVICE_RUNTIME_SHARE:.0%} "
                    f"(abstract boards: {', '.join(abstract) or 'none'})"
                )
        for shot in shots:
            board = boards[shot.shot_id]
            if captures is not None and board.device is not None:
                devices.append(self._device_shot(ctx, shot, board, kit, captures))
                continue
            if shot.template is None and self.scene_builder is not None:
                scene.append((shot, boards[shot.shot_id]))
                continue
            if shot.template is None:
                assert shot.scene_builder is not None
                pending.append(
                    SceneBuilderShot(
                        shot_id=shot.shot_id, description=shot.scene_builder, aspect=shot.aspect
                    )
                )
                continue
            template = TEMPLATES[shot.template]
            try:
                _check_copy(kit, shot.slots)
                base = template.build(kit, shot.slots, variant=shot.variant, aspect=shot.aspect)
                rendered = template.preview(
                    kit, shot.slots, variant=shot.variant, aspect=shot.aspect
                )
            except (ValueError, ValidationError) as exc:
                raise ValueError(f"shot {shot.shot_id!r} ({shot.template}): {exc}") from None
            builds.append((shot, base, rendered))
        done = [
            self._render(ctx, s, base, rendered, Tier.PREVIEW, TEMPLATES[str(s.template)].frames)
            for s, base, rendered in builds
        ]
        for s, base, rendered, frame_end, key, extra, record, path in devices:
            gate: dict[str, str] | None = None
            if path is not None:
                gate, used = self._gate(ctx, s, base, path, extra)
                usage = _add(usage, used)
            done.append(
                self._render(
                    ctx,
                    s,
                    base,
                    rendered,
                    Tier.PREVIEW,
                    frame_end,
                    capture=key,
                    extra=extra,
                    screen_state=record,
                    path=path,
                    gate_stills=gate,
                )
            )
        for shot, board in scene:
            built, used = self._scene_shot(ctx, shot, board, kit)
            done.append(built)
            usage = _add(usage, used)
        order = {s.shot_id: n for n, s in enumerate(shots)}
        done.sort(key=lambda d: order[d[0].shot_id])
        return self._result(ctx, done, usage, pending)

    def _device_shot(
        self,
        ctx: RunContext,
        shot: TemplateShot,
        board: Board,
        kit: BrandKit,
        captures: UICaptures,
    ) -> _DeviceBuild:
        """A device board on ``device-<kind>`` with its capture on the screen (#339)."""
        assert board.device is not None
        template = f"device-{board.device}"
        variant = shot.variant if shot.variant in DEVICE_VARIANTS else "three-quarter"
        built: DevicePath | None = None
        try:
            capture = board_capture(captures, board)
            if variant in ch.SHOT_PATHS:
                built = device_path(
                    kit,
                    board,
                    capture,
                    ch.SHOT_PATHS[variant],
                    aspect=shot.aspect,
                    look=ds.qa_look(ctx),
                    assets=_assets(ctx.spec),
                )
                base, slots, extra = built.recipe, built.slots, built.extra
            else:
                base, slots, extra = device_recipe(
                    kit,
                    board,
                    capture,
                    variant=variant,
                    aspect=shot.aspect,
                    look=ds.qa_look(ctx),
                    assets=_assets(ctx.spec),
                )
            extra.update(model_assets(base))
        except (ValueError, ValidationError) as exc:
            raise ValueError(f"shot {shot.shot_id!r} ({template}): {exc}") from None
        planned = shot.model_copy(
            update={"template": template, "scene_builder": None, "variant": variant, "slots": slots}
        )
        rendered = tier_recipe(base, Tier.PREVIEW, shot.aspect, DEVICE_PIPELINE)
        record = screen_state_record(capture)
        frames = built.frame_end if built is not None else shot_frames(board)
        return planned, base, rendered, frames, board.capture_key, extra, record, built

    def _art(self, ctx: RunContext, kit: BrandKit) -> ArtDirection:
        art = self.art_for(ctx.spec) if self.art_for is not None else None
        return art if art is not None else _kit_art(kit)

    def _scene_shot(
        self, ctx: RunContext, shot: TemplateShot, board: Board, kit: BrandKit
    ) -> tuple[tuple[RenderedShot, Usage], Usage]:
        """Build a board no template fits with the scene builder, then render it animated."""
        from spectracut.studio.scene_builder import SceneBrief

        assert self.scene_builder is not None and shot.scene_builder is not None
        description = shot_brief_text(shot.scene_builder, board)
        art = self._art(ctx, kit)
        build = self.scene_builder(
            SceneBrief(id=shot.shot_id, description=description, art=art, brand_kit=kit)
        )
        if build.recipe is None:
            raise ValueError(
                f"shot {shot.shot_id!r}: the scene builder produced no recipe "
                f"({build.status}: {build.notes or 'no candidate rendered'})"
            )
        base = with_motion(build.recipe, board)
        rendered = tier_recipe(base, Tier.PREVIEW, shot.aspect)
        frame_end = shot_frames(board)
        done = self._render(
            ctx,
            shot,
            base,
            rendered,
            Tier.PREVIEW,
            frame_end,
            template=SCENE_BUILDER,
            description=description,
            art=art,
        )
        return done, build.usage

    # ---- shots ---------------------------------------------------------------------

    def _shots(self, ctx: RunContext) -> RunResult:
        source = ctx.inputs.get(TEMPLATE_SHOTS)
        if source is None:
            raise ValueError(f"node {ctx.spec.id!r} needs a template_shots input")
        upstream = RenderedShots.model_validate(source)
        done: list[tuple[RenderedShot, Usage]] = []
        for prior in upstream.shots:
            base = Recipe.model_validate(prior.recipe)
            rendered = tier_recipe(base, ctx.tier, prior.aspect)
            if prior.capture is not None:
                rendered = tier_recipe(base, ctx.tier, prior.aspect, DEVICE_PIPELINE)
                extra = _restage(ctx, prior, base)
                frames = prior.frame_end or TEMPLATES[prior.template].frames
                done.append(
                    self._render(
                        ctx,
                        prior,
                        base,
                        rendered,
                        ctx.tier,
                        frames,
                        capture=prior.capture,
                        extra=extra,
                        screen_state=prior.screen_state,
                        cuts=prior.cuts,
                        anchors=prior.anchors,
                        gate_stills=prior.gate_stills,
                    )
                )
                continue
            if prior.template == SCENE_BUILDER:
                art = self._art(ctx, _kit(ctx.spec))
                frames = prior.frame_end or 1
                done.append(
                    self._render(
                        ctx,
                        prior,
                        base,
                        rendered,
                        ctx.tier,
                        frames,
                        template=SCENE_BUILDER,
                        description=prior.description,
                        art=art,
                    )
                )
                continue
            frames = TEMPLATES[prior.template].frames if prior.template in TEMPLATES else 1
            done.append(self._render(ctx, prior, base, rendered, ctx.tier, frames))
        return self._result(ctx, done, Usage(), upstream.scene_builder)

    # ---- farm ----------------------------------------------------------------------

    def _render(
        self,
        ctx: RunContext,
        shot: TemplateShot | RenderedShot,
        base: Recipe,
        recipe: Recipe,
        tier: Tier,
        frame_end: int,
        *,
        template: str | None = None,
        description: str | None = None,
        art: ArtDirection | None = None,
        capture: str | None = None,
        extra: Mapping[str, Path] | None = None,
        screen_state: dict[str, str] | None = None,
        path: DevicePath | None = None,
        cuts: list[dict[str, JsonValue]] | None = None,
        anchors: dict[str, dict[str, JsonValue]] | None = None,
        gate_stills: dict[str, str] | None = None,
    ) -> tuple[RenderedShot, Usage]:
        """Render one shot as one signed Blender job pinned to the config's worker tag.

        A scene-built shot passes ``template`` (:data:`SCENE_BUILDER`), its
        ``description`` and ``art``: its vision QA scores the first frame against the
        description with the scene verifier's prompt instead of the generic rubric.
        A device shot passes its ``capture`` key and the capture files to stage
        (``extra``); it records the key and its ``frame_end``. A camera-path device
        shot (``path``, or the ``cuts``/``anchors`` a re-render carries over) records its
        cut timing, resolved anchors and preview-gate stills (#369).
        """
        config, opts = self.config, self.options
        shot_id = shot.shot_id
        template = template or shot.template
        if template is None:
            raise ValueError(f"shot {shot_id!r} has no template to render")
        if extra is None:
            assets_dir, _sha = ds.stage_for(config, _assets(ctx.spec), recipe)
        else:
            assets_dir, _sha = ds.stage_for(config, _assets(ctx.spec), recipe, extra)
        base_id = f"{self.production_id}-{ctx.spec.id}-{shot_id}"[:96]
        job_id = ds.fresh_job_id(config, f"{base_id}-{recipe.content_hash[7:19]}")
        envelope = ds.envelope(job_id, recipe, config, opts.now(), frame_end=frame_end)
        job_file = ds.sign_to_shared(envelope, self.key, config.shared)
        out = config.out_dir(job_id)
        job = ds.blender_job(envelope, job_file, out, config, tier, assets_dir)
        queue = JobQueue([ds.executor(self.client, out, config, opts, assets_dir)], now=opts.now)
        result = queue.as_runner(lambda _ctx: job, sleep=opts.sleep)(ctx)
        scene, frames = ds.render_record(out)
        flamenco = result.output if isinstance(result.output, dict) else {}
        qa = ds.render_qa(ctx, job_id, out, ds.qa_look(ctx), self.qa_store)
        judge = self.judge
        if judge is None:
            from spectracut.providers.base import select_vision_judge

            judge = self.judge = select_vision_judge()
        rubric = ds.VISION_RUBRIC
        if description is not None and art is not None:
            pack_rubric = (SCENE_PACK / "rubric.md").read_text(encoding="utf-8")
            rubric = scene_verifier_prompt(description, art, rubric=pack_rubric)
        verdict = judge.score(out / sorted(frames)[0], rubric)
        usage = ds.vision_qa_usage(result.usage, judge)
        failures = qa.get("failures")
        rendered = RenderedShot(
            shot_id=shot_id,
            template=template,
            variant=shot.variant,
            aspect=shot.aspect,
            recipe=base.model_dump(mode="json"),
            recipe_hash=recipe.content_hash,
            tier=tier,
            scene_graph_hash=scene,
            frames=dict(frames),
            output_dir=str(out),
            job_id=job_id,
            worker_tag=str(flamenco.get("worker_tag") or config.worker_tag),
            qa=ShotQA(
                passed=bool(qa.get("passed")),
                failures=[str(f) for f in failures] if isinstance(failures, list) else [],
            ),
            vision_qa=VisionQA(score=float(verdict.score), notes=verdict.notes),
            frame_end=frame_end if template == SCENE_BUILDER or capture is not None else None,
            description=description if template == SCENE_BUILDER else None,
            capture=capture,
            screen_state=screen_state,
            cuts=path.cuts if path is not None else cuts,
            anchors=path.anchors if path is not None else anchors,
            gate_stills=gate_stills,
        )
        return rendered, usage

    def _gate(
        self,
        ctx: RunContext,
        shot: TemplateShot,
        base: Recipe,
        built: DevicePath,
        extra: Mapping[str, Path],
    ) -> tuple[dict[str, str], Usage]:
        """The preview gate (#369): each cut's first and last frame as single-frame
        Cycles jobs (final tier steps) at :data:`GATE_SAMPLES` samples, before the
        sequence render. Returns ``{"<cut>:<frame>": still path}`` for the Critic."""
        config, opts = self.config, self.options
        recipe = tier_recipe(base, Tier.FINAL, shot.aspect, DEVICE_PIPELINE)
        steps = list(recipe.steps)
        steps.append(
            RecipeStep(
                index=len(steps),
                tool="render.set_property",
                args={"property": "cycles.samples", "value": GATE_SAMPLES},
            )
        )
        recipe = recipe.model_copy(update={"steps": steps})
        assets_dir, _sha = ds.stage_for(config, _assets(ctx.spec), recipe, extra)
        stills: dict[str, str] = {}
        usage = Usage()
        for cut in built.cuts:
            name, start, end = str(cut["name"]), cut["start"], cut["end"]
            assert isinstance(start, int) and isinstance(end, int)
            for frame in dict.fromkeys((start, end)):
                base_id = f"{self.production_id}-{ctx.spec.id}-{shot.shot_id}-gate-{frame}"[:96]
                job_id = ds.fresh_job_id(config, f"{base_id}-{recipe.content_hash[7:19]}")
                envelope = ds.envelope(
                    job_id, recipe, config, opts.now(), frame_end=frame, frame_start=frame
                )
                job_file = ds.sign_to_shared(envelope, self.key, config.shared)
                out = config.out_dir(job_id)
                job = ds.blender_job(envelope, job_file, out, config, Tier.FINAL, assets_dir)
                queue = JobQueue(
                    [ds.executor(self.client, out, config, opts, assets_dir)], now=opts.now
                )
                result = queue.as_runner(_const_job(job), sleep=opts.sleep)(ctx)
                usage = _add(usage, result.usage)
                _scene, frames = ds.render_record(out)
                still = out / f"frame_{frame:04d}.png"
                if still.name not in frames:
                    raise RuntimeError(f"gate job {job_id} rendered no {still.name}")
                stills[f"{name}:{frame}"] = str(still)
        return stills, usage

    def _result(
        self,
        ctx: RunContext,
        done: list[tuple[RenderedShot, Usage]],
        usage: Usage,
        scene_builder: list[SceneBuilderShot] | None = None,
    ) -> RunResult:
        total = usage
        for _shot, used in done:
            total = _add(total, used)
        dept = self.departments[ctx.spec.department]
        output = RenderedShots(
            shots=[s for s, _u in done], scene_builder=list(scene_builder or [])
        ).model_dump(mode="json")
        return RunResult(output=dept.validate_output(ctx.spec.id, output), usage=total)


def _const_job(job: Job) -> Callable[[RunContext], Job]:
    return lambda _ctx: job


def _restage(ctx: RunContext, prior: RenderedShot, base: Recipe) -> dict[str, Path]:
    """The capture files a device shot's recipe pins, found by sha256 in the UI captures."""
    captures = _captures(ctx)
    if captures is None:
        raise ValueError(
            f"shot {prior.shot_id!r} shows capture {prior.capture!r} but node "
            f"{ctx.spec.id!r} has no {UI_CAPTURE} input"
        )
    by_sha = {f.sha256: Path(f.path) for c in captures.captures for f in c.clip}
    for c in captures.captures:
        if c.screen is not None:
            by_sha.update({f.sha256: Path(f.path) for f in c.screen.clip})
    extra: dict[str, Path] = {}
    for rel, sha in ds.referenced_assets(base).items():
        if rel.startswith("captures/"):
            if sha not in by_sha:
                raise ValueError(f"shot {prior.shot_id!r}: capture frame {rel} is not captured")
            extra[rel] = by_sha[sha]
    extra.update(model_assets(base))
    return extra


def model_assets(recipe: Recipe) -> dict[str, Path]:
    """The kit model glbs ``recipe`` pins (``devices/<sha256>.glb``), fetched and
    verified into the shared assets cache by :func:`ensure_device_asset`."""
    out: dict[str, Path] = {}
    for rel, sha in ds.referenced_assets(recipe).items():
        if not rel.startswith("devices/"):
            continue
        entry = by_sha256(sha)
        if entry is None:
            raise ValueError(f"device model {rel} is not in the device kit")
        out[rel] = ensure_device_asset(entry)
    return out


def _add(a: Usage, b: Usage) -> Usage:
    ai = dict(a.ai_usage)
    for metric, amount in b.ai_usage.items():
        ai[metric] = ai.get(metric, 0) + amount
    return Usage(
        gpu_minutes=a.gpu_minutes + b.gpu_minutes,
        ai_usage=ai,
        elevenlabs_characters=a.elevenlabs_characters + b.elevenlabs_characters,
    )


def _path_problems(shot: RenderedShot) -> list[str]:
    """A camera-path shot's gate and twitch problems: each cut's first and last 16 spp
    gate still must exist and not be blank, and the rendered sequence must not twitch
    (an isolated frame-to-frame PSNR drop that is not at a cut start)."""
    problems: list[str] = []
    stills = shot.gate_stills or {}
    starts: list[int] = []
    for cut in shot.cuts or []:
        start, end = cut.get("start"), cut.get("end")
        if not isinstance(start, int) or not isinstance(end, int):
            problems.append(f"cut {cut.get('name')!r} has no frame range")
            continue
        starts.append(start)
        for frame in dict.fromkeys((start, end)):
            key = f"{cut.get('name')}:{frame}"
            still = stills.get(key)
            if still is None or not Path(still).is_file():
                problems.append(f"gate still {key} is missing")
            elif is_blank(Path(still)):
                problems.append(f"gate still {key} is blank")
    out = Path(shot.output_dir)
    twitches = ch.twitch_frames([out / name for name in sorted(shot.frames)], starts)
    if twitches:
        problems.append("twitch (isolated PSNR drop) at frame " + ", ".join(map(str, twitches)))
    return problems


def shot_critic(pass_score: float, inner: Critic | None = None) -> Critic:
    """The vision-QA gate for shot nodes: fail any shot whose QA failed or scored low.

    A shot whose first frame is blank (luma stddev below the scene verifier's
    ``BLANK_STDDEV``) fails too, whatever its score; a camera-path shot (``cuts``) fails
    a missing or blank preview-gate still and a twitching sequence (#369); a shot node
    with only boards pending for the scene builder (nothing rendered to review) fails. Other kinds,
    and shot nodes whose output is not a rendered-shots record (a custom
    runner built them, so there are no frames to review), go to ``inner``, or pass
    when there is none.
    """

    def review(spec: NodeSpec, output: JsonValue) -> CriticVerdict:
        shots: list[RenderedShot] | None = None
        if spec.kind in (TEMPLATE_SHOTS, SHOTS):
            try:
                shots = RenderedShots.model_validate(output).shots
            except ValidationError:
                shots = None
        if shots is None:
            return inner(spec, output) if inner is not None else CriticVerdict(passed=True)
        if not shots:
            return CriticVerdict(
                passed=False,
                comment="no rendered shots: every board is pending for the scene builder",
                findings=[
                    Finding(
                        reviewer="vision-qa",
                        severity=Severity.HIGH,
                        text="no shot was rendered; boards are only pending for the scene builder",
                        confirmed=True,
                    )
                ],
            )
        findings: list[Finding] = []
        for shot in shots:
            problems: list[str] = []
            if shot.frames:
                first = Path(shot.output_dir) / sorted(shot.frames)[0]
                if first.is_file() and is_blank(first):
                    problems.append("first frame is blank")
            if not shot.qa.passed:
                problems.append("render QA failed: " + (", ".join(shot.qa.failures) or "unknown"))
            if shot.cuts is not None:
                problems += _path_problems(shot)
            if shot.vision_qa.score < pass_score:
                problems.append(
                    f"vision QA {shot.vision_qa.score:.2f} < {pass_score:.2f}"
                    + (f" ({shot.vision_qa.notes})" if shot.vision_qa.notes else "")
                )
            if problems:
                findings.append(
                    Finding(
                        reviewer="vision-qa",
                        severity=Severity.HIGH,
                        text=f"shot {shot.shot_id}: " + "; ".join(problems),
                        confirmed=True,
                        frame=0,
                    )
                )
        scores = {"vision_qa": min(s.vision_qa.score for s in shots)}
        comment = "; ".join(f.text for f in findings)
        return CriticVerdict(
            passed=not findings, scores=scores, comment=comment[:4000], findings=findings
        )

    return review
