# SPDX-License-Identifier: AGPL-3.0-or-later
"""Edit-level QA: overlays inside the platform safe area, and shot repetition."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from spectracut.formats.linter import ShotLook
from spectracut.formats.safe_areas import Overlay, SafeAreaError, inside_safe_area, safe_area
from spectracut.qa.common import BBox, Gray, QACheck, mean, resample

REPEAT_WINDOW = 3
"""Shots this close in the edit (by position) are compared for repetition."""

REPEAT_TOLERANCE = 0.03
"""Mean absolute luma difference of two signatures at or under which shots repeat."""

SIGNATURE_W, SIGNATURE_H = 16, 9


def check_safe_areas(platform: str, aspect: str, overlays: Sequence[Overlay]) -> list[QACheck]:
    """One row per overlay; an unknown platform or aspect is a single failure."""
    try:
        safe_area(platform, aspect)
        rows = [
            QACheck(
                name=f"safe_area.{o.name}",
                status="pass" if inside_safe_area(platform, aspect, o) else "fail",
                detail=f"{o.name} must sit inside the {platform} {aspect} safe area",
                bbox=BBox(x=o.x, y=o.y, w=o.w, h=o.h),
            )
            for o in overlays
        ]
    except SafeAreaError as exc:
        return [QACheck(name="safe_area", status="fail", detail=str(exc))]
    return rows or [QACheck(name="safe_area", status="pass", detail="no overlays")]


@dataclass(frozen=True)
class EditShot:
    """A shot of the edit: its id, a representative frame and how it was framed."""

    id: str
    frame: Gray | None = None
    look: ShotLook | None = None
    frame_index: int | None = None


def signature(frame: Gray) -> tuple[float, ...]:
    """A 16x9 area-averaged luma signature of ``frame``."""
    return resample(frame, SIGNATURE_W, SIGNATURE_H).pixels


def signature_distance(a: Sequence[float], b: Sequence[float]) -> float:
    return mean([abs(x - y) for x, y in zip(a, b, strict=True)])


def check_repetition(
    shots: Sequence[EditShot],
    *,
    window: int = REPEAT_WINDOW,
    tolerance: float = REPEAT_TOLERANCE,
) -> list[QACheck]:
    """A failure for each pair of shots within ``window`` that look or are framed alike."""
    sigs = [signature(s.frame) if s.frame is not None else None for s in shots]
    out: list[QACheck] = []
    for i, a in enumerate(shots):
        for j in range(i + 1, min(len(shots), i + window + 1)):
            b = shots[j]
            reason = ""
            sa, sb = sigs[i], sigs[j]
            if sa is not None and sb is not None:
                dist = signature_distance(sa, sb)
                if dist <= tolerance:
                    reason = f"frames match (mean abs diff {dist:.3f})"
            if (
                not reason
                and a.look is not None
                and b.look is not None
                and a.look.same_setup(b.look)
            ):
                reason = "same scale, lens and move"
            if reason:
                out.append(
                    QACheck(
                        name="repetition",
                        status="fail",
                        detail=f"shot {b.id} repeats shot {a.id}: {reason}",
                        frame=b.frame_index,
                    )
                )
    return out or [QACheck(name="repetition", status="pass")]
