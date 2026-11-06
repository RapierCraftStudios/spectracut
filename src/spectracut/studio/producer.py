# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Producer: breakdown into a production graph, scheduling, estimates and budget caps.

- :meth:`Producer.breakdown` turns a brief (and, when there is one, a script
  :class:`~spectracut.spec.spec.Spec`) into a :class:`ProductionPlan`: the shot
  list, the asset list, and one department task per stage of the quality
  profile, gated as the profile says. :meth:`Producer.new_graph` builds the
  production graph from it. The tasks are wired as each department declares its
  inputs; :func:`edge_problems` lists the edges of a graph that do not agree
  (e.g. a production created before the departments declared them).
- :meth:`Producer.add_step` and :meth:`Producer.drop_step` change the steps of a
  production; both rewire the neighbouring nodes, mark exactly the affected
  dependants dirty and return the new estimate.
- :meth:`Producer.run` enforces :class:`BudgetCaps`: before a node builds, its
  estimated cost plus what the production has used (and what this run has
  already let through) is checked against each cap. A node that would exceed a
  cap is held, not run; it stays pending, its dependants wait, and the report
  asks for approval (:attr:`ProducerReport.needs_approval`).
- :class:`ScheduledRunner` schedules tasks against GPU leases and AI capacity:
  a task of a GPU department waits for a lease (``waiting_for_gpu``), and a task
  whose provider is out of capacity (``ProviderExhausted``) waits in
  ``waiting_for_ai`` until the reset time and then retries, instead of failing.

The Producer runs in the control plane: it never touches Blender or the network.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import Field, JsonValue

from spectracut.providers.base import ProviderExhausted
from spectracut.spec._base import Slug, StrictModel
from spectracut.spec.brief import Brief, CreativeBrief
from spectracut.spec.spec import Spec
from spectracut.studio.department import CostEstimate, Department
from spectracut.studio.departments import department_for
from spectracut.studio.estimate import (
    BRIEF_PARAM,
    PLAN_PARAM,
    ProductionEstimate,
    breakdown,
    estimate,
)
from spectracut.studio.gates import Critic, GateMode
from spectracut.studio.graph import ProductionGraph, RunContext, Runner, RunReport, RunResult
from spectracut.studio.node import NodeSpec, Tier
from spectracut.studio.profiles import QualityProfile
from spectracut.studio.runner import CapacityLimits
from spectracut.studio.supervisor import Supervisor, SupervisorHold

AVG_SHOT_S = 4.0
"""Average shot length used to plan a shot list when there is no script yet."""

_CARRIED = (BRIEF_PARAM, PLAN_PARAM)
"""Params of the first node that move to its dependants when it is dropped."""


class PlannedShot(StrictModel):
    id: Slug
    duration_s: float = Field(gt=0)
    template_id: Slug | None = None


class PlannedAsset(StrictModel):
    id: Slug
    kind: Slug
    source: str
    """Where the asset comes from, for provenance (e.g. ``brief:product``)."""


class ProductionPlan(StrictModel):
    """The breakdown: film length, shot list, asset list and department tasks."""

    duration_s: float = Field(gt=0)
    shots: tuple[PlannedShot, ...] = Field(min_length=1)
    assets: tuple[PlannedAsset, ...] = ()
    vo_characters: int | None = Field(default=None, ge=0)
    """Characters of the script's VO, when the script is known."""
    nodes: tuple[NodeSpec, ...] = ()
    """One department task per profile stage, in production order."""
    gates: dict[Slug, GateMode] = Field(default_factory=dict)
    """Gate of each task, by node id."""

    def figures(self) -> dict[str, JsonValue]:
        """The plan as stored on the graph: everything but the tasks and gates."""
        return self.model_dump(mode="json", exclude={"nodes", "gates"})


class BudgetCaps(StrictModel):
    """Hard caps on a production's usage; ``None`` means no cap."""

    elevenlabs_characters: int | None = Field(default=None, ge=0)
    gpu_minutes: float | None = Field(default=None, ge=0)
    ai_requests: int | None = Field(default=None, ge=0)

    def limits(self) -> dict[str, float]:
        return {k: float(v) for k, v in self.model_dump().items() if v is not None}


class BudgetHold(StrictModel):
    """A node held because building it could exceed a cap."""

    node_id: Slug
    metric: Slug
    projected: float
    limit: float

    def text(self) -> str:
        return f"{self.node_id} {self.metric} projected={self.projected:g} limit={self.limit:g}"


@dataclass
class ProducerReport:
    run: RunReport
    estimate: ProductionEstimate
    needs_approval: list[BudgetHold] = field(default_factory=list)
    supervisor_hold: SupervisorHold | None = None
    """The tripped breaker, when the supervisor stopped new admissions."""


def edge_problems(
    graph: ProductionGraph | Iterable[NodeSpec], departments: Mapping[str, type[Department]]
) -> list[str]:
    """Every input whose kind its node's department does not declare, as text.

    Nodes whose department is not in ``departments`` are skipped (they fail on
    their own when run). An empty list means the graph's edges agree with the
    departments' declared ``inputs``.
    """
    specs = (
        [graph.spec(n) for n in graph.order()]
        if isinstance(graph, ProductionGraph)
        else list(graph)
    )
    kinds = {s.id: s.kind for s in specs}
    problems: list[str] = []
    for spec in specs:
        dept = departments.get(spec.department)
        if dept is None:
            continue
        for input_id in spec.inputs:
            kind = kinds.get(input_id, input_id)
            if kind not in dept.inputs:
                problems.append(
                    f"node {spec.id!r} reads {input_id!r} of kind {kind!r}, "
                    f"which department {dept.name!r} does not declare"
                )
    return problems


def _metric(c: CostEstimate, name: str) -> float:
    return float(getattr(c, name))


class Producer:
    """Plans, re-plans and runs productions within their budget."""

    def __init__(self, departments: Mapping[str, type[Department]] | None = None) -> None:
        self.departments: dict[str, type[Department]] = dict(departments or {})

    # ---- breakdown ---------------------------------------------------------

    def breakdown(
        self,
        brief: Brief | CreativeBrief,
        profile: QualityProfile,
        *,
        duration_s: float | None = None,
        script: Spec | None = None,
    ) -> ProductionPlan:
        """Shot list, asset list and one task per profile stage for ``brief``.

        The length comes from ``script.length_s`` or ``duration_s`` (one is
        required); the shot list from the script's shots, or evenly split
        :data:`AVG_SHOT_S` shots.
        """
        if script is not None:
            length = script.length_s
            beats = sum(s.duration_beats for s in script.shots)
            shots = tuple(
                PlannedShot(
                    id=s.id,
                    duration_s=round(length * s.duration_beats / beats, 3),
                    template_id=s.template_id,
                )
                for s in script.shots
            )
            vo: int | None = len(script.vo) if script.vo else None
        elif duration_s is not None and duration_s > 0:
            length = float(duration_s)
            count = max(1, math.ceil(length / AVG_SHOT_S))
            each = round(length / count, 3)
            shots = tuple(
                PlannedShot(id=f"shot-{i:02d}", duration_s=each) for i in range(1, count + 1)
            )
            vo = None
        else:
            raise ValueError("a breakdown needs a script or a positive duration_s")
        plan = ProductionPlan(
            duration_s=length,
            shots=shots,
            assets=_assets(brief, profile),
            vo_characters=vo,
        )
        nodes = breakdown(brief, profile)
        nodes[0] = nodes[0].model_copy(
            update={"params": {**nodes[0].params, PLAN_PARAM: plan.figures()}}
        )
        gates = {n.id: profile.gates.get(n.kind, GateMode.AUTO) for n in nodes}
        return plan.model_copy(update={"nodes": tuple(nodes), "gates": gates})

    def new_graph(
        self, production_id: str, plan: ProductionPlan, profile: QualityProfile
    ) -> ProductionGraph:
        graph = ProductionGraph(production_id, profile=profile)
        graph.add_nodes(plan.nodes)
        return graph

    def estimate(self, graph: ProductionGraph) -> ProductionEstimate:
        return estimate(graph, self.departments)

    # ---- adding and dropping steps -----------------------------------------

    def add_step(
        self,
        graph: ProductionGraph,
        stage: str,
        *,
        after: str | None,
        department: str | None = None,
        tier: Tier | None = None,
        gate: GateMode | None = None,
        revision_budget: int = 0,
    ) -> ProductionEstimate:
        """Insert ``stage`` after node ``after``; the nodes that read ``after`` read it instead."""
        if after is not None:
            graph.spec(after)
        readers = [graph.spec(n) for n in graph.order() if after in graph.spec(n).inputs]
        if after is None:
            readers = []
        profile = graph.profile
        if profile is not None:
            stages = list(profile.stages)
            at = stages.index(graph.spec(after).kind) + 1 if after is not None else 0
            if after is not None and graph.spec(after).kind not in stages:
                at = len(stages)
            stages.insert(at, stage)
            data = profile.model_dump(mode="json")
            data["stages"] = stages
            data["revision_budget"][stage] = revision_budget
            data["tier"][stage] = (tier or Tier.PREVIEW).value
            if gate is not None:
                data["gates"][stage] = gate.value
            profile = QualityProfile.model_validate(data)
        graph.add_node(
            NodeSpec(
                id=stage,
                kind=stage,
                department=department or department_for(stage),
                inputs=(after,) if after is not None else (),
                tier=tier if profile is None else None,
            )
        )
        self._set_profile(graph, profile, stage, gate)
        for reader in readers:
            inputs = tuple(stage if i == after else i for i in reader.inputs)
            graph.update_node(reader.model_copy(update={"inputs": inputs}))
        return self.estimate(graph)

    def drop_step(self, graph: ProductionGraph, stage: str) -> ProductionEstimate:
        """Remove node ``stage``; the nodes that read it read its inputs instead."""
        dropped = graph.spec(stage)
        carried = {k: v for k, v in dropped.params.items() if k in _CARRIED}
        for nid in graph.order():
            reader = graph.spec(nid)
            if stage not in reader.inputs:
                continue
            inputs: list[str] = []
            for i in reader.inputs:
                for j in dropped.inputs if i == stage else (i,):
                    if j not in inputs:
                        inputs.append(j)
            params = dict(reader.params)
            if not inputs:
                params = {**carried, **params}
            graph.update_node(reader.model_copy(update={"inputs": tuple(inputs), "params": params}))
        graph.remove_node(stage)
        profile = graph.profile
        if profile is not None and dropped.kind in profile.stages:
            if len(profile.stages) == 1:
                raise ValueError("cannot drop the only stage of a production")
            data = profile.model_dump(mode="json")
            data["stages"] = [s for s in profile.stages if s != dropped.kind]
            for table in ("revision_budget", "tier", "gates"):
                data[table].pop(dropped.kind, None)
            for table in ("best_of_n", "verify_samples"):
                data["critique"][table].pop(dropped.kind, None)
            self._set_profile(graph, QualityProfile.model_validate(data), None, None)
        return self.estimate(graph)

    @staticmethod
    def _set_profile(
        graph: ProductionGraph,
        profile: QualityProfile | None,
        stage: str | None,
        gate: GateMode | None,
    ) -> None:
        if profile is not None:
            graph.profile = profile
            graph.gates = graph.gates.model_copy(update={"defaults": dict(profile.gates)})
        elif stage is not None and gate is not None:
            nodes = {**graph.gates.nodes, stage: gate}
            graph.gates = graph.gates.model_copy(update={"nodes": nodes})

    # ---- running within budget ---------------------------------------------

    def run(
        self,
        graph: ProductionGraph,
        runner: Runner,
        *,
        caps: BudgetCaps | None = None,
        approved: Iterable[str] = (),
        critic: Critic | None = None,
        max_workers: int = 1,
        supervisor: Supervisor | None = None,
    ) -> ProducerReport:
        """Run ``graph``, holding any node that could push usage past a cap.

        With a ``supervisor``, transient failures retry with backoff, a burst of
        permanent failures stops new admissions, and a node out of revisions is
        retried once at the next tier; its decisions are recorded on the node.
        """
        est = self.estimate(graph)
        limits = caps.limits() if caps is not None else {}
        approvals = frozenset(approved)
        totals = graph.ledger.totals()
        spent = {
            "elevenlabs_characters": float(totals.elevenlabs_characters),
            "gpu_minutes": float(totals.gpu_minutes),
            "ai_requests": float(
                sum(v for k, v in totals.ai_usage.items() if k.endswith("_requests"))
            ),
        }
        holds: dict[str, BudgetHold] = {}
        lock = threading.Lock()

        def hold(spec: NodeSpec) -> str | None:
            if supervisor is not None and (reason := supervisor.hold(spec)) is not None:
                return reason
            try:
                cost = est.stage(spec.id).cost
            except KeyError:
                return None
            with lock:
                if spec.id not in approvals:
                    for metric, limit in limits.items():
                        need = _metric(cost, metric)
                        projected = spent[metric] + need
                        if need > 0 and projected > limit:
                            holds[spec.id] = BudgetHold(
                                node_id=spec.id, metric=metric, projected=projected, limit=limit
                            )
                            return f"needs budget approval: {holds[spec.id].text()}"
                for metric in spent:
                    spent[metric] += _metric(cost, metric)
            return None

        if supervisor is None:
            report = graph.run(runner, critic=critic, max_workers=max_workers, hold=hold)
        else:
            mark = {n: len(d) for n, d in supervisor.decisions.items()}
            report = graph.run(
                supervisor.wrap(runner),
                critic=critic,
                max_workers=max_workers,
                hold=hold,
                escalate=supervisor.escalate,
            )
            for nid, log in supervisor.decisions.items():
                if nid in graph.order() and len(log) > mark.get(nid, 0):
                    graph.add_decisions(nid, log[mark.get(nid, 0) :])
        held = [holds[n] for n in report.held if n in holds]
        return ProducerReport(
            run=report,
            estimate=self.estimate(graph),
            needs_approval=held,
            supervisor_hold=supervisor.hold_record() if supervisor is not None else None,
        )


def _assets(brief: Brief | CreativeBrief, profile: QualityProfile) -> tuple[PlannedAsset, ...]:
    """What the production needs before shots: product UI, brand kit, VO and music."""
    product = "brief:product" if isinstance(brief, Brief) else "brief:message"
    assets = [
        PlannedAsset(id="product-ui", kind="ui_capture", source=product),
        PlannedAsset(id="brand-kit", kind="brand_kit", source="brand-kit"),
    ]
    if profile.includes("voiceover"):
        assets.append(PlannedAsset(id="voiceover", kind="audio", source="script:vo"))
    if profile.includes("sound_design") or profile.includes("mix"):
        assets.append(PlannedAsset(id="music", kind="audio", source="licensed-library"))
    return tuple(assets)


# ---- scheduling ----------------------------------------------------------------


class TaskState(StrEnum):
    """What a scheduled task is doing; ``waiting_for_ai`` matches the queue's job state."""

    QUEUED = "queued"
    WAITING_FOR_GPU = "waiting_for_gpu"
    WAITING_FOR_AI = "waiting_for_ai"
    """Same value as ``JobStatus.WAITING_FOR_AI`` of the queue."""
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class ScheduledRunner:
    """Wraps a graph runner so tasks wait for GPU leases and AI capacity instead of failing."""

    def __init__(
        self,
        runner: Runner,
        *,
        departments: Mapping[str, type[Department]] | None = None,
        limits: CapacityLimits | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        max_ai_waits: int = 100,
    ) -> None:
        self.runner = runner
        self.departments = dict(departments or {})
        self.limits = limits if limits is not None else CapacityLimits()
        self.sleep = sleep
        self.now = now
        self.max_ai_waits = max_ai_waits
        self._gpu = threading.BoundedSemaphore(max(1, self.limits.gpu_leases))
        self._lock = threading.Lock()
        self.history: dict[str, list[TaskState]] = {}

    def _note(self, node_id: str, state: TaskState) -> None:
        with self._lock:
            self.history.setdefault(node_id, []).append(state)

    def state(self, node_id: str) -> TaskState | None:
        with self._lock:
            states = self.history.get(node_id)
            return states[-1] if states else None

    def _uses_gpu(self, department: str) -> bool:
        dept = self.departments.get(department)
        return dept is not None and dept.uses_gpu

    def __call__(self, ctx: RunContext) -> RunResult:
        nid = ctx.spec.id
        gpu = self._uses_gpu(ctx.spec.department)
        self._note(nid, TaskState.QUEUED)
        for _ in range(self.max_ai_waits + 1):
            if gpu and not self._gpu.acquire(blocking=False):
                self._note(nid, TaskState.WAITING_FOR_GPU)
                self._gpu.acquire()
            try:
                self._note(nid, TaskState.RUNNING)
                result = self.runner(ctx)
            except ProviderExhausted as exc:
                self._note(nid, TaskState.WAITING_FOR_AI)
                reset_at = exc.reset_at
            except Exception:
                self._note(nid, TaskState.FAILED)
                raise
            else:
                self._note(nid, TaskState.DONE)
                return result
            finally:
                if gpu:
                    self._gpu.release()
            self.sleep(max(0.0, (reset_at - self.now()).total_seconds()))
        self._note(nid, TaskState.FAILED)
        raise RuntimeError(
            f"node {nid!r}: AI capacity still exhausted after {self.max_ai_waits} waits"
        )


def scheduled(
    runner: Runner,
    *,
    departments: Mapping[str, type[Department]] | None = None,
    limits: CapacityLimits | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> ScheduledRunner:
    """Wrap ``runner`` so tasks wait for GPU leases and AI capacity (:class:`ScheduledRunner`)."""
    return ScheduledRunner(runner, departments=departments, limits=limits, sleep=sleep, now=now)
