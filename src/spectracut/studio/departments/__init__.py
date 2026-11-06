# SPDX-License-Identifier: AGPL-3.0-or-later
"""Concrete studio departments and the one registry of them.

Discovery (intake, product understanding, brief) lives in ``.discovery``,
Development (concepts, script) in ``.writer`` and Art in ``.art``; import from
those modules. :func:`all_departments` lists every department ``studio run``
can use, and :data:`STAGE_DEPARTMENTS` says which one builds each profile stage
(node kind). A stage with no entry keeps its own name as the department name, so
it shows up as unregistered in the estimate until a department is written.
"""

from __future__ import annotations

from spectracut.studio.department import Department

STAGE_DEPARTMENTS: dict[str, str] = {
    "brief": "strategist",
    "concept": "concept-writer",
    "script": "script-writer",
    "style_frames": "style-frame-artist",
    "storyboard": "storyboard",
    "animatic": "animatic-editor",
}
"""Profile stage (node kind) -> name of the department that builds it."""


def department_for(stage: str) -> str:
    """The department name for ``stage``; the stage name itself when none is mapped."""
    return STAGE_DEPARTMENTS.get(stage, stage)


def all_departments() -> dict[str, type[Department]]:
    """Every implemented department by name (imported lazily to avoid import cycles)."""
    from spectracut.studio.departments.art import ART_DEPARTMENTS
    from spectracut.studio.departments.discovery import DISCOVERY_DEPARTMENTS
    from spectracut.studio.departments.writer import DEVELOPMENT_DEPARTMENTS

    return {d.name: d for d in (*DISCOVERY_DEPARTMENTS, *DEVELOPMENT_DEPARTMENTS, *ART_DEPARTMENTS)}
