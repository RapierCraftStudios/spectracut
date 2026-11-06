# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shots: storyboard boards rendered on the farm from the shot template library.

- ``template-shots`` (node kind ``template_shots``): a :class:`ShotPlan` picks a
  template (devices, hero object, end card, typography), camera variant, aspect and
  slots for every storyboard board. The plan's schema enumerates the registered
  template ids; a board that fits no template sets ``scene_builder`` (a shot
  description) instead and is recorded, not rendered, for the scene builder. The
  :class:`ShotRunner` builds each template board's recipe with
  ``TEMPLATES[t].preview(...)`` from the node's brand kit, stages the kit's assets,
  signs the job to shared storage and renders it as one Blender job
  through ``JobQueue`` + :class:`~spectracut.queue.executors.flamenco.FlamencoExecutor`,
  pinned to a registered GPU host by the config's worker tag.
- ``shots`` (kind ``shots``): re-renders every template shot's recipe at the node's
  tier (``device_shot.tier_steps``).

Every rendered shot records its frames' sha256s, render QA and a vision-QA score of
its first frame; GPU minutes and ``vision_qa_*`` AI usage go to the node's ledger
entry. The node output is content-hashed by the graph, so a re-run with nothing
changed is a cache hit and submits no jobs. :func:`shot_critic` is the Critic gate on
those records. :func:`shot_nodes` declares the two nodes for the Producer (#262) to
compose; the control plane only builds declarative recipes and never imports ``bpy``.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePosixPath
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

from spectracut.edit.timeline import Edit
from spectracut.finishing.color import ColorPipeline
from spectracut.queue.queue import JobQueue
from spectracut.spec._base import Slug, StrictModel
from spectracut.spec.brand import BrandKit
from spectracut.spec.recipe import Recipe, RecipeStep
from spectracut.storyboard.models import Storyboard
from spectracut.studio import device_shot as ds
from spectracut.studio.department import SKILLS_ROOT, CostEstimate, Department
from spectracut.studio.departments.art import ANIMATIC, STORYBOARD
from spectracut.studio.estimate import STAGE_RATES
from spectracut.studio.gates import Critic, CriticVerdict, Finding, Severity
from spectracut.studio.graph import RunContext, Runner, RunResult
from spectracut.studio.ledger import Usage
from spectracut.studio.node import NodeSpec, Tier
from spectracut.templates import TEMPLATES
from spectracut.templates.base import RESOLUTIONS, Template, TemplateAspect, require_clean_copy

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from spectracut.providers.base import VisionJudge
    from spectracut.qa.report import QAReportStore
    from spectracut.queue.executors.flamenco import FlamencoApi

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

ShotPlanEntry: Any = Annotated[
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

    shots: list[ShotPlanEntry] = Field(min_length=1)


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
    inputs: ClassVar[dict[str, type[BaseModel]]] = {STORYBOARD: Storyboard, ANIMATIC: Edit}
    output = RenderedShots
    skill_pack = SKILLS_ROOT / "motion-designer"
    uses_gpu = True
    cost = _rate_cost(TEMPLATE_SHOTS)


class ShotsDepartment(Department):
    """Every template shot re-rendered at the node's tier."""

    name = "shots"
    produces = frozenset({SHOTS})
    inputs: ClassVar[dict[str, type[BaseModel]]] = {TEMPLATE_SHOTS: RenderedShots}
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


def tier_recipe(base: Recipe, tier: Tier, aspect: TemplateAspect = "16:9") -> Recipe:
    """``base`` plus ``device_shot`` tier steps at ``tier`` (at ``aspect``), to PNG."""
    width, height = RESOLUTIONS[aspect]
    out: list[RecipeStep] = list(base.steps)
    for step in ds.tier_steps(_render_tier(tier), start=len(out)):
        if step.tool == "render.set_resolution":
            step = step.model_copy(update={"args": {**step.args, "width": width, "height": height}})
        out.append(step)
    out += ColorPipeline().render_steps(start=len(out))
    out.append(RecipeStep(index=len(out), tool="render.set_output", args={"format": "PNG"}))
    return base.model_copy(update={"steps": out})


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
    """Wraps the text runner for the shot nodes; other kinds pass through to ``inner``."""

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
    ) -> None:
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
        for shot in shots:
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
        return self._result(ctx, done, usage, pending)

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
    ) -> tuple[RenderedShot, Usage]:
        """Render one shot as one signed Blender job pinned to the config's worker tag."""
        config, opts = self.config, self.options
        shot_id = shot.shot_id
        if shot.template is None:
            raise ValueError(f"shot {shot_id!r} has no template to render")
        assets_dir, _sha = ds.stage_for(config, _assets(ctx.spec), recipe)
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
        verdict = judge.score(out / sorted(frames)[0], ds.VISION_RUBRIC)
        usage = ds.vision_qa_usage(result.usage, judge)
        failures = qa.get("failures")
        rendered = RenderedShot(
            shot_id=shot_id,
            template=shot.template,
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
        )
        return rendered, usage

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


def _add(a: Usage, b: Usage) -> Usage:
    ai = dict(a.ai_usage)
    for metric, amount in b.ai_usage.items():
        ai[metric] = ai.get(metric, 0) + amount
    return Usage(
        gpu_minutes=a.gpu_minutes + b.gpu_minutes,
        ai_usage=ai,
        elevenlabs_characters=a.elevenlabs_characters + b.elevenlabs_characters,
    )


def shot_critic(pass_score: float, inner: Critic | None = None) -> Critic:
    """The vision-QA gate for shot nodes: fail any shot whose QA failed or scored low.

    Other kinds, and shot nodes whose output is not a rendered-shots record (a custom
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
            return CriticVerdict(passed=True, comment="no rendered shots (scene builder only)")
        findings: list[Finding] = []
        for shot in shots:
            problems: list[str] = []
            if not shot.qa.passed:
                problems.append("render QA failed: " + (", ".join(shot.qa.failures) or "unknown"))
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
