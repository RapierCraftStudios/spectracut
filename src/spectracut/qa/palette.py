# SPDX-License-Identifier: AGPL-3.0-or-later
"""Palette QA: a rendered frame's colours against the brand swatches.

Pure Python (no numpy). A frame is downscaled to at most :data:`MAX_WIDTH` pixels
wide, then measured in CIE L*a*b* (sRGB display, D65):

- ``background``: the median colour of the outer border band against its nearest
  swatch by CIEDE2000;
- ``lifted_blacks``: when that swatch is the palette's darkest (a brand void), how
  far the background's L* sits above it;
- ``blown_highlights``: the share of pixels clipped to white in every channel;
- ``swatch:<name>``: for each chromatic swatch, the most saturated pixels of its hue
  against the swatch (skipped when the hue is not in frame).

Every failed check carries a measured hex, the target swatch and advice the scene
builder can act on (:meth:`PaletteReport.lines`).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from spectracut.finishing.color import (
    Lab,
    delta_e2000,
    hex_to_lab,
    linear_to_lab,
    srgb_decode,
    srgb_encode,
)
from spectracut.harness.imagecmp import ImageError, decode_png

DELTA_E_MAX = 10.0
"""CIEDE2000 limit for the background and every in-frame swatch."""
BLACK_LIFT_MAX = 6.0
"""Most L* a dark brand void may sit above its swatch."""
BLOWN_AREA_MAX = 0.02
"""Largest share of the frame that may be clipped white."""
BLOWN_LEVEL = 250
"""8-bit level every channel must reach for a pixel to count as blown."""
BORDER_BAND = 0.05
"""Width of the border band sampled for the background, as a share of each side."""
DARK_SWATCH_L_MAX = 25.0
"""Only a darkest swatch below this L* is treated as a void for ``lifted_blacks``."""
SWATCH_CHROMA_MIN = 30.0
"""Swatches at or above this C* get a ``swatch:<name>`` check."""
PIXEL_CHROMA_MIN = 15.0
"""Pixels below this C* never count towards a swatch."""
HUE_WINDOW = 25.0
"""Pixels within this many degrees of Lab hue of a swatch count towards it."""
SWATCH_AREA_MIN = 0.002
"""A swatch whose hue covers less of the frame than this is not in frame."""
TOP_CHROMA_SHARE = 0.25
"""The most saturated share of a swatch's pixels that is measured."""
LOW_CHROMA_RATIO = 0.7
"""Measured chroma below this share of the swatch's reads as washed out."""
MAX_WIDTH = 320
"""Frames are downscaled (nearest neighbour) to at most this width before measuring."""

Pixel = tuple[int, int, int]


@dataclass(frozen=True)
class RGBFrame:
    """A frame of 8-bit sRGB-encoded pixels, row-major."""

    width: int
    height: int
    pixels: tuple[Pixel, ...]

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0 or len(self.pixels) != self.width * self.height:
            raise ValueError("pixels do not match width x height")

    @classmethod
    def from_linear(cls, width: int, height: int, pixels: Sequence[Sequence[float]]) -> RGBFrame:
        """A frame from scene-linear RGB pixels, encoded for an sRGB display (clipped)."""
        encoded = tuple((_byte(p[0]), _byte(p[1]), _byte(p[2])) for p in pixels)
        return cls(width, height, encoded)

    def downscaled(self, max_width: int = MAX_WIDTH) -> RGBFrame:
        """This frame, nearest-neighbour downscaled to at most ``max_width`` wide."""
        if self.width <= max_width:
            return self
        w = max_width
        h = max(1, round(self.height * max_width / self.width))
        px = tuple(
            self.pixels[(y * self.height // h) * self.width + x * self.width // w]
            for y in range(h)
            for x in range(w)
        )
        return RGBFrame(w, h, px)


def _byte(linear: float) -> int:
    return max(0, min(255, round(srgb_encode(float(linear)) * 255)))


def load_rgb(png: bytes) -> RGBFrame | None:
    """An 8-bit PNG as an :class:`RGBFrame` (alpha dropped), or None if unreadable."""
    try:
        image = decode_png(png)
    except ImageError:
        return None
    n = image.channels
    data = image.pixels
    pixels: list[Pixel]
    if n >= 3:
        pixels = [(data[i], data[i + 1], data[i + 2]) for i in range(0, len(data), n)]
    else:
        pixels = [(data[i], data[i], data[i]) for i in range(0, len(data), n)]
    try:
        return RGBFrame(image.width, image.height, tuple(pixels))
    except ValueError:
        return None


@dataclass(frozen=True)
class PaletteCheck:
    """One palette measurement against its limit."""

    name: str
    passed: bool
    value: float | None
    limit: float
    measured_hex: str | None
    target: str | None
    """The swatch name measured against."""
    target_hex: str | None
    delta_e: float | None
    advice: str

    def line(self) -> str:
        """``palette <name>: measured #… vs <swatch> #…, dE 23.4 > 10; <advice>``."""
        parts: list[str] = []
        if self.measured_hex is not None and self.target is not None:
            parts.append(f"measured {self.measured_hex} vs {self.target} {self.target_hex}")
        if self.delta_e is not None:
            parts.append(f"dE {self.delta_e:.1f} > {self.limit:g}")
        elif self.name == "lifted_blacks" and self.value is not None:
            parts.append(f"L* +{self.value:.1f} > {self.limit:g}")
        elif self.name == "blown_highlights" and self.value is not None:
            parts.append(f"{self.value:.1%} of pixels blown > {self.limit:.0%}")
        return f"palette {self.name}: {', '.join(parts)}; {self.advice}"


@dataclass(frozen=True)
class PaletteReport:
    """Every palette check on one frame."""

    checks: list[PaletteCheck] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    def failures(self) -> list[PaletteCheck]:
        return [c for c in self.checks if not c.passed]

    def lines(self) -> list[str]:
        """One line per failed check, with the measurement and the fix."""
        return [c.line() for c in self.failures()]


def _hex(pixel: Sequence[float]) -> str:
    return "#" + "".join(f"{max(0, min(255, round(v))):02x}" for v in pixel[:3])


def _lab(pixel: Pixel, cache: dict[Pixel, Lab]) -> Lab:
    lab = cache.get(pixel)
    if lab is None:
        lab = linear_to_lab([srgb_decode(v / 255.0) for v in pixel])
        cache[pixel] = lab
    return lab


def _chroma(lab: Sequence[float]) -> float:
    return math.hypot(lab[1], lab[2])


def _hue(lab: Sequence[float]) -> float:
    return math.degrees(math.atan2(lab[2], lab[1])) % 360.0


def _median(values: list[int]) -> float:
    s = sorted(values)
    mid = len(s) // 2
    return float(s[mid]) if len(s) % 2 else (s[mid - 1] + s[mid]) / 2.0


def _border(frame: RGBFrame) -> list[Pixel]:
    bw = max(1, round(frame.width * BORDER_BAND))
    bh = max(1, round(frame.height * BORDER_BAND))
    return [
        frame.pixels[y * frame.width + x]
        for y in range(frame.height)
        for x in range(frame.width)
        if x < bw or x >= frame.width - bw or y < bh or y >= frame.height - bh
    ]


def palette_qa(frame: RGBFrame, swatches: Mapping[str, str]) -> PaletteReport:
    """Measure ``frame`` against the brand ``swatches`` (name to ``#rrggbb``)."""
    if not swatches:
        return PaletteReport([])
    frame = frame.downscaled()
    cache: dict[Pixel, Lab] = {}
    labs = {name: hex_to_lab(h) for name, h in swatches.items()}
    checks: list[PaletteCheck] = []

    border = _border(frame)
    bg_rgb = tuple(_median([p[c] for p in border]) for c in range(3))
    bg_hex = _hex(bg_rgb)
    bg_lab = linear_to_lab([srgb_decode(v / 255.0) for v in bg_rgb])
    near = min(labs, key=lambda n: delta_e2000(bg_lab, labs[n]))
    de = delta_e2000(bg_lab, labs[near])
    bg_ok = de <= DELTA_E_MAX
    checks.append(
        PaletteCheck(
            "background",
            bg_ok,
            round(de, 2),
            DELTA_E_MAX,
            bg_hex,
            near,
            swatches[near],
            round(de, 2),
            "on palette"
            if bg_ok
            else f"set the world with world.set_gradient brand_bottom/brand_top "
            f"'{swatches[near]}' at strength 1 and keep backdrops on brand_color",
        )
    )

    darkest = min(labs, key=lambda n: labs[n][0])
    if near == darkest and labs[darkest][0] < DARK_SWATCH_L_MAX:
        lift = bg_lab[0] - labs[darkest][0]
        lift_ok = lift <= BLACK_LIFT_MAX
        checks.append(
            PaletteCheck(
                "lifted_blacks",
                lift_ok,
                round(lift, 2),
                BLACK_LIFT_MAX,
                bg_hex,
                darkest,
                swatches[darkest],
                None,
                "void at its swatch"
                if lift_ok
                else f"the void is lifted: lower world strength to 1 or less, remove "
                f"world.set_haze, keep view_settings.exposure at 0 and set the world "
                f"with world.set_gradient brand_bottom/brand_top '{swatches[darkest]}'",
            )
        )

    total = len(frame.pixels)
    blown = sum(1 for p in frame.pixels if min(p) >= BLOWN_LEVEL) / total
    blown_ok = blown <= BLOWN_AREA_MAX
    checks.append(
        PaletteCheck(
            "blown_highlights",
            blown_ok,
            round(blown, 4),
            BLOWN_AREA_MAX,
            None,
            None,
            None,
            None,
            "highlights hold"
            if blown_ok
            else "lower emission_strength (at most 1 with brand_color) or light energy "
            "so cores keep their hue instead of clipping to white",
        )
    )

    pixel_labs = [(p, _lab(p, cache)) for p in frame.pixels]
    for name, target in labs.items():
        target_c = _chroma(target)
        if target_c < SWATCH_CHROMA_MIN:
            continue
        target_h = _hue(target)
        matches = [
            (p, lab)
            for p, lab in pixel_labs
            if _chroma(lab) >= PIXEL_CHROMA_MIN
            and abs((_hue(lab) - target_h + 180.0) % 360.0 - 180.0) <= HUE_WINDOW
            and delta_e2000(lab, bg_lab) > DELTA_E_MAX
        ]
        check_name = f"swatch:{name}"
        if len(matches) < SWATCH_AREA_MIN * total:
            checks.append(
                PaletteCheck(
                    check_name,
                    True,
                    None,
                    DELTA_E_MAX,
                    None,
                    name,
                    swatches[name],
                    None,
                    "not in frame",
                )
            )
            continue
        matches.sort(key=lambda m: _chroma(m[1]), reverse=True)
        top = matches[: max(1, round(len(matches) * TOP_CHROMA_SHARE))]
        mean_lab = tuple(sum(m[1][i] for m in top) / len(top) for i in range(3))
        mean_rgb = tuple(sum(m[0][i] for m in top) / len(top) for i in range(3))
        de = delta_e2000(mean_lab, target)
        ok = de <= DELTA_E_MAX
        measured_c = _chroma(mean_lab)
        advice = f"measured C* {measured_c:.0f} vs swatch C* {target_c:.0f}"
        if measured_c < LOW_CHROMA_RATIO * target_c:
            advice += (
                f"; washed out: use brand_color '{swatches[name]}' on the emissive preset "
                f"with emission_strength at most 1"
            )
        elif not ok:
            advice += f"; use brand_color '{swatches[name]}' instead of a hand-mixed colour"
        checks.append(
            PaletteCheck(
                check_name,
                ok,
                round(de, 2),
                DELTA_E_MAX,
                _hex(mean_rgb),
                name,
                swatches[name],
                round(de, 2),
                advice,
            )
        )
    return PaletteReport(checks)
