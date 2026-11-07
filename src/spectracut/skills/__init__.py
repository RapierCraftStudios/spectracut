# SPDX-License-Identifier: AGPL-3.0-or-later
"""Department skill packs: craft knowledge, rubrics, failure corpus and taste anchors.

Each department's pack is a folder of markdown (plus ``rubric.json``) under this
package; see ``README.md``. This module is a leaf: it imports nothing from the
studio or the critique packages.
"""

from __future__ import annotations

from pathlib import Path

#: Folder holding the shipped skill packs: ``spectracut/skills/<department>/``.
SKILLS_DIR = Path(__file__).resolve().parent

#: The departments of the M2 studio, in production order (one skill pack each).
M2_DEPARTMENTS: tuple[str, ...] = (
    "writer",
    "director",
    "art-director",
    "storyboard",
    "editor",
    "lighter",
    "scene-builder",
    "compositor",
    "motion-designer",
    "sound-designer",
    "mixer",
    "critic",
)


def pack_dir(name: str) -> Path:
    """Folder of the shipped skill pack for department ``name``."""
    return SKILLS_DIR / name


__all__ = ["M2_DEPARTMENTS", "SKILLS_DIR", "pack_dir"]
