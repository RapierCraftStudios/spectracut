# SPDX-License-Identifier: AGPL-3.0-or-later
"""Concrete studio departments: Discovery (intake, product, brief) and Development."""

from __future__ import annotations

from spectracut.studio.departments.discovery import (
    DISCOVERY_DEPARTMENTS,
    STORY_SKILLS,
    Intake,
    IntakeNotes,
    ProductFeature,
    ProductNotes,
    ProductUnderstanding,
    Strategist,
    discovery_nodes,
)
from spectracut.studio.departments.writer import (
    DEVELOPMENT_DEPARTMENTS,
    Claim,
    Concept,
    ConceptWriter,
    Script,
    ScriptLine,
    ScriptWriter,
    development_nodes,
    pick_concept,
)

__all__ = [
    "DEVELOPMENT_DEPARTMENTS",
    "DISCOVERY_DEPARTMENTS",
    "STORY_SKILLS",
    "Claim",
    "Concept",
    "ConceptWriter",
    "Intake",
    "IntakeNotes",
    "ProductFeature",
    "ProductNotes",
    "ProductUnderstanding",
    "Script",
    "ScriptLine",
    "ScriptWriter",
    "Strategist",
    "development_nodes",
    "discovery_nodes",
    "pick_concept",
]
