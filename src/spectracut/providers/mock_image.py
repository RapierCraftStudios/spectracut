# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deterministic mock image provider for dev and CI (no network, no CLI)."""

from __future__ import annotations

import hashlib
import struct
import zlib
from pathlib import Path


def _chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


def solid_png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """A valid solid-colour RGB PNG built with the standard library."""
    row = b"\x00" + bytes(rgb) * width
    raw = row * height
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(raw, 9))
        + _chunk(b"IEND", b"")
    )


def _parse_size(size: str) -> tuple[int, int]:
    try:
        w, h = (int(part) for part in size.lower().split("x", 1))
    except ValueError as exc:
        raise ValueError(f"size must look like 1536x1024, got {size!r}") from exc
    if w <= 0 or h <= 0:
        raise ValueError(f"size must be positive, got {size!r}")
    return w, h


class MockImageProvider:
    """Writes a solid PNG whose colour is derived from the prompt."""

    name = "mock-image"

    def generate(self, prompt: str, out_path: Path, *, size: str = "1536x1024") -> Path:
        width, height = _parse_size(size)
        digest = hashlib.sha256(prompt.encode()).digest()
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(solid_png(width, height, (digest[0], digest[1], digest[2])))
        return out_path
