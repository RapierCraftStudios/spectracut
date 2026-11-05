# SPDX-License-Identifier: AGPL-3.0-or-later
"""Screen-text legibility: contrast and height of text regions, and text read time.

A text region is a box the caller knows holds one line of on-screen text (a UI
label on the device screen, a title, a caption). Contrast is the WCAG ratio of
the region's brightest and darkest tenths in linear light; height is the extent
of the rows that carry ink, as a fraction of the frame height.

Device-screen legibility (#55): :func:`screen_legibility` measures the UI inside a
screen rect of an 8-bit master PNG, and :func:`linear_legibility` does the same over
scene-linear RGB rows. :func:`spectracut.finishing.comp.screen_transfer` uses it to
prove every comp graph keeps the screen at WCAG AA. The darkest and brightest ends of
the luminance distribution (5th/95th percentiles) stand for text and background;
whichever sits closer to the median is the background.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import Field

from spectracut.harness.imagecmp import ImageError, decode_png
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


# ---- device-screen legibility (#55) ---------------------------------------------------

AA_RATIO = MIN_CONTRAST
"""WCAG 2.x AA minimum contrast for body text."""
TEXT_PERCENTILE = 0.05
BACKGROUND_PERCENTILE = 0.95
_R, _G, _B = 0.2126, 0.7152, 0.0722

RGB = tuple[float, float, float]
Rect = tuple[int, int, int, int]
"""``(x, y, width, height)`` in pixels, origin top-left."""


@dataclass(frozen=True)
class LegibilityReport:
    """Text and background luminance inside the screen, their contrast, and the verdict."""

    text_luminance: float
    background_luminance: float
    contrast: float
    passed: bool


def _linearise(code: float) -> float:
    c = code / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(rgb: Sequence[float]) -> float:
    """WCAG relative luminance (0..1) of 8-bit sRGB/Rec.709 code values ``(r, g, b)``."""
    r, g, b = (float(v) for v in rgb[:3])
    for v in (r, g, b):
        if not 0.0 <= v <= 255.0:
            raise ValueError(f"code value {v} is outside 0..255")
    return _R * _linearise(r) + _G * _linearise(g) + _B * _linearise(b)


def linear_luminance(rgb: Sequence[float]) -> float:
    """Relative luminance of scene-linear Rec.709 RGB, clipped to the display range 0..1."""
    r, g, b = (min(max(float(v), 0.0), 1.0) for v in rgb[:3])
    return _R * r + _G * g + _B * b


def luminance_contrast(a: float, b: float) -> float:
    """WCAG contrast ratio of two relative luminances (order does not matter)."""
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def _percentile(sorted_values: list[float], q: float) -> float:
    return sorted_values[min(len(sorted_values) - 1, int(q * (len(sorted_values) - 1) + 0.5))]


def legibility_of(luminances: Sequence[float], min_ratio: float = AA_RATIO) -> LegibilityReport:
    """Text vs background contrast of a screen region's luminances."""
    values = sorted(luminances)
    if not values:
        raise ValueError("legibility needs at least one pixel")
    dark = _percentile(values, TEXT_PERCENTILE)
    light = _percentile(values, BACKGROUND_PERCENTILE)
    median = _percentile(values, 0.5)
    text, background = (dark, light) if light - median <= median - dark else (light, dark)
    contrast = luminance_contrast(text, background)
    return LegibilityReport(
        text_luminance=text,
        background_luminance=background,
        contrast=contrast,
        passed=contrast >= min_ratio,
    )


def linear_legibility(
    rows: Sequence[Sequence[Sequence[float]]], min_ratio: float = AA_RATIO
) -> LegibilityReport:
    """:func:`legibility_of` over rows of scene-linear RGB pixels (no frame needed)."""
    return legibility_of([linear_luminance(px) for row in rows for px in row], min_ratio)


def screen_legibility(
    png_bytes: bytes, rect: Rect, min_ratio: float = AA_RATIO
) -> LegibilityReport:
    """Legibility of the UI inside ``rect`` of an 8-bit master PNG.

    Raises :class:`ValueError` when the PNG cannot be decoded or ``rect`` is
    empty or not fully inside the image.
    """
    try:
        image = decode_png(png_bytes)
    except ImageError as exc:
        raise ValueError(f"legibility needs an 8-bit PNG: {exc}") from None
    x, y, w, h = rect
    if w <= 0 or h <= 0 or x < 0 or y < 0 or x + w > image.width or y + h > image.height:
        raise ValueError(f"screen rect {rect} is not inside the {image.width}x{image.height} frame")
    n, px = image.channels, image.pixels
    stride = image.width * n
    lums: list[float] = []
    for row in range(y, y + h):
        for col in range(x, x + w):
            at = row * stride + col * n
            sample = px[at : at + 3] if n >= 3 else bytes([px[at]] * 3)
            lums.append(relative_luminance(tuple(sample)))
    return legibility_of(lums, min_ratio)
