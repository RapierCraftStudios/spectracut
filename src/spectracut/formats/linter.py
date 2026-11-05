# SPDX-License-Identifier: AGPL-3.0-or-later
"""The edit-rule linter: editing rules checked against an :class:`Edit`.

Rule ``repeat-setup``: two shots cut back to back must differ in at least one of
shot scale, lens and camera move; the same setup twice reads as a jump cut.
Gaps and transitions between two clips do not separate them. Shot scale is
not part of :class:`~spectracut.spec.spec.CameraSpec`, so the caller passes each
shot's :class:`ShotLook` by shot id.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Literal

from pydantic import Field

from spectracut.edit.timeline import Clip, Edit
from spectracut.spec._base import Slug, StrictModel
from spectracut.spec.spec import CameraMove

ShotScale = Literal["ecu", "cu", "mcu", "ms", "mws", "ws", "ews"]
"""Extreme close-up to extreme wide shot."""


class ShotLook(StrictModel):
    """How a shot is framed: scale, lens and move."""

    scale: ShotScale
    lens_mm: float = Field(gt=0)
    move: CameraMove = "static"

    def same_setup(self, other: ShotLook) -> bool:
        return (
            self.scale == other.scale
            and math.isclose(self.lens_mm, other.lens_mm)
            and self.move == other.move
        )


class LintIssue(StrictModel):
    rule: Slug
    clip_ids: tuple[str, ...]
    message: str


class EditRuleError(ValueError):
    """An edit breaks one or more edit rules."""

    def __init__(self, issues: list[LintIssue]) -> None:
        self.issues = issues
        super().__init__("; ".join(i.message for i in issues))


def _lint_edit(edit: Edit, looks: Mapping[str, ShotLook], issues: list[LintIssue]) -> None:
    for track in edit.tracks:
        if track.kind != "video":
            continue
        prev: tuple[Clip, ShotLook] | None = None
        for item in track.items:
            if not isinstance(item, Clip):
                continue
            if item.nested is not None:
                _lint_edit(item.nested, looks, issues)
                prev = None
                continue
            if item.shot_id is None:
                prev = None
                continue
            look = looks[item.shot_id]
            if prev is not None and prev[1].same_setup(look):
                issues.append(
                    LintIssue(
                        rule="repeat-setup",
                        clip_ids=(prev[0].id, item.id),
                        message=(
                            f"{edit.id}/{track.id}: clips {prev[0].id} and {item.id} cut "
                            f"back to back with the same scale, lens and move"
                        ),
                    )
                )
            prev = (item, look)


def lint(edit: Edit, looks: Mapping[str, ShotLook]) -> list[LintIssue]:
    """Edit-rule issues in ``edit``; ``looks`` maps shot id to its framing.

    Raises ``KeyError`` for a clip whose shot has no look.
    """
    issues: list[LintIssue] = []
    _lint_edit(edit, looks, issues)
    return issues


def ensure_clean(edit: Edit, looks: Mapping[str, ShotLook]) -> None:
    """Raise :class:`EditRuleError` if ``edit`` breaks any edit rule."""
    issues = lint(edit, looks)
    if issues:
        raise EditRuleError(issues)
