# SPDX-License-Identifier: AGPL-3.0-or-later
"""Concrete studio departments and the one registry of them.

Discovery (intake, product understanding, brief) lives in ``.discovery``,
Development (concepts, script) in ``.writer``, Art in ``.art``, Shots (template
shots and shots rendered on the farm) in ``.shots`` and Sound (voiceover, sound
design, music, mix) in ``.sound``; import from those modules.
:func:`all_departments` lists every department ``studio run`` can use, and
:data:`STAGE_DEPARTMENTS` says which one builds each profile stage (node kind). A
stage with no entry keeps its own name as the department name, so it shows up as
unregistered in the estimate until a department is written.

A department's declared ``inputs`` are the production graph's edges:
:func:`spectracut.studio.estimate.breakdown` wires each stage to the earlier
stages of the kinds its department declares.
"""

from __future__ import annotations

from spectracut.studio.department import Department

STAGE_DEPARTMENTS: dict[str, str] = {
    "brief": "strategist",
    "concept": "concept-writer",
    "script": "script-writer",
    "art_direction": "art-director",
    "style_frames": "style-frame-artist",
    "storyboard": "storyboard",
    "animatic": "animatic-editor",
    "template_shots": "template-shots",
    "shots": "shots",
    "voiceover": "voiceover",
    "sound_design": "sound-designer",
    "music": "composer",
    "mix": "mixer",
}
"""Profile stage (node kind) -> name of the department that builds it."""


def department_for(stage: str) -> str:
    """The department name for ``stage``; the stage name itself when none is mapped."""
    return STAGE_DEPARTMENTS.get(stage, stage)


def all_departments() -> dict[str, type[Department]]:
    """Every implemented department by name (imported lazily to avoid import cycles)."""
    from spectracut.studio.departments.art import ART_DEPARTMENTS
    from spectracut.studio.departments.discovery import DISCOVERY_DEPARTMENTS
    from spectracut.studio.departments.shots import SHOT_DEPARTMENTS
    from spectracut.studio.departments.sound import SOUND_DEPARTMENTS
    from spectracut.studio.departments.writer import DEVELOPMENT_DEPARTMENTS

    return {
        d.name: d
        for d in (
            *DISCOVERY_DEPARTMENTS,
            *DEVELOPMENT_DEPARTMENTS,
            *ART_DEPARTMENTS,
            *SHOT_DEPARTMENTS,
            *SOUND_DEPARTMENTS,
        )
    }
