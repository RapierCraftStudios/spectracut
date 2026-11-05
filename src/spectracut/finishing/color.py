# SPDX-License-Identifier: AGPL-3.0-or-later
"""Scene-referred colour: the production's OCIO pipeline and the colour maths QA needs.

Blender renders scene-linear Rec.709 into EXR; finishing applies the Look's
grade, then the OCIO view transform (``AgX`` by default, ``ACES 2.0`` per
project on Blender 5.x) and the display (``Rec.1886`` for the SDR Rec.709
master). Review playback uses the same ``{display, view, look}`` triple
(:meth:`ColorPipeline.playback`).

This module is pure Python (no ``bpy``, no numpy): it builds recipe steps and
measures 8-bit masters against a 24-patch ColorChecker in CIELAB (ΔE2000).
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import Field, model_validator

from spectracut.harness.imagecmp import decode_png
from spectracut.spec._base import NonEmptyStr, StrictModel
from spectracut.spec.brand import CDL
from spectracut.spec.recipe import RecipeStep

RGB = tuple[float, float, float]
Lab = tuple[float, float, float]

ViewTransform = Literal["AgX", "ACES 2.0", "Standard"]
Display = Literal["Rec.1886", "sRGB"]

ACES2_MIN_BLENDER = (5, 0)
"""Blender's bundled OCIO config gains the ACES 2.0 view in 5.0."""

REC709_LUMA: RGB = (0.2126, 0.7152, 0.0722)

_VERSION = re.compile(r"^\s*(\d+)\.(\d+)(?:\.\d+)?\s*$")


def _blender_version(version: str) -> tuple[int, int]:
    match = _VERSION.match(version)
    if match is None:
        raise ValueError(f"Blender version {version!r} is not MAJOR.MINOR[.PATCH]")
    return int(match.group(1)), int(match.group(2))


class ColorPipeline(StrictModel):
    """A production's colour pipeline: OCIO view transform, display and optional look."""

    view_transform: ViewTransform = "AgX"
    display: Display = "Rec.1886"
    """``Rec.1886`` (inverse BT.1886, gamma 2.4) is the SDR Rec.709 master."""
    look: NonEmptyStr | None = Field(
        default=None, description="OCIO look name, e.g. 'AgX - Base Contrast'"
    )

    @model_validator(mode="after")
    def _look_matches_view(self) -> ColorPipeline:
        if (
            self.look is not None
            and self.look.startswith("AgX - ")
            and self.view_transform != "AgX"
        ):
            raise ValueError(f"OCIO look {self.look!r} needs the AgX view transform")
        return self

    def require_blender(self, version: str) -> None:
        """Refuse ``ACES 2.0`` on a Blender older than 5.0, naming ``version``."""
        if self.view_transform == "ACES 2.0" and _blender_version(version) < ACES2_MIN_BLENDER:
            raise ValueError(
                f"view transform 'ACES 2.0' needs Blender 5.0 or newer; "
                f"this production renders on Blender {version}"
            )

    def render_steps(self, start: int = 0) -> list[RecipeStep]:
        """Recipe steps that set the display, view transform and look on the scene."""
        calls = [
            ("view_settings.display_device", self.display),
            ("view_settings.view_transform", self.view_transform),
            ("view_settings.look", self.look or "None"),
        ]
        return [
            RecipeStep(
                index=start + i, tool="render.set_property", args={"property": p, "value": v}
            )
            for i, (p, v) in enumerate(calls)
        ]

    def playback(self) -> dict[str, str]:
        """The OCIO ``{display, view, look}`` triple review playback must use."""
        return {"display": self.display, "view": self.view_transform, "look": self.look or "None"}

    def eotf(self, encoded: float) -> float:
        """Display-encoded value (0..1) to linear light under this pipeline's display."""
        return rec709_decode(encoded) if self.display == "Rec.1886" else srgb_decode(encoded)


# ---- transfer functions ---------------------------------------------------------


def rec709_encode(linear: float) -> float:
    """Inverse BT.1886 (gamma 2.4): linear 0..1 to the Rec.709 SDR master's code value."""
    return min(max(linear, 0.0), 1.0) ** (1.0 / 2.4)


def rec709_decode(encoded: float) -> float:
    """BT.1886 EOTF (gamma 2.4, black 0): code value 0..1 to linear light."""
    return min(max(encoded, 0.0), 1.0) ** 2.4


def srgb_encode(linear: float) -> float:
    """The piecewise sRGB inverse EOTF (IEC 61966-2-1)."""
    v = min(max(linear, 0.0), 1.0)
    return 12.92 * v if v <= 0.0031308 else 1.055 * v ** (1.0 / 2.4) - 0.055


def srgb_decode(encoded: float) -> float:
    """The piecewise sRGB EOTF (IEC 61966-2-1)."""
    v = min(max(encoded, 0.0), 1.0)
    return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4


# ---- grades ---------------------------------------------------------------------


def apply_cdl(rgb: Sequence[float], cdl: CDL) -> RGB:
    """ASC CDL: slope, offset, clamp negatives, power, then saturation (Rec.709 luma)."""
    sop = [
        max(c * s + o, 0.0) ** p
        for c, s, o, p in zip(rgb, cdl.slope, cdl.offset, cdl.power, strict=True)
    ]
    luma = sum(w * c for w, c in zip(REC709_LUMA, sop, strict=True))
    r, g, b = (luma + cdl.saturation * (c - luma) for c in sop)
    return (r, g, b)


@dataclass(frozen=True)
class CubeLUT:
    """A parsed ``.cube`` LUT; a 3D table is red-fastest, as the format orders it."""

    dimensions: Literal[1, 3]
    size: int
    table: tuple[RGB, ...]
    domain_min: RGB = (0.0, 0.0, 0.0)
    domain_max: RGB = (1.0, 1.0, 1.0)
    title: str = ""


def _triple(parts: list[str], line: int) -> RGB:
    if len(parts) != 3:
        raise ValueError(f".cube line {line}: expected 3 numbers, got {len(parts)}")
    try:
        r, g, b = (float(p) for p in parts)
    except ValueError:
        raise ValueError(f".cube line {line}: {' '.join(parts)!r} is not three numbers") from None
    if not all(math.isfinite(v) for v in (r, g, b)):
        raise ValueError(f".cube line {line}: values must be finite")
    return (r, g, b)


def parse_cube(text: str) -> CubeLUT:
    """Parse a 1D or 3D Adobe/Resolve ``.cube`` LUT; malformed input raises ValueError."""
    dims: Literal[1, 3] | None = None
    size = 0
    title = ""
    lo: RGB = (0.0, 0.0, 0.0)
    hi: RGB = (1.0, 1.0, 1.0)
    rows: list[RGB] = []
    for n, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        head, *rest = line.split()
        key = head.upper()
        if key == "TITLE":
            title = line[len(head) :].strip().strip('"')
        elif key in ("LUT_1D_SIZE", "LUT_3D_SIZE"):
            if dims is not None:
                raise ValueError(f".cube line {n}: a LUT declares one size")
            try:
                size = int(rest[0]) if len(rest) == 1 else -1
            except ValueError:
                size = -1
            if size < 2 or size > (65536 if key == "LUT_1D_SIZE" else 256):
                raise ValueError(f".cube line {n}: bad {key} {' '.join(rest)!r}")
            dims = 1 if key == "LUT_1D_SIZE" else 3
        elif key == "DOMAIN_MIN":
            lo = _triple(rest, n)
        elif key == "DOMAIN_MAX":
            hi = _triple(rest, n)
        elif key[0].isdigit() or key[0] in "+-.":
            rows.append(_triple(line.split(), n))
        else:
            raise ValueError(f".cube line {n}: unknown keyword {head!r}")
    if dims is None:
        raise ValueError(".cube has no LUT_1D_SIZE or LUT_3D_SIZE")
    want = size if dims == 1 else size**3
    if len(rows) != want:
        raise ValueError(f".cube declares {want} entries but has {len(rows)}")
    if any(h <= low for low, h in zip(lo, hi, strict=True)):
        raise ValueError(".cube DOMAIN_MAX must exceed DOMAIN_MIN on every channel")
    return CubeLUT(
        dimensions=dims, size=size, table=tuple(rows), domain_min=lo, domain_max=hi, title=title
    )


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _coord(value: float, lo: float, hi: float, size: int) -> tuple[int, int, float]:
    x = min(max((value - lo) / (hi - lo), 0.0), 1.0) * (size - 1)
    i = min(int(x), size - 2)
    return i, i + 1, x - i


def apply_cube(rgb: Sequence[float], lut: CubeLUT) -> RGB:
    """Apply ``lut`` to ``rgb``: per-channel linear (1D) or trilinear (3D) interpolation."""
    n = lut.size
    coords = [
        _coord(c, lo, hi, n) for c, lo, hi in zip(rgb, lut.domain_min, lut.domain_max, strict=True)
    ]
    t = lut.table
    if lut.dimensions == 1:
        out = [_lerp(t[i0][ch], t[i1][ch], f) for ch, (i0, i1, f) in enumerate(coords)]
        return (out[0], out[1], out[2])
    (r0, r1, fr), (g0, g1, fg), (b0, b1, fb) = coords

    def at(r: int, g: int, b: int) -> RGB:
        return t[r + n * g + n * n * b]

    res: list[float] = []
    for ch in range(3):
        c00 = _lerp(at(r0, g0, b0)[ch], at(r1, g0, b0)[ch], fr)
        c10 = _lerp(at(r0, g1, b0)[ch], at(r1, g1, b0)[ch], fr)
        c01 = _lerp(at(r0, g0, b1)[ch], at(r1, g0, b1)[ch], fr)
        c11 = _lerp(at(r0, g1, b1)[ch], at(r1, g1, b1)[ch], fr)
        res.append(_lerp(_lerp(c00, c10, fg), _lerp(c01, c11, fg), fb))
    return (res[0], res[1], res[2])


# ---- CIELAB and ΔE2000 ----------------------------------------------------------

_RGB709_TO_XYZ = (
    (0.4124564, 0.3575761, 0.1804375),
    (0.2126729, 0.7151522, 0.0721750),
    (0.0193339, 0.1191920, 0.9503041),
)
D65_WHITE: RGB = (0.95047, 1.0, 1.08883)


def linear_to_xyz(rgb: Sequence[float]) -> RGB:
    """Linear Rec.709 (D65) to CIE XYZ."""
    x, y, z = (sum(m * c for m, c in zip(row, rgb, strict=True)) for row in _RGB709_TO_XYZ)
    return (x, y, z)


def xyz_to_lab(xyz: Sequence[float], white: Sequence[float] = D65_WHITE) -> Lab:
    """CIE XYZ to CIELAB relative to ``white``."""
    eps, kappa = 216 / 24389, 24389 / 27

    def f(t: float) -> float:
        return t ** (1 / 3) if t > eps else (kappa * t + 16) / 116

    fx, fy, fz = (f(c / w) for c, w in zip(xyz, white, strict=True))
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def linear_to_lab(rgb: Sequence[float]) -> Lab:
    return xyz_to_lab(linear_to_xyz(rgb))


def delta_e2000(lab1: Sequence[float], lab2: Sequence[float]) -> float:
    """CIEDE2000 colour difference (Sharma, Wu and Dalal 2005), kL = kC = kH = 1."""
    l1, a1, b1 = lab1
    l2, a2, b2 = lab2
    c_bar = (math.hypot(a1, b1) + math.hypot(a2, b2)) / 2
    g = 0.5 * (1 - math.sqrt(c_bar**7 / (c_bar**7 + 25**7)))
    a1p, a2p = (1 + g) * a1, (1 + g) * a2
    c1p, c2p = math.hypot(a1p, b1), math.hypot(a2p, b2)
    h1p = math.degrees(math.atan2(b1, a1p)) % 360 if c1p else 0.0
    h2p = math.degrees(math.atan2(b2, a2p)) % 360 if c2p else 0.0
    dl, dc = l2 - l1, c2p - c1p
    if c1p * c2p == 0:
        dh = 0.0
    elif abs(h2p - h1p) <= 180:
        dh = h2p - h1p
    elif h2p - h1p > 180:
        dh = h2p - h1p - 360
    else:
        dh = h2p - h1p + 360
    d_hp = 2 * math.sqrt(c1p * c2p) * math.sin(math.radians(dh / 2))
    l_bar, c_bar_p = (l1 + l2) / 2, (c1p + c2p) / 2
    if c1p * c2p == 0:
        h_bar = h1p + h2p
    elif abs(h1p - h2p) <= 180:
        h_bar = (h1p + h2p) / 2
    elif h1p + h2p < 360:
        h_bar = (h1p + h2p + 360) / 2
    else:
        h_bar = (h1p + h2p - 360) / 2
    t = (
        1
        - 0.17 * math.cos(math.radians(h_bar - 30))
        + 0.24 * math.cos(math.radians(2 * h_bar))
        + 0.32 * math.cos(math.radians(3 * h_bar + 6))
        - 0.20 * math.cos(math.radians(4 * h_bar - 63))
    )
    d_theta = 30 * math.exp(-(((h_bar - 275) / 25) ** 2))
    r_c = 2 * math.sqrt(c_bar_p**7 / (c_bar_p**7 + 25**7))
    s_l = 1 + 0.015 * (l_bar - 50) ** 2 / math.sqrt(20 + (l_bar - 50) ** 2)
    s_c = 1 + 0.045 * c_bar_p
    s_h = 1 + 0.015 * c_bar_p * t
    r_t = -math.sin(math.radians(2 * d_theta)) * r_c
    return math.sqrt(
        (dl / s_l) ** 2 + (dc / s_c) ** 2 + (d_hp / s_h) ** 2 + r_t * (dc / s_c) * (d_hp / s_h)
    )


# ---- the colour chart -----------------------------------------------------------

# ColorChecker Classic (24 patches), reading order. Source: X-Rite's published
# sRGB (D65) values for the post-November-2014 formulation, as tabulated by
# BabelColor and Wikipedia ("ColorChecker"). They are linearised with the sRGB
# EOTF below, giving the linear Rec.709 references (Rec.709 and sRGB share
# primaries and white point).
_COLORCHECKER_SRGB8: tuple[tuple[str, tuple[int, int, int]], ...] = (
    ("dark_skin", (115, 82, 68)),
    ("light_skin", (194, 150, 130)),
    ("blue_sky", (98, 122, 157)),
    ("foliage", (87, 108, 67)),
    ("blue_flower", (133, 128, 177)),
    ("bluish_green", (103, 189, 170)),
    ("orange", (214, 126, 44)),
    ("purplish_blue", (80, 91, 166)),
    ("moderate_red", (193, 90, 99)),
    ("purple", (94, 60, 108)),
    ("yellow_green", (157, 188, 64)),
    ("orange_yellow", (224, 163, 46)),
    ("blue", (56, 61, 150)),
    ("green", (70, 148, 73)),
    ("red", (175, 54, 60)),
    ("yellow", (231, 199, 31)),
    ("magenta", (187, 86, 149)),
    ("cyan", (8, 133, 161)),
    ("white_9_5", (243, 243, 242)),
    ("neutral_8", (200, 200, 200)),
    ("neutral_6_5", (160, 160, 160)),
    ("neutral_5", (122, 122, 121)),
    ("neutral_3_5", (85, 85, 85)),
    ("black_2", (52, 52, 52)),
)

COLORCHECKER_24: tuple[tuple[str, RGB], ...] = tuple(
    (name, (srgb_decode(r / 255), srgb_decode(g / 255), srgb_decode(b / 255)))
    for name, (r, g, b) in _COLORCHECKER_SRGB8
)
"""24 named patches with their linear Rec.709 reference values."""

CHART_COLUMNS, CHART_ROWS = 6, 4


@dataclass(frozen=True)
class PatchRect:
    """A patch's pixel rectangle: ``x0 <= x < x1``, ``y0 <= y < y1``."""

    name: str
    x0: int
    y0: int
    x1: int
    y1: int

    def inner(self) -> PatchRect:
        """The centre 50% of the patch (the sampling area)."""
        w, h = self.x1 - self.x0, self.y1 - self.y0
        return PatchRect(
            self.name,
            self.x0 + w // 4,
            self.y0 + h // 4,
            self.x0 + w // 4 + max(w // 2, 1),
            self.y0 + h // 4 + max(h // 2, 1),
        )


def chart_layout(width: int, height: int) -> list[PatchRect]:
    """The 6x4 grid of ColorChecker patches over a ``width`` x ``height`` frame."""
    if width < CHART_COLUMNS or height < CHART_ROWS:
        raise ValueError(f"a {width}x{height} frame is too small for a 6x4 chart")
    rects: list[PatchRect] = []
    for i, (name, _) in enumerate(COLORCHECKER_24):
        col, row = i % CHART_COLUMNS, i // CHART_COLUMNS
        rects.append(
            PatchRect(
                name,
                col * width // CHART_COLUMNS,
                row * height // CHART_ROWS,
                (col + 1) * width // CHART_COLUMNS,
                (row + 1) * height // CHART_ROWS,
            )
        )
    return rects


class PatchMeasure(StrictModel):
    name: str
    measured: tuple[float, float, float]
    """Mean linear Rec.709 of the patch's inner 50%."""
    delta_e: float


class ChartReport(StrictModel):
    """Per-patch ΔE2000 of a master against :data:`COLORCHECKER_24`."""

    patches: list[PatchMeasure]
    max_delta_e: float
    mean_delta_e: float

    def passed(self, limit: float = 2.0) -> bool:
        return self.max_delta_e < limit


def measure_chart(
    png_bytes: bytes, layout: Sequence[PatchRect], pipeline: ColorPipeline
) -> ChartReport:
    """Measure an 8-bit master of the chart: linearise with the display EOTF, compare in Lab."""
    image = decode_png(png_bytes)
    if image.channels < 3:
        raise ValueError(f"chart master has {image.channels} channel(s); RGB is required")
    refs = dict(COLORCHECKER_24)
    lut = [pipeline.eotf(v / 255) for v in range(256)]
    stride = image.width * image.channels
    results: list[PatchMeasure] = []
    for rect in layout:
        if rect.name not in refs:
            raise ValueError(f"layout patch {rect.name!r} is not a ColorChecker patch")
        box = rect.inner()
        if box.x1 > image.width or box.y1 > image.height:
            raise ValueError(
                f"patch {rect.name!r} lies outside the {image.width}x{image.height} master"
            )
        sums = [0.0, 0.0, 0.0]
        count = 0
        for y in range(box.y0, box.y1):
            row = y * stride
            for x in range(box.x0, box.x1):
                px = row + x * image.channels
                for ch in range(3):
                    sums[ch] += lut[image.pixels[px + ch]]
                count += 1
        mean = (sums[0] / count, sums[1] / count, sums[2] / count)
        de = delta_e2000(linear_to_lab(mean), linear_to_lab(refs[rect.name]))
        results.append(PatchMeasure(name=rect.name, measured=mean, delta_e=de))
    if not results:
        raise ValueError("chart layout has no patches")
    values = [p.delta_e for p in results]
    return ChartReport(
        patches=results, max_delta_e=max(values), mean_delta_e=sum(values) / len(values)
    )
