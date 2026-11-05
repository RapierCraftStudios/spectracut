# SPDX-License-Identifier: AGPL-3.0-or-later
"""Screen-text legibility: contrast and height of text regions, and text read time.

A text region is a box the caller knows holds one line of on-screen text (a UI
label on the device screen, a title, a caption). Contrast is the WCAG ratio of
the region's brightest and darkest tenths in linear light; height is the extent
of the rows that carry ink, as a fraction of the frame height.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import Field

from spectracut.qa.common import BBox, Gray, QACheck, mean, srgb_to_linear
from spectracut.spec._base import StrictModel

MIN_CONTRAST = 4.5
"""WCAG 2.2 AA for body text."""

MIN_TEXT_HEIGHT = 0.025
"""Text under 2.5% of the frame height is unreadable on a phone at 9:16."""

READ_RATE_WPS = 3.0
"""Words per second a viewer reads on screen."""

READ_LEAD_S = 0.5
"""Time to find the text before reading starts."""


class TextRegion(StrictModel):
    """A box (fractions of the frame) holding one line of on-screen text."""

    name: str
    box: BBox
    words: int = Field(default=0, ge=0)


class LegibilityLimits(StrictModel):
    min_contrast: float = Field(default=MIN_CONTRAST, gt=1.0)
    min_height: float = Field(default=MIN_TEXT_HEIGHT, gt=0.0, lt=1.0)


def contrast_ratio(region: Gray) -> float:
    """WCAG contrast ratio between the brightest and darkest tenths of ``region``."""
    ordered = sorted(region.pixels)
    tenth = max(1, len(ordered) // 10)
    dark = srgb_to_linear(mean(ordered[:tenth]))
    light = srgb_to_linear(mean(ordered[-tenth:]))
    return (light + 0.05) / (dark + 0.05)


def text_height(frame: Gray, box: BBox) -> float:
    """Height of the ink rows inside ``box``, as a fraction of the frame height."""
    region = frame.crop(box)
    ordered = sorted(region.pixels)
    lo, hi = ordered[0], ordered[-1]
    if hi - lo < 0.02:
        return 0.0
    background = ordered[len(ordered) // 2]
    edge = 0.5 * (hi - lo)
    rows = [
        y
        for y in range(region.height)
        if any(
            abs(region.pixels[y * region.width + x] - background) > edge
            for x in range(region.width)
        )
    ]
    if not rows:
        return 0.0
    return (rows[-1] - rows[0] + 1) / frame.height


def check_legibility(
    frame: Gray,
    regions: Sequence[TextRegion],
    *,
    frame_index: int,
    limits: LegibilityLimits | None = None,
) -> list[QACheck]:
    """Contrast and height checks for each text region of one frame."""
    lim = limits or LegibilityLimits()
    out: list[QACheck] = []
    for region in regions:
        ratio = contrast_ratio(frame.crop(region.box))
        out.append(
            QACheck(
                name=f"legibility.contrast.{region.name}",
                status="pass" if ratio >= lim.min_contrast else "fail",
                value=round(ratio, 3),
                limit=lim.min_contrast,
                detail="" if ratio >= lim.min_contrast else "screen text is too dim to read",
                frame=frame_index,
                bbox=region.box,
            )
        )
        height = text_height(frame, region.box)
        out.append(
            QACheck(
                name=f"legibility.height.{region.name}",
                status="pass" if height >= lim.min_height else "fail",
                value=round(height, 4),
                limit=lim.min_height,
                detail="" if height >= lim.min_height else "text is too small",
                frame=frame_index,
                bbox=region.box,
            )
        )
    return out


def min_read_time(
    words: int, *, rate_wps: float = READ_RATE_WPS, lead_s: float = READ_LEAD_S
) -> float:
    return lead_s + words / rate_wps


def check_read_time(name: str, words: int, on_screen_s: float) -> QACheck:
    """Whether ``words`` of text stay on screen long enough to read."""
    needed = min_read_time(words)
    ok = on_screen_s >= needed
    return QACheck(
        name=f"read_time.{name}",
        status="pass" if ok else "fail",
        value=round(on_screen_s, 3),
        limit=round(needed, 3),
        detail="" if ok else f"{words} words need {needed:.1f} s on screen",
    )
