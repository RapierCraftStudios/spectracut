# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shot template library (Python definitions and assets manifest).

Brand-kit-driven, deterministic recipe builders on the host's template ops:
devices (laptop, phone), hero object, end card and depth/cutout typography.
"""

from __future__ import annotations

from spectracut.templates.base import (
    ASPECTS,
    CameraVariant,
    PinnedAsset,
    Template,
    TemplateError,
    final_tier_steps,
    preview_steps,
)
from spectracut.templates.devices import LAPTOP, PHONE, DeviceSlots
from spectracut.templates.end_card import END_CARD, EndCardSlots
from spectracut.templates.hero_object import HERO_OBJECT, HeroSlots
from spectracut.templates.typography import TYPOGRAPHY, TypeSlots

TEMPLATES: dict[str, Template] = {  # type: ignore[type-arg]
    t.id: t for t in (LAPTOP, PHONE, HERO_OBJECT, END_CARD, TYPOGRAPHY)
}

__all__ = [
    "ASPECTS",
    "END_CARD",
    "HERO_OBJECT",
    "LAPTOP",
    "PHONE",
    "TEMPLATES",
    "TYPOGRAPHY",
    "CameraVariant",
    "DeviceSlots",
    "EndCardSlots",
    "HeroSlots",
    "PinnedAsset",
    "Template",
    "TemplateError",
    "TypeSlots",
    "final_tier_steps",
    "preview_steps",
]
