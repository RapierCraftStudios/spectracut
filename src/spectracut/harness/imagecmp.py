# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stdlib PNG decoding and frame comparison for replay checks.

Supports what Blender writes for previews: 8-bit greyscale, RGB, grey+alpha and
RGBA, non-interlaced, with any of the five PNG row filters.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_CHANNELS = {0: 1, 2: 3, 4: 2, 6: 4}


class ImageError(ValueError):
    """The bytes are not a PNG this module can decode."""


@dataclass(frozen=True)
class Image:
    width: int
    height: int
    channels: int
    pixels: bytes
    """Row-major samples, ``channels`` bytes per pixel, no filter bytes."""


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def decode_png(data: bytes) -> Image:
    """Decode an 8-bit non-interlaced PNG."""
    if not data.startswith(_SIGNATURE):
        raise ImageError("not a PNG")
    pos, idat, header = len(_SIGNATURE), bytearray(), None
    while pos + 8 <= len(data):
        length, kind = struct.unpack(">I4s", data[pos : pos + 8])
        body = data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat += body
        elif kind == b"IEND":
            break
    if header is None:
        raise ImageError("PNG has no IHDR")
    width, height, depth, color, _, _, interlace = header
    if depth != 8 or color not in _CHANNELS or interlace:
        raise ImageError(f"unsupported PNG (depth {depth}, colour {color}, interlace {interlace})")
    channels = _CHANNELS[color]
    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error as exc:
        raise ImageError(f"corrupt PNG data: {exc}") from None
    stride = width * channels
    if len(raw) != height * (stride + 1):
        raise ImageError("PNG data size does not match its header")
    out = bytearray(height * stride)
    prev = bytearray(stride)
    for y in range(height):
        ftype = raw[y * (stride + 1)]
        row = bytearray(raw[y * (stride + 1) + 1 : (y + 1) * (stride + 1)])
        for x in range(stride):
            a = row[x - channels] if x >= channels else 0
            b = prev[x]
            c = prev[x - channels] if x >= channels else 0
            if ftype == 1:
                row[x] = (row[x] + a) & 0xFF
            elif ftype == 2:
                row[x] = (row[x] + b) & 0xFF
            elif ftype == 3:
                row[x] = (row[x] + (a + b) // 2) & 0xFF
            elif ftype == 4:
                row[x] = (row[x] + _paeth(a, b, c)) & 0xFF
            elif ftype != 0:
                raise ImageError(f"unknown PNG filter {ftype}")
        out[y * stride : (y + 1) * stride] = row
        prev = row
    return Image(width, height, channels, bytes(out))


def mean_abs_diff(a: bytes, b: bytes) -> float:
    """Mean absolute sample difference of two PNGs, 0 (same) to 1; 1 if sizes differ."""
    ia, ib = decode_png(a), decode_png(b)
    if (ia.width, ia.height, ia.channels) != (ib.width, ib.height, ib.channels):
        return 1.0
    if not ia.pixels:
        return 0.0
    total = sum(abs(x - y) for x, y in zip(ia.pixels, ib.pixels, strict=True))
    return total / (255.0 * len(ia.pixels))


def frames_match(a: bytes, b: bytes, tolerance: float = 0.01) -> bool:
    """Whether two frames differ by at most ``tolerance`` (mean absolute difference)."""
    return mean_abs_diff(a, b) <= tolerance
