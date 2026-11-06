# SPDX-License-Identifier: AGPL-3.0-or-later
"""The department runner: executes one node's department task through a text provider.

:class:`DepartmentRunner` is a graph :data:`~spectracut.studio.graph.Runner`. For
each node it:

1. resolves the node's department and checks the department produces its kind;
2. validates the node's inputs against the department's declared input models;
3. assembles the task context from canon, brief, the node's declared inputs, its
   params, the Critic's feedback and reviewers' revision notes (frame-pinned
   comments from the review page), and nothing else;
4. calls the provider with the skill pack's ``prompt.md`` as the system prompt,
   holding a GPU lease (GPU departments) and an AI slot (always), each bounded by
   :class:`CapacityLimits`;
5. validates the output against the department's output model, then checks it
   against the node's inputs (``Department.check_output``), raising
   :class:`~spectracut.studio.department.DepartmentOutputError` on failure so the
   graph records the node ``failed``;
6. reports usage, including ``prompt_context_bytes``, for the ledger.

:meth:`DepartmentRunner.critic` is the Critic hook: a graph Critic that judges a
node's output against its department's rubric.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable

from pydantic import Field, JsonValue

from spectracut.canon.schema import Canon
from spectracut.providers.base import TextProvider
from spectracut.spec._base import StrictModel, canonical_json
from spectracut.spec.brief import Brief, CreativeBrief
from spectracut.studio.department import Department
from spectracut.studio.gates import Critic, CriticVerdict
from spectracut.studio.graph import ProductionGraph, RevisionNote, RunContext, RunResult
from spectracut.studio.ledger import Usage
from spectracut.studio.node import NodeSpec

#: Prepended to the task context in every department prompt.
TASK_INSTRUCTION = (
    "Produce this node's output as JSON matching the given schema. Use only the task "
    "context below: the canon, the brief, the node's inputs, its params, any Critic "
    "feedback on the previous attempt and any reviewer revision notes (pinned to frames); "
    "address every revision note."
)

#: Ledger ``ai_usage`` metric: UTF-8 bytes of the assembled task context.
CONTEXT_METRIC = "prompt_context_bytes"

Judge = Callable[[type[Department], str, JsonValue], CriticVerdict]
"""``judge(department, rubric, output) -> CriticVerdict``."""


class CapacityLimits(StrictModel):
    """How many department tasks may use each scarce resource at once."""

    ai: int = Field(default=1, ge=1)
    """Concurrent AI provider calls."""
    gpu_leases: int = Field(default=0, ge=0)
    """Concurrent tasks of departments that use a GPU."""


class TaskContext(StrictModel):
    """Everything a department task may see besides its skill pack."""

    canon: Canon | None
    brief: Brief | CreativeBrief | None
    inputs: dict[str, JsonValue]
    """Accepted outputs of the node's declared inputs, by input id."""
    params: dict[str, JsonValue]
    feedback: CriticVerdict | None
    notes: tuple[RevisionNote, ...] = ()
    """Reviewer revision notes on the node, oldest first."""


def assemble_context(
    ctx: RunContext, *, canon: Canon | None, brief: Brief | CreativeBrief | None
) -> TaskContext:
    """The task context for ``ctx``: canon, brief, declared inputs, params, feedback, notes."""
    return TaskContext(
        canon=canon,
        brief=brief,
        inputs={i: ctx.inputs[i] for i in ctx.spec.inputs if i in ctx.inputs},
        params=dict(ctx.spec.params),
        feedback=ctx.feedback,
        notes=ctx.notes,
    )


class DepartmentRunner:
    """Runs department tasks for a production graph through one text provider."""

    def __init__(
        self,
        departments: Iterable[type[Department]],
        provider: TextProvider,
        *,
        canon: Canon | None = None,
        brief: Brief | CreativeBrief | None = None,
        limits: CapacityLimits | None = None,
        graph: ProductionGraph | None = None,
    ) -> None:
        self.provider = provider
        self.canon = canon
        self.brief = brief
        self.limits = limits if limits is not None else CapacityLimits()
        self.graph = graph
        """When set, input kinds come from the graph; otherwise an input's id is its kind."""
        self._departments: dict[str, type[Department]] = {}
        producers: dict[str, str] = {}
        for dept in departments:
            if dept.name in self._departments:
                raise ValueError(f"duplicate department {dept.name!r}")
            for kind in dept.produces:
                if kind in producers:
                    raise ValueError(
                        f"node kind {kind!r} is produced by both {producers[kind]!r} "
                        f"and {dept.name!r}"
                    )
                producers[kind] = dept.name
            if dept.uses_gpu and self.limits.gpu_leases < 1:
                raise ValueError(f"department {dept.name!r} uses a GPU but gpu_leases is 0")
            dept.pack()  # fail fast on a missing or incomplete skill pack
            self._departments[dept.name] = dept
        self._ai = threading.BoundedSemaphore(self.limits.ai)
        self._gpu = threading.BoundedSemaphore(max(1, self.limits.gpu_leases))

    def department(self, name: str) -> type[Department]:
        try:
            return self._departments[name]
        except KeyError:
            raise KeyError(f"unknown department {name!r}") from None

    def _resolve(self, spec: NodeSpec) -> type[Department]:
        dept = self.department(spec.department)
        if spec.kind not in dept.produces:
            raise ValueError(
                f"department {dept.name!r} does not produce node kind {spec.kind!r} "
                f"(node {spec.id!r})"
            )
        return dept

    def context(self, ctx: RunContext) -> TaskContext:
        return assemble_context(ctx, canon=self.canon, brief=self.brief)

    def __call__(self, ctx: RunContext) -> RunResult:
        spec = ctx.spec
        dept = self._resolve(spec)
        kinds = {i: self.graph.spec(i).kind for i in spec.inputs} if self.graph else {}
        dept.validate_inputs(spec.id, ctx.inputs, kinds)
        context = canonical_json(self.context(ctx))
        prompt = f"{TASK_INSTRUCTION}\n\n{context.decode('utf-8')}"
        system = dept.pack().prompt
        schema = dept.output_schema()
        if dept.uses_gpu:
            with self._gpu, self._ai:
                completion = self.provider.complete(system, prompt, schema=schema)
        else:
            with self._ai:
                completion = self.provider.complete(system, prompt, schema=schema)
        output = dept.validate_output(spec.id, completion.output)
        dept.check_output(spec.id, output, ctx.inputs, kinds)
        usage = {**completion.usage, CONTEXT_METRIC: len(context)}
        return RunResult(output=output, usage=Usage(ai_usage=usage))

    def critic(self, judge: Judge) -> Critic:
        """A graph Critic that judges each output against its department's rubric."""

        def review(spec: NodeSpec, output: JsonValue) -> CriticVerdict:
            dept = self._resolve(spec)
            return judge(dept, dept.rubric(), output)

        return review
