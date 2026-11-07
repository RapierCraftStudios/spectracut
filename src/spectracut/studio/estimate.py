# SPDX-License-Identifier: AGPL-3.0-or-later
"""Production estimates: what a graph may cost before it runs.

:func:`breakdown` turns a brief into one node per profile stage, wired by the
inputs each stage's department declares (the stage before it when no department
is registered yet); the Producer (:mod:`spectracut.studio.producer`) adds the
shot and asset list as the first node's ``plan`` param. :func:`estimate` adds up
the upper-bound cost of building every node once with its revisions: the
department runs each stage may take (1 plus its revision budget when a Critic or
person reviews it), the cost each registered department declares, or else the
default rate of the stage (:data:`STAGE_RATES`) scaled by the film's duration
and shot count, plus GPU hours, AI usage, ElevenLabs characters, the wall time
along the critical path and the human gates.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from pydantic import Field, JsonValue

from spectracut.spec._base import Slug, StrictModel
from spectracut.spec.brief import Brief, CreativeBrief
from spectracut.studio.department import CostEstimate, Department
from spectracut.studio.departments import department_for
from spectracut.studio.gates import GateMode
from spectracut.studio.graph import ProductionGraph
from spectracut.studio.node import NodeSpec, Tier
from spectracut.studio.profiles import QualityProfile

if TYPE_CHECKING:
    from spectracut.studio.producer import ProductionPlan

BRIEF_PARAM = "brief"
"""Param of the first node that holds the brief as JSON."""

PLAN_PARAM = "plan"
"""Param of the first node that holds the Producer's plan (duration, shots, assets) as JSON."""


class StageRate(StrictModel):
    """Default cost of one run of a stage whose department declares none."""

    ai_requests: int = Field(default=1, ge=0)
    ai_input_tokens: int = Field(default=8_000, ge=0)
    gpu_minutes: float = Field(default=0.0, ge=0)
    """GPU minutes per run at the ``final`` tier."""
    gpu_minutes_per_shot: float = Field(default=0.0, ge=0)
    gpu_minutes_per_second: float = Field(default=0.0, ge=0)
    """GPU minutes per second of film."""
    elevenlabs_characters_per_second: float = Field(default=0.0, ge=0)
    """Voice characters per second of film when the script's VO is not known."""
    wall_minutes: float = Field(default=2.0, ge=0)
    wall_minutes_per_shot: float = Field(default=0.0, ge=0)


DEFAULT_RATE = StageRate()
"""Rate of a stage not in :data:`STAGE_RATES`: one AI request, no GPU."""

STAGE_RATES: dict[str, StageRate] = {
    "style_frames": StageRate(gpu_minutes=6.0, wall_minutes=10.0),
    "storyboard": StageRate(ai_requests=2, ai_input_tokens=16_000, wall_minutes=4.0),
    "animatic": StageRate(gpu_minutes_per_shot=0.5, wall_minutes=5.0),
    "previs": StageRate(gpu_minutes_per_shot=1.0, wall_minutes=4.0, wall_minutes_per_shot=0.5),
    "custom_assets": StageRate(gpu_minutes=20.0, wall_minutes=30.0),
    "template_shots": StageRate(gpu_minutes_per_shot=1.0, wall_minutes_per_shot=1.0),
    "shots": StageRate(gpu_minutes_per_shot=3.0, wall_minutes=2.0, wall_minutes_per_shot=2.0),
    "shot": StageRate(gpu_minutes_per_shot=3.0, wall_minutes=2.0, wall_minutes_per_shot=2.0),
    "animation_polish": StageRate(gpu_minutes_per_shot=2.0, wall_minutes_per_shot=1.5),
    "cgi_effects": StageRate(gpu_minutes_per_shot=4.0, wall_minutes_per_shot=2.0),
    "voiceover": StageRate(elevenlabs_characters_per_second=15.0, wall_minutes=3.0),
    "sound_design": StageRate(wall_minutes=4.0),
    "mix": StageRate(wall_minutes=3.0),
    "grade": StageRate(gpu_minutes_per_second=0.2, wall_minutes=5.0),
    "render": StageRate(gpu_minutes_per_second=2.0, wall_minutes=5.0),
    "render_preview": StageRate(gpu_minutes_per_second=0.5, wall_minutes=3.0),
    "render_final": StageRate(gpu_minutes_per_second=2.0, wall_minutes=5.0),
    "delivery": StageRate(ai_requests=0, ai_input_tokens=0, wall_minutes=2.0),
}
"""Default rates by stage (node kind), for stages whose department declares no cost."""

TIER_GPU_FACTOR: dict[Tier, float] = {
    Tier.PREVIEW: 0.25,
    Tier.REVIEW: 0.5,
    Tier.FINAL: 1.0,
    Tier.HERO: 2.0,
}
"""GPU time of a tier relative to ``final``."""


class StageEstimate(StrictModel):
    """Expected cost of one node: at most ``max_runs`` department runs."""

    node_id: Slug
    stage: Slug
    department: Slug
    tier: Tier
    gate: GateMode
    max_runs: int = Field(ge=1)
    cost: CostEstimate = CostEstimate()
    """Cost of one run times ``max_runs``."""
    wall_minutes: float = Field(default=0.0, ge=0)
    """Working time of ``max_runs`` runs, not counting time waiting on people."""
    known: bool = False
    """Whether the department, and so its declared cost, is known (else default rates)."""


class ProductionEstimate(StrictModel):
    """Upper bounds for a whole production, before it runs."""

    production_id: Slug
    profile: Slug | None = None
    duration_s: float | None = None
    shots: int = Field(default=0, ge=0)
    stages: tuple[StageEstimate, ...] = ()
    max_runs: int = Field(default=0, ge=0)
    human_gates: int = Field(default=0, ge=0)
    total: CostEstimate = CostEstimate()
    gpu_hours: float = Field(default=0.0, ge=0)
    wall_minutes: float = Field(default=0.0, ge=0)
    """Working time along the longest dependency chain, not counting human gates."""
    unknown_departments: tuple[Slug, ...] = ()

    def stage(self, node_id: str) -> StageEstimate:
        for s in self.stages:
            if s.node_id == node_id:
                return s
        raise KeyError(f"no estimate for node {node_id!r}")

    def lines(self) -> list[str]:
        """The estimate as text, one stage per line, then the totals."""
        film = f"{self.duration_s:g} s, {self.shots} shots, " if self.duration_s else ""
        out = [f"estimate for {self.production_id} ({film}profile {self.profile or 'none'}):"]
        for s in self.stages:
            rates = "" if s.known else " (default rates)"
            out.append(
                f"  {s.node_id}: department={s.department} tier={s.tier.value} "
                f"gate={s.gate.value} runs<={s.max_runs} {_cost_text(s.cost)} "
                f"wall_minutes={s.wall_minutes:g}{rates}"
            )
        out.append(
            f"total: {len(self.stages)} stages, runs<={self.max_runs}, "
            f"human gates={self.human_gates}, gpu_hours={self.gpu_hours:.2f}, "
            f"wall_minutes={self.wall_minutes:g}, {_cost_text(self.total)}"
        )
        if self.unknown_departments:
            out.append("default rates used for departments: " + ", ".join(self.unknown_departments))
        return out


def _cost_text(c: CostEstimate) -> str:
    return (
        f"ai_requests={c.ai_requests} ai_input_tokens={c.ai_input_tokens} "
        f"gpu_minutes={c.gpu_minutes:g} elevenlabs_characters={c.elevenlabs_characters}"
    )


def breakdown(
    brief: Brief | CreativeBrief,
    profile: QualityProfile,
    *,
    seeds: Sequence[NodeSpec] = (),
) -> list[NodeSpec]:
    """One node per profile stage (id = kind = stage), wired as the departments declare.

    The department is :func:`~spectracut.studio.departments.department_for` the stage.
    A stage whose department is registered reads, in the order its department
    declares its ``inputs``, the earlier profile stages of those kinds: the same edges the
    departments' node builders (``discovery_nodes``, ``pick_concept``,
    ``preproduction_nodes``) wire. A stage with no registered department yet reads
    the stage before it. The first node carries the brief.

    ``seeds`` are extra nodes outside the profile (id = kind, e.g. the ``brand_kit``
    node from :func:`~spectracut.studio.departments.art.brand_kit_node`). Each goes
    right after the first profile stage, and its kind counts as an earlier kind for
    every later stage, so departments that declare it read it. With no seeds the
    nodes are exactly the profile's.
    """
    from spectracut.studio.departments import all_departments

    registry = all_departments()
    earlier: set[str] = set()
    specs: list[NodeSpec] = []
    previous: str | None = None
    for stage in profile.stages:
        params = {BRIEF_PARAM: brief.model_dump(mode="json")} if previous is None else {}
        name = department_for(stage)
        dept = registry.get(name)
        if dept is not None:
            # A kind read only alongside another (``wired_with``, e.g. the editor reads
            # the storyboard only for the references' craft citations) needs that too.
            needs: Mapping[str, str] = getattr(dept, "wired_with", {})
            reads = [k for k in dept.inputs if k in earlier and needs.get(k, k) in earlier]
        else:
            reads = [previous] if previous is not None else []
        specs.append(
            NodeSpec(id=stage, kind=stage, department=name, inputs=tuple(reads), params=params)
        )
        if previous is None:
            for seed in seeds:
                specs.append(seed)
                earlier.add(seed.kind)
        previous = stage
        earlier.add(stage)
    return specs


def plan_of(graph: ProductionGraph) -> ProductionPlan | None:
    """The Producer's plan stored on the graph (first node carrying :data:`PLAN_PARAM`)."""
    from spectracut.studio.producer import ProductionPlan

    for nid in graph.order():
        raw: JsonValue = graph.spec(nid).params.get(PLAN_PARAM)
        if raw is not None:
            return ProductionPlan.model_validate(raw)
    return None


def _default_cost(
    rate: StageRate, tier: Tier, duration_s: float, shots: int, vo_characters: int | None
) -> tuple[CostEstimate, float]:
    gpu = rate.gpu_minutes + rate.gpu_minutes_per_shot * shots
    gpu += rate.gpu_minutes_per_second * duration_s
    chars = 0
    if rate.elevenlabs_characters_per_second:
        chars = (
            vo_characters
            if vo_characters is not None
            else math.ceil(rate.elevenlabs_characters_per_second * duration_s)
        )
    cost = CostEstimate(
        ai_requests=rate.ai_requests,
        ai_input_tokens=rate.ai_input_tokens,
        gpu_minutes=round(gpu * TIER_GPU_FACTOR[tier], 3),
        elevenlabs_characters=chars,
    )
    return cost, rate.wall_minutes + rate.wall_minutes_per_shot * shots


def _times(c: CostEstimate, n: int) -> CostEstimate:
    return CostEstimate(
        ai_requests=c.ai_requests * n,
        ai_input_tokens=c.ai_input_tokens * n,
        gpu_minutes=c.gpu_minutes * n,
        elevenlabs_characters=c.elevenlabs_characters * n,
    )


def _plus(a: CostEstimate, b: CostEstimate) -> CostEstimate:
    return CostEstimate(
        ai_requests=a.ai_requests + b.ai_requests,
        ai_input_tokens=a.ai_input_tokens + b.ai_input_tokens,
        gpu_minutes=a.gpu_minutes + b.gpu_minutes,
        elevenlabs_characters=a.elevenlabs_characters + b.elevenlabs_characters,
    )


def estimate(
    graph: ProductionGraph,
    departments: Mapping[str, type[Department]] | None = None,
    *,
    plan: ProductionPlan | None = None,
) -> ProductionEstimate:
    """Upper-bound cost of building every node of ``graph`` once, with revisions.

    ``plan`` defaults to the plan stored on the graph; without one, duration and
    shot-scaled rates count as zero.
    """
    departments = departments or {}
    if plan is None:
        plan = plan_of(graph)
    duration = plan.duration_s if plan is not None else 0.0
    shots = len(plan.shots) if plan is not None else 0
    vo_characters = plan.vo_characters if plan is not None else None
    stages: list[StageEstimate] = []
    total = CostEstimate()
    unknown: list[str] = []
    finish: dict[str, float] = {}
    for nid in graph.order():
        spec = graph.spec(nid)
        gate = graph.gate_for(spec)
        tier = graph.tier_for(spec)
        runs = 1 + (graph.revision_budget(spec) if gate is not GateMode.AUTO else 0)
        dept = departments.get(spec.department)
        rate = STAGE_RATES.get(spec.kind, DEFAULT_RATE)
        one, wall = _default_cost(rate, tier, duration, shots, vo_characters)
        if dept is not None:
            # Keep the plan-scaled ElevenLabs projection: a department's static cost
            # cannot know how many VO characters this film's script has.
            one = dept.cost.model_copy(
                update={
                    "elevenlabs_characters": max(
                        dept.cost.elevenlabs_characters, one.elevenlabs_characters
                    )
                }
            )
        elif spec.department not in unknown:
            unknown.append(spec.department)
        cost = _times(one, runs)
        total = _plus(total, cost)
        wall_total = wall * runs
        finish[nid] = max((finish[i] for i in spec.inputs), default=0.0) + wall_total
        stages.append(
            StageEstimate(
                node_id=nid,
                stage=spec.kind,
                department=spec.department,
                tier=tier,
                gate=gate,
                max_runs=runs,
                cost=cost,
                wall_minutes=wall_total,
                known=dept is not None,
            )
        )
    return ProductionEstimate(
        production_id=graph.production_id,
        profile=graph.profile.name if graph.profile is not None else None,
        duration_s=plan.duration_s if plan is not None else None,
        shots=shots,
        stages=tuple(stages),
        max_runs=sum(s.max_runs for s in stages),
        human_gates=sum(1 for s in stages if s.gate is GateMode.HUMAN),
        total=total,
        gpu_hours=round(total.gpu_minutes / 60.0, 4),
        wall_minutes=max(finish.values(), default=0.0),
        unknown_departments=tuple(unknown),
    )
