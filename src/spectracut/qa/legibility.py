# SPDX-License-Identifier: AGPL-3.0-or-later
"""QA legibility check: is the device screen's UI text readable against its background?

The measure is WCAG 2.x contrast. :func:`relative_luminance` takes 8-bit
sRGB/Rec.709 code values, and :func:`contrast_ratio` is
``(L_hi + 0.05) / (L_lo + 0.05)``, from 1 (equal colours) to 21 (black on
white). Inside a screen rect, the darkest and brightest ends of the luminance
distribution (5th and 95th percentiles) stand for text and background.
Whichever end sits closer to the median is the background, so dark-on-light
and light-on-dark UIs both read correctly.

:func:`screen_legibility` measures an 8-bit master PNG. :func:`linear_legibility`
is the frame-free variant over scene-linear RGB rows; it is used with
:func:`spectracut.finishing.comp.screen_transfer` to prove that every comp
graph keeps the screen legible. The default threshold is WCAG AA (4.5:1).

#27 (the automated QA gate) extends this module with text height and fixtures.
Pure Python; no Blender.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from spectracut.harness.imagecmp import ImageError, decode_png

AA_RATIO = 4.5
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


def contrast_ratio(a: float, b: float) -> float:
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
    contrast = contrast_ratio(text, background)
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
