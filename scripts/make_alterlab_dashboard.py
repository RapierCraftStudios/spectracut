# SPDX-License-Identifier: AGPL-3.0-or-later
"""Generate the AlterLab dashboard UI screen ``brands/alterlab/assets/ui/dashboard.png``.

The image is original AlterLab artwork drawn from flat blocks in the AlterLab
palette: no text, no logos, no third-party or OEM marks. It is high contrast so
the screen stays legible on a rendered device. The generator uses only the
standard library (``zlib`` + ``struct``) and no clocks or randomness, so running
it reproduces the committed bytes exactly.

Usage::

    python scripts/make_alterlab_dashboard.py            # write the PNG
    python scripts/make_alterlab_dashboard.py --check P  # exit 0 if P matches, else 1
"""

from __future__ import annotations

import argparse
import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "brands" / "alterlab" / "assets" / "ui" / "dashboard.png"

WIDTH, HEIGHT = 1280, 800

#: Indexed palette, matching ``brands/alterlab/brand-kit.yaml`` swatches.
PALETTE: tuple[tuple[int, int, int], ...] = (
    (0x0B, 0x12, 0x20),  # 0 ink: sidebar and top bar
    (0xF5, 0xF7, 0xFA),  # 1 surface: page background
    (0xFF, 0xFF, 0xFF),  # 2 card
    (0x2F, 0x6B, 0xFF),  # 3 accent
    (0x18, 0xB4, 0x7B),  # 4 signal
    (0xF2, 0xA3, 0x3A),  # 5 warm
    (0xC9, 0xD2, 0xE0),  # 6 rule
    (0x3A, 0x47, 0x5C),  # 7 slate: glyph blocks on cards
)

Rect = tuple[int, int, int, int, int]
"""``(x, y, width, height, palette index)``."""


def layout() -> list[Rect]:
    """The dashboard as flat rectangles, painted in order."""
    rects: list[Rect] = [(0, 0, WIDTH, HEIGHT, 1)]
    rects.append((0, 0, 220, HEIGHT, 0))  # sidebar
    rects.append((220, 0, WIDTH - 220, 72, 0))  # top bar
    rects.append((32, 28, 48, 48, 3))  # sidebar mark (plain square)
    for i in range(6):  # sidebar nav items
        y = 128 + i * 64
        rects.append((32, y, 156, 20, 3 if i == 0 else 7))
    rects.append((WIDTH - 72, 20, 32, 32, 4))  # status dot block
    # KPI cards
    accents = (3, 4, 5)
    for i in range(3):
        x = 256 + i * 336
        rects.append((x, 104, 304, 152, 2))
        rects.append((x, 104, 304, 8, accents[i]))
        rects.append((x + 24, 140, 120, 16, 7))
        rects.append((x + 24, 180, 200 - i * 40, 44, accents[i]))
    # chart card with bars
    rects.append((256, 288, 640, 480, 2))
    rects.append((280, 312, 180, 18, 7))
    rects.append((280, 720, 592, 4, 6))
    heights = (180, 260, 220, 320, 280, 360, 300, 340)
    for i, h in enumerate(heights):
        rects.append((304 + i * 70, 720 - h, 44, h, 3 if i % 2 == 0 else 4))
    # activity list card
    rects.append((928, 288, 320, 480, 2))
    rects.append((952, 312, 140, 18, 7))
    for i in range(6):
        y = 360 + i * 64
        rects.append((952, y, 28, 28, accents[i % 3]))
        rects.append((996, y + 6, 220 - (i % 3) * 40, 16, 7))
        rects.append((952, y + 48, 272, 2, 6))
    return rects


def pixels() -> list[bytearray]:
    rows = [bytearray(WIDTH) for _ in range(HEIGHT)]
    for x, y, w, h, colour in layout():
        for row in rows[y : y + h]:
            row[x : x + w] = bytes([colour]) * w
    return rows


def _chunk(kind: bytes, data: bytes) -> bytes:
    body = kind + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def render() -> bytes:
    """The PNG bytes: 8-bit indexed colour, no ancillary chunks, no timestamps."""
    header = struct.pack(">IIBBBBB", WIDTH, HEIGHT, 8, 3, 0, 0, 0)
    plte = b"".join(bytes(rgb) for rgb in PALETTE)
    raw = b"".join(b"\x00" + bytes(row) for row in pixels())
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"PLTE", plte)
        + _chunk(b"IDAT", zlib.compress(raw, 9))
        + _chunk(b"IEND", b"")
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", type=Path, metavar="PATH", help="compare PATH, do not write")
    args = parser.parse_args(argv)
    data = render()
    if args.check is not None:
        try:
            same = args.check.read_bytes() == data
        except OSError as exc:
            print(f"cannot read {args.check}: {exc.strerror or exc}", file=sys.stderr)
            return 1
        if not same:
            print(f"{args.check} differs from the generated dashboard", file=sys.stderr)
            return 1
        return 0
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_bytes(data)
    print(f"wrote {OUTPUT.relative_to(ROOT)} ({len(data)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
