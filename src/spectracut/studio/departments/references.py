# SPDX-License-Identifier: AGPL-3.0-or-later
"""The production's reference films as a node the creative departments read.

- ``references`` (node kind ``references``): the production's
  :class:`~spectracut.spec.references.ReferenceSet` (craft notes and local keyframe
  paths), built in code from the node's ``references`` param by
  :func:`build_references`, the same way the brand kit is.
- :func:`check_craft_refs` is the shared output check: with a references input, an
  output must cite at least one craft point (``craft_refs``) and every cited id must
  be a craft point of the set. With no references input it does nothing.

The films are studied for craft only, never copied.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from pydantic import JsonValue

from spectracut.spec.references import ReferenceSet
from spectracut.studio.department import (
    SKILLS_ROOT,
    CostEstimate,
    Department,
    DepartmentOutputError,
)
from spectracut.studio.graph import RunContext, RunResult
from spectracut.studio.ledger import Usage
from spectracut.studio.node import NodeSpec

REFERENCES = "references"
REFERENCES_PARAM = "references"
"""Param of the ``references`` node holding the reference set's JSON."""


class ReferencesDepartment(Department):
    """The production's reference set as a node; built in code by :func:`build_references`."""

    name = "references"
    produces = frozenset({REFERENCES})
    output = ReferenceSet
    skill_pack = SKILLS_ROOT / "art-director"
    cost = CostEstimate()


REFERENCE_DEPARTMENTS: tuple[type[Department], ...] = (ReferencesDepartment,)


def references_node(refs: ReferenceSet) -> NodeSpec:
    """The ``references`` node carrying ``refs``; the creative nodes read it as an input."""
    return NodeSpec(
        id=REFERENCES,
        kind=REFERENCES,
        department=ReferencesDepartment.name,
        params={REFERENCES_PARAM: refs.model_dump(mode="json")},
    )


def build_references(ctx: RunContext) -> RunResult:
    """Build a ``references`` node in code: its ``references`` param, validated."""
    raw = ctx.spec.params.get(REFERENCES_PARAM)
    if raw is None:
        raise ValueError(
            f"node {ctx.spec.id!r} has no {REFERENCES_PARAM!r} param "
            "(recreate the production to load its reference set)"
        )
    refs = ReferenceSet.model_validate(raw)
    return RunResult(output=refs.model_dump(mode="json"), usage=Usage())


def references_input(
    inputs: Mapping[str, JsonValue], kinds: Mapping[str, str]
) -> ReferenceSet | None:
    """The reference set among ``inputs`` (an id missing from ``kinds`` is its kind)."""
    for input_id, value in inputs.items():
        if kinds.get(input_id, input_id) == REFERENCES:
            return ReferenceSet.model_validate(value)
    return None


def craft_ref_problems(refs: ReferenceSet, cited: Sequence[str]) -> list[str]:
    """Why ``cited`` is not a valid citation of ``refs``' craft points (empty when it is)."""
    if not cited:
        return [f"cites no craft point of reference set {refs.id!r} (craft_refs is empty)"]
    unknown = refs.unknown(list(cited))
    if unknown:
        return [f"craft_refs not in reference set {refs.id!r}: {', '.join(unknown)}"]
    return []


def check_craft_refs(
    department: str,
    node_id: str,
    cited: Sequence[str],
    inputs: Mapping[str, JsonValue],
    kinds: Mapping[str, str],
) -> ReferenceSet | None:
    """Raise when a references input is wired and ``cited`` is empty or unknown.

    Returns the reference set (``None`` with no references input: nothing to check).
    """
    refs = references_input(inputs, kinds)
    if refs is None:
        return None
    problems = craft_ref_problems(refs, cited)
    if problems:
        raise DepartmentOutputError(department, node_id, "; ".join(problems))
    return refs
