# SPDX-License-Identifier: AGPL-3.0-or-later
"""Development: concepts written in parallel, then the chosen one becomes the script.

- ``concept-writer`` (node kind ``concept``): one :class:`Concept` per node from
  the creative brief and the node's ``angle`` param. :func:`development_nodes`
  declares several concept nodes that read only the brief, so
  ``ProductionGraph.run(max_workers=n)`` writes them at the same time.
- A person picks one at the concepts' human gate: :func:`pick_concept` approves
  it, rejects the others and adds the ``script`` node that reads the chosen one.
- ``script-writer`` (kind ``script``): the :class:`Script` (beats with VO,
  on-screen text and the claims each line makes, each citing a brief source).

Every script passes the copy gate (:mod:`spectracut.writer.copy_gate`).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from pydantic import BaseModel, Field

from spectracut.spec._base import NonEmptyStr, Slug, StrictModel
from spectracut.spec.brief import CreativeBrief
from spectracut.studio.department import CostEstimate, Department
from spectracut.studio.departments.discovery import BRIEF, STORY_SKILLS
from spectracut.studio.graph import ProductionGraph
from spectracut.studio.node import NodeSpec, NodeStatus

CONCEPT = "concept"
SCRIPT = "script"

#: Default angles for the parallel concepts, one per concept node.
DEFAULT_ANGLES: tuple[str, ...] = ("problem-solution", "product-demo", "customer-story")


class Concept(StrictModel):
    """One idea for the film: a logline, a structure and why it fits the audience."""

    title: NonEmptyStr
    logline: NonEmptyStr
    structure: list[NonEmptyStr] = Field(min_length=1)
    fit: NonEmptyStr
    """Why this concept fits the brief's audience."""
    visuals: list[NonEmptyStr] = Field(default_factory=list)
    """The concept's signature visuals; every style frame shows one of them or a beat."""


class Claim(StrictModel):
    """A factual claim a script line makes, and the brief source it rests on."""

    text: NonEmptyStr
    source_id: str = ""
    """Id of one of the brief's sources; empty means unsourced (the copy gate fails it)."""


class ScriptLine(StrictModel):
    """One beat of the script: what is said, what is on screen, what is claimed."""

    beat: NonEmptyStr
    vo: str = ""
    on_screen: str = ""
    claims: list[Claim] = Field(default_factory=list)


class Script(StrictModel):
    """The chosen concept written out: VO, on-screen text and beats."""

    concept_id: Slug
    lines: list[ScriptLine] = Field(min_length=1)


class ConceptWriter(Department):
    """Writes one concept from the brief and the node's angle."""

    name = "concept-writer"
    produces = frozenset({CONCEPT})
    inputs: ClassVar[dict[str, type[BaseModel]]] = {BRIEF: CreativeBrief}
    output = Concept
    skill_pack = STORY_SKILLS / "concept-writer"
    cost = CostEstimate(ai_requests=1, ai_input_tokens=3_000)


class ScriptWriter(Department):
    """Turns the chosen concept into the script."""

    name = "script-writer"
    produces = frozenset({SCRIPT})
    inputs: ClassVar[dict[str, type[BaseModel]]] = {BRIEF: CreativeBrief, CONCEPT: Concept}
    output = Script
    skill_pack = STORY_SKILLS / "script-writer"
    cost = CostEstimate(ai_requests=1, ai_input_tokens=4_000)


DEVELOPMENT_DEPARTMENTS: tuple[type[Department], ...] = (ConceptWriter, ScriptWriter)


def development_nodes(
    brief_id: str = BRIEF, n: int = 3, angles: Sequence[str] | None = None
) -> list[NodeSpec]:
    """``n`` concept nodes (``concept-1`` .. ``concept-n``), each reading only the brief."""
    if n < 1:
        raise ValueError(f"need at least one concept, got {n}")
    chosen = list(angles) if angles is not None else list(DEFAULT_ANGLES)
    if len(chosen) < n:
        chosen += [f"angle-{i + 1}" for i in range(len(chosen), n)]
    return [
        NodeSpec(
            id=f"{CONCEPT}-{i + 1}",
            kind=CONCEPT,
            department=ConceptWriter.name,
            inputs=(brief_id,),
            params={"angle": chosen[i]},
        )
        for i in range(n)
    ]


def pick_concept(
    graph: ProductionGraph,
    chosen_id: str,
    *,
    brief_id: str = BRIEF,
    script_id: str = SCRIPT,
    comment: str = "",
) -> NodeSpec:
    """Pass the concepts' human gate with ``chosen_id`` and declare the script node.

    Approves ``chosen_id``, rejects every other concept node still awaiting
    approval, and adds (or rewires) ``script_id`` to read the brief and the chosen
    concept. Returns the script node's spec; the next ``run`` writes it.
    """
    spec = graph.spec(chosen_id)
    if spec.kind != CONCEPT:
        raise ValueError(f"node {chosen_id!r} is a {spec.kind!r}, not a concept")
    if graph.status(chosen_id) is not NodeStatus.AWAITING_APPROVAL:
        raise ValueError(f"concept {chosen_id!r} is not awaiting approval")
    graph.approve(chosen_id, comment)
    for nid in graph.order():
        if (
            nid != chosen_id
            and graph.spec(nid).kind == CONCEPT
            and graph.status(nid) is NodeStatus.AWAITING_APPROVAL
        ):
            graph.reject(nid, f"not chosen; {chosen_id} was")
    script = NodeSpec(
        id=script_id,
        kind=SCRIPT,
        department=ScriptWriter.name,
        inputs=(brief_id, chosen_id),
        params={"concept_id": chosen_id},
    )
    if script_id in graph.order():
        graph.update_node(script)
    else:
        graph.add_node(script)
    return script
