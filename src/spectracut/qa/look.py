# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Look QA gate: a master's grain, haze and bloom against its Look's own thresholds.

Every Look declares ``qa_thresholds`` (``grain_max``, ``haze_max``,
``bloom_max``); :func:`look_gate` compares measured metrics against those and
never against a global constant, so a cinematic Look may carry more grain than
a clean one. The frame measurements that produce :class:`LookMetrics` are
:func:`measure_look_metrics`; :func:`flicker` and :func:`exposure_jumps` are
the motion checks the QA report (#27) runs alongside.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Literal

from spectracut.qa.common import BBox, Gray, downscale, mean, percentile
from spectracut.spec._base import StrictModel, UnitFloat, Version
from spectracut.spec.brand import Look

Metric = Literal["grain", "haze", "bloom"]


class LookMetrics(StrictModel):
    """Measured finishing metrics of a master, each 0..1."""

    grain: UnitFloat
    haze: UnitFloat
    bloom: UnitFloat


class LookCheck(StrictModel):
    metric: Metric
    value: float
    limit: float
    passed: bool


class LookQAReport(StrictModel):
    look_id: str
    look_version: Version
    checks: list[LookCheck]
    passed: bool

    def failures(self) -> list[LookCheck]:
        return [c for c in self.checks if not c.passed]


def look_gate(metrics: LookMetrics, look: Look) -> LookQAReport:
    """Check ``metrics`` against ``look.qa_thresholds``; a value equal to its limit passes."""
    limits = look.qa_thresholds
    rows: list[tuple[Metric, float, float]] = [
        ("grain", metrics.grain, limits.grain_max),
        ("haze", metrics.haze, limits.haze_max),
        ("bloom", metrics.bloom, limits.bloom_max),
    ]
    checks = [
        LookCheck(metric=m, value=value, limit=limit, passed=value <= limit)
        for m, value, limit in rows
    ]
    return LookQAReport(
        look_id=look.id,
        look_version=look.version,
        checks=checks,
        passed=all(c.passed for c in checks),
    )


# --- Frame measurements (#27) -------------------------------------------------

HAZE_CONTRAST = 0.2
"""RMS luma contrast at or above which a frame shows no veil, however high its floor."""

CLIP_LEVEL = 0.98
"""Luma at or above which a pixel counts as near-clipped."""

GRAIN_CROP = 256
"""Grain is measured on a centred crop at most this many pixels square, at full resolution."""

FLICKER_MAX = 0.02
"""Standard deviation of frame-to-frame mean-luma steps within a shot that reads as flicker."""

EXPOSURE_JUMP = 0.15
"""Mean-luma change across a cut above which the cut jumps in exposure."""


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, v))


def haze(frame: Gray) -> float:
    """A veil: a raised black floor (5th-percentile luma) that also flattens contrast."""
    work = downscale(frame)
    floor = percentile(work.pixels, 0.05)
    m = mean(work.pixels)
    rms = math.sqrt(mean([(v - m) ** 2 for v in work.pixels]))
    return _clamp01(floor * (1.0 - min(1.0, rms / HAZE_CONTRAST)))


def _box_blur_residuals(frame: Gray) -> list[float]:
    w, h = frame.width, frame.height
    px = frame.pixels
    out: list[float] = []
    for y in range(1, h - 1):
        for x in range(1, w - 1):
            total = 0.0
            for dy in (-1, 0, 1):
                row = (y + dy) * w + x
                total += px[row - 1] + px[row] + px[row + 1]
            out.append(abs(px[y * w + x] - total / 9.0))
    return out


def grain(frame: Gray) -> float:
    """High-frequency energy: the median residual against a 3x3 box blur (edges are sparse)."""
    cw, ch = min(frame.width, GRAIN_CROP), min(frame.height, GRAIN_CROP)
    if cw < 3 or ch < 3:
        return 0.0
    x0, y0 = (frame.width - cw) // 2, (frame.height - ch) // 2
    crop = frame.crop(
        BBox(x=x0 / frame.width, y=y0 / frame.height, w=cw / frame.width, h=ch / frame.height)
    )
    return _clamp01(4.0 * percentile(_box_blur_residuals(crop), 0.5))


def bloom(frame: Gray) -> float:
    """Share of the frame's pixels that are near-clipped and wrapped in a soft halo.

    A near-clipped pixel blooms when at least a quarter of its 5x5 neighbourhood
    is mid-bright (0.5 up to the clip level): a glow, not a hard edge against
    a dark or white background.
    """
    work = downscale(frame)
    w, h = work.width, work.height
    halo = 0
    for y in range(h):
        for x in range(w):
            if work.at(x, y) < CLIP_LEVEL:
                continue
            soft = total = 0
            for ny in range(max(0, y - 2), min(h, y + 3)):
                for nx in range(max(0, x - 2), min(w, x + 3)):
                    total += 1
                    soft += 0.5 <= work.at(nx, ny) < CLIP_LEVEL
            if soft * 4 >= total:
                halo += 1
    return _clamp01(halo / (w * h))


def measure_look_metrics(frames: Sequence[Gray]) -> LookMetrics:
    """Mean haze, grain and bloom of ``frames``; all zero when there are none."""
    if not frames:
        return LookMetrics(grain=0.0, haze=0.0, bloom=0.0)
    return LookMetrics(
        grain=round(mean([grain(f) for f in frames]), 4),
        haze=round(mean([haze(f) for f in frames]), 4),
        bloom=round(mean([bloom(f) for f in frames]), 4),
    )


def flicker(frames: Sequence[Gray]) -> float:
    """Standard deviation of frame-to-frame mean-luma steps (a steady fade scores 0)."""
    means = [mean(downscale(f).pixels) for f in frames]
    steps = [b - a for a, b in zip(means, means[1:], strict=False)]
    if len(steps) < 2:
        return 0.0
    m = mean(steps)
    return math.sqrt(mean([(s - m) ** 2 for s in steps]))


def exposure_jumps(
    frames: Sequence[Gray], cut_indices: Sequence[int], *, threshold: float = EXPOSURE_JUMP
) -> list[tuple[int, float]]:
    """``(cut frame index, luma delta)`` of each cut whose luma change exceeds ``threshold``."""
    means = [mean(downscale(f).pixels) for f in frames]
    out: list[tuple[int, float]] = []
    for cut in cut_indices:
        if 0 < cut < len(means):
            delta = abs(means[cut] - means[cut - 1])
            if delta > threshold:
                out.append((cut, round(delta, 4)))
    return out
