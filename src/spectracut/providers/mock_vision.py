# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deterministic mock vision judge for dev and CI (no network, no CLI)."""

from __future__ import annotations

import hashlib
from pathlib import Path

from spectracut.providers.base import ProviderError, VisionScore


class MockVisionJudge:
    """Scores a frame from a hash of its bytes and the rubric."""

    name = "mock-vision"

    def score(self, frame_path: Path, rubric: str) -> VisionScore:
        frame_path = Path(frame_path)
        if not frame_path.is_file():
            raise ProviderError("mock-vision", f"frame not found: {frame_path}")
        digest = hashlib.sha256(frame_path.read_bytes() + rubric.encode()).digest()
        value = round(int.from_bytes(digest[:2], "big") / 0xFFFF, 3)
        return VisionScore(score=value, notes="mock vision judge")

    def compare(self, frame_path: Path, reference_path: Path, rubric: str) -> VisionScore:
        frame_path, reference_path = Path(frame_path), Path(reference_path)
        for path in (frame_path, reference_path):
            if not path.is_file():
                raise ProviderError("mock-vision", f"frame not found: {path}")
        digest = hashlib.sha256(
            frame_path.read_bytes() + reference_path.read_bytes() + rubric.encode()
        ).digest()
        value = round(int.from_bytes(digest[:2], "big") / 0xFFFF, 3)
        return VisionScore(score=value, notes="mock reference comparison")
