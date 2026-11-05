# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared QA types and pixel helpers: a check row, boxes, greyscale frames and PNG I/O.

Frames are decoded with the stdlib decoder in :mod:`spectracut.harness.imagecmp`
and reduced to Rec. 709 luma in 0..1 (gamma-encoded); checks that need linear
light (contrast) convert with :func:`srgb_to_linear`.
"""

from __future__ import annotations

import math
import struct
import zlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import Field

from spectracut.harness.imagecmp import Image, ImageError, decode_png
from spectracut.spec._base import StrictModel

Status = Literal["pass", "fail", "skipped"]

WORK_WIDTH = 160
"""Frames are box-averaged down to at most this width before whole-frame metrics."""


class BBox(StrictModel):
    """A box as fractions of the frame: ``x, y`` top-left, ``w, h`` size."""

    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    w: float = Field(ge=0.0, le=1.0)
    h: float = Field(ge=0.0, le=1.0)


class QACheck(StrictModel):
    """One QA result row; ``frame`` and ``bbox`` locate a failure for its thumbnail."""

    name: str
    status: Status
    value: float | None = None
    limit: float | None = None
    detail: str = ""
    frame: int | None = None
    bbox: BBox | None = None
    thumbnail: str | None = None

    @property
    def failed(self) -> bool:
        return self.status == "fail"


@dataclass(frozen=True)
class Gray:
    """A greyscale frame: row-major luma samples in 0..1."""

    width: int
    height: int
    pixels: tuple[float, ...]

    def at(self, x: int, y: int) -> float:
        return self.pixels[y * self.width + x]

    def box(self, bbox: BBox) -> tuple[int, int, int, int]:
        """``(x0, y0, x1, y1)`` pixel bounds of ``bbox``, at least one pixel."""
        x0 = min(self.width - 1, int(bbox.x * self.width))
        y0 = min(self.height - 1, int(bbox.y * self.height))
        x1 = max(x0 + 1, min(self.width, math.ceil((bbox.x + bbox.w) * self.width)))
        y1 = max(y0 + 1, min(self.height, math.ceil((bbox.y + bbox.h) * self.height)))
        return x0, y0, x1, y1

    def crop(self, bbox: BBox) -> Gray:
        x0, y0, x1, y1 = self.box(bbox)
        rows = [self.pixels[y * self.width + x0 : y * self.width + x1] for y in range(y0, y1)]
        return Gray(x1 - x0, y1 - y0, tuple(v for row in rows for v in row))


def to_gray(image: Image) -> Gray:
    """Rec. 709 luma of ``image`` (alpha ignored)."""
    c, px = image.channels, image.pixels
    if c in (1, 2):
        values = tuple(px[i] / 255.0 for i in range(0, len(px), c))
    else:
        values = tuple(
            (0.2126 * px[i] + 0.7152 * px[i + 1] + 0.0722 * px[i + 2]) / 255.0
            for i in range(0, len(px), c)
        )
    return Gray(image.width, image.height, values)


def load_gray(png: bytes) -> Gray | None:
    """The luma of a PNG frame, or ``None`` when it cannot be decoded (EXR, 16-bit)."""
    try:
        return to_gray(decode_png(png))
    except ImageError:
        return None


def downscale(frame: Gray, max_width: int = WORK_WIDTH) -> Gray:
    """Box-average ``frame`` by an integer factor so it is at most ``max_width`` wide."""
    factor = max(1, math.ceil(frame.width / max_width))
    if factor == 1:
        return frame
    w, h = frame.width // factor, frame.height // factor
    if w == 0 or h == 0:
        return frame
    out: list[float] = []
    area = factor * factor
    for by in range(h):
        for bx in range(w):
            total = 0.0
            for y in range(by * factor, (by + 1) * factor):
                row = y * frame.width
                total += sum(frame.pixels[row + bx * factor : row + (bx + 1) * factor])
            out.append(total / area)
    return Gray(w, h, tuple(out))


def resample(frame: Gray, width: int, height: int) -> Gray:
    """Area-average ``frame`` onto a ``width`` x ``height`` grid (a coarse signature)."""
    table = Integral(frame)
    out: list[float] = []
    for j in range(height):
        y0, y1 = j * frame.height // height, max((j + 1) * frame.height // height, 1)
        for i in range(width):
            x0, x1 = i * frame.width // width, max((i + 1) * frame.width // width, 1)
            out.append(table.mean(x0, y0, max(x1, x0 + 1), max(y1, y0 + 1)))
    return Gray(width, height, tuple(out))


class Integral:
    """A summed-area table of a :class:`Gray` frame for O(1) box means."""

    def __init__(self, frame: Gray) -> None:
        w = frame.width
        self.width = w
        self.height = frame.height
        stride = w + 1
        table = [0.0] * (stride * (frame.height + 1))
        for y in range(frame.height):
            run = 0.0
            row = y * w
            for x in range(w):
                run += frame.pixels[row + x]
                table[(y + 1) * stride + x + 1] = table[y * stride + x + 1] + run
        self._t = table
        self._stride = stride

    def mean(self, x0: int, y0: int, x1: int, y1: int) -> float:
        s, t = self._stride, self._t
        total = t[y1 * s + x1] - t[y0 * s + x1] - t[y1 * s + x0] + t[y0 * s + x0]
        return total / ((x1 - x0) * (y1 - y0))


def srgb_to_linear(v: float) -> float:
    return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4


def percentile(values: Sequence[float], q: float) -> float:
    """The ``q`` (0..1) quantile of ``values`` by nearest rank."""
    ordered = sorted(values)
    if not ordered:
        return 0.0
    return ordered[min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))]


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _chunk(kind: bytes, body: bytes) -> bytes:
    crc = zlib.crc32(kind + body) & 0xFFFFFFFF
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", crc)


def encode_png(width: int, height: int, channels: int, pixels: bytes) -> bytes:
    """An 8-bit PNG (grey, grey+alpha, RGB or RGBA by ``channels``), filter 0 rows."""
    colour = {1: 0, 2: 4, 3: 2, 4: 6}[channels]
    stride = width * channels
    if len(pixels) != stride * height:
        raise ValueError("pixel data does not match width x height x channels")
    raw = b"".join(b"\x00" + pixels[y * stride : (y + 1) * stride] for y in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, colour, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(raw, 9))
        + _chunk(b"IEND", b"")
    )


def gray_png(frame: Gray) -> bytes:
    """``frame`` as a greyscale PNG."""
    data = bytes(max(0, min(255, round(v * 255))) for v in frame.pixels)
    return encode_png(frame.width, frame.height, 1, data)


def png_size(png: bytes) -> tuple[int, int] | None:
    """``(width, height)`` from a PNG's IHDR, without decoding."""
    if len(png) < 24 or not png.startswith(b"\x89PNG\r\n\x1a\n") or png[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", png[16:24])
    return int(width), int(height)
