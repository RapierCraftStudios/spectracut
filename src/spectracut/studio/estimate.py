# SPDX-License-Identifier: AGPL-3.0-or-later
"""A first production estimate from the quality profile (M1).

:func:`breakdown` turns a brief into one node per profile stage, each reading
the stage before it. :func:`estimate` adds up what the graph may cost before it
runs: the department runs each stage may take (1 plus its revision budget when
a Critic or person reviews it), the cost each known department declares, and
the human gates. The Producer (#53) replaces the breakdown with a real shot and
asset list and extends the estimate; both keep these entry points.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import Field

from spectracut.spec._base import Slug, StrictModel
from spectracut.spec.brief import Brief, CreativeBrief
from spectracut.studio.department import CostEstimate, Department
from spectracut.studio.gates import GateMode
from spectracut.studio.graph import ProductionGraph
from spectracut.studio.node import NodeSpec, Tier
from spectracut.studio.profiles import QualityProfile

BRIEF_PARAM = "brief"
"""Param of the first node that holds the brief as JSON."""


class StageEstimate(StrictModel):
    """Expected cost of one node: at most ``max_runs`` department runs."""

    node_id: Slug
    stage: Slug
    department: Slug
    tier: Tier
    gate: GateMode
    max_runs: int = Field(ge=1)
    cost: CostEstimate = CostEstimate()
    """Department cost times ``max_runs``; zero when the department is unknown."""
    known: bool = False
    """Whether the department, and so its cost, is known."""


class ProductionEstimate(StrictModel):
    """Upper bounds for a whole production, before it runs."""

    production_id: Slug
    profile: Slug | None = None
    stages: tuple[StageEstimate, ...] = ()
    max_runs: int = Field(default=0, ge=0)
    human_gates: int = Field(default=0, ge=0)
    total: CostEstimate = CostEstimate()
    unknown_departments: tuple[Slug, ...] = ()

    def lines(self) -> list[str]:
        """The estimate as text, one stage per line, then the totals."""
        out = [f"estimate for {self.production_id} (profile {self.profile or 'none'}):"]
        for s in self.stages:
            cost = _cost_text(s.cost) if s.known else "cost unknown"
            out.append(
                f"  {s.node_id}: department={s.department} tier={s.tier.value} "
                f"gate={s.gate.value} runs<={s.max_runs} {cost}"
            )
        out.append(
            f"total: {len(self.stages)} stages, runs<={self.max_runs}, "
            f"human gates={self.human_gates}, {_cost_text(self.total)}"
        )
        if self.unknown_departments:
            out.append(f"cost unknown for departments: {', '.join(self.unknown_departments)}")
        return out


def _cost_text(c: CostEstimate) -> str:
    return (
        f"ai_requests={c.ai_requests} ai_input_tokens={c.ai_input_tokens} "
        f"gpu_minutes={c.gpu_minutes:g} elevenlabs_characters={c.elevenlabs_characters}"
    )


def breakdown(brief: Brief | CreativeBrief, profile: QualityProfile) -> list[NodeSpec]:
    """One node per profile stage (id = kind = department = stage), chained in order."""
    specs: list[NodeSpec] = []
    previous: str | None = None
    for stage in profile.stages:
        params = {BRIEF_PARAM: brief.model_dump(mode="json")} if previous is None else {}
        specs.append(
            NodeSpec(
                id=stage,
                kind=stage,
                department=stage,
                inputs=(previous,) if previous is not None else (),
                params=params,
            )
        )
        previous = stage
    return specs


def estimate(
    graph: ProductionGraph, departments: Mapping[str, type[Department]] | None = None
) -> ProductionEstimate:
    """Upper-bound cost of building every node of ``graph`` once, with revisions."""
    departments = departments or {}
    stages: list[StageEstimate] = []
    total = CostEstimate()
    unknown: list[str] = []
    for nid in graph.order():
        spec = graph.spec(nid)
        gate = graph.gate_for(spec)
        runs = 1 + (graph.revision_budget(spec) if gate is not GateMode.AUTO else 0)
        dept = departments.get(spec.department)
        cost = CostEstimate()
        if dept is not None:
            c = dept.cost
            cost = CostEstimate(
                ai_requests=c.ai_requests * runs,
                ai_input_tokens=c.ai_input_tokens * runs,
                gpu_minutes=c.gpu_minutes * runs,
                elevenlabs_characters=c.elevenlabs_characters * runs,
            )
            total = CostEstimate(
                ai_requests=total.ai_requests + cost.ai_requests,
                ai_input_tokens=total.ai_input_tokens + cost.ai_input_tokens,
                gpu_minutes=total.gpu_minutes + cost.gpu_minutes,
                elevenlabs_characters=total.elevenlabs_characters + cost.elevenlabs_characters,
            )
        elif spec.department not in unknown:
            unknown.append(spec.department)
        stages.append(
            StageEstimate(
                node_id=nid,
                stage=spec.kind,
                department=spec.department,
                tier=graph.tier_for(spec),
                gate=gate,
                max_runs=runs,
                cost=cost,
                known=dept is not None,
            )
        )
    return ProductionEstimate(
        production_id=graph.production_id,
        profile=graph.profile.name if graph.profile is not None else None,
        stages=tuple(stages),
        max_runs=sum(s.max_runs for s in stages),
        human_gates=sum(1 for s in stages if s.gate is GateMode.HUMAN),
        total=total,
        unknown_departments=tuple(unknown),
    )
