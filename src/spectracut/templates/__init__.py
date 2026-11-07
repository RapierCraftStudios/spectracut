# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shot template library (Python definitions and assets manifest).

Brand-kit-driven, deterministic recipe builders on the host's template ops:
devices (laptop, phone on the glTF device kit), hero object, end card and depth/cutout typography.
"""

from __future__ import annotations

from spectracut.templates import base, device_kit, devices, end_card, hero_object, typography
from spectracut.templates.base import Template
from spectracut.templates.devices import LAPTOP, PHONE
from spectracut.templates.end_card import END_CARD
from spectracut.templates.hero_object import HERO_OBJECT
from spectracut.templates.typography import TYPOGRAPHY

# Classes and helpers (Template, TemplateError, CameraVariant, final_tier_steps, ...)
# are exported by their defining modules; this package only adds the registry.
TEMPLATES: dict[str, Template] = {  # type: ignore[type-arg]
    t.id: t for t in (LAPTOP, PHONE, HERO_OBJECT, END_CARD, TYPOGRAPHY)
}

__all__ = [
    "END_CARD",
    "HERO_OBJECT",
    "LAPTOP",
    "PHONE",
    "TEMPLATES",
    "TYPOGRAPHY",
    "base",
    "device_kit",
    "devices",
    "end_card",
    "hero_object",
    "typography",
]
