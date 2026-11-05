# SPDX-License-Identifier: AGPL-3.0-or-later
"""On-screen text: the OCR reader protocol, a local Tesseract adapter and the copy check.

OCR runs locally (Tesseract) or through an injected :class:`TextReader`; no
cloud OCR is ever called. When no reader is available the text checks report
``skipped``, never a silent pass.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Sequence
from typing import Protocol

from pydantic import Field

from spectracut.qa.common import BBox, QACheck, png_size
from spectracut.spec._base import StrictModel

MIN_CONFIDENCE = 0.6
"""OCR lines below this confidence are ignored by the copy check."""


class TextBox(StrictModel):
    """One line of recognised text and its box as fractions of the frame."""

    text: str
    box: BBox
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class TextReader(Protocol):
    """Reads the text lines in a PNG frame."""

    def read(self, png: bytes) -> list[TextBox]: ...


class OCRError(RuntimeError):
    """The OCR engine failed on a frame."""


class TesseractReader:
    """Runs the local ``tesseract`` CLI with TSV output, one line per text line."""

    def __init__(self, binary: str = "tesseract", timeout_s: float = 60.0) -> None:
        self.binary = binary
        self.timeout_s = timeout_s

    def read(self, png: bytes) -> list[TextBox]:
        size = png_size(png)
        if size is None:
            raise OCRError("frame is not a PNG")
        try:
            proc = subprocess.run(
                [self.binary, "stdin", "stdout", "tsv"],
                input=png,
                capture_output=True,
                timeout=self.timeout_s,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise OCRError(f"tesseract failed: {exc}") from None
        if proc.returncode != 0:
            raise OCRError(f"tesseract exited {proc.returncode}")
        return parse_tsv(proc.stdout.decode("utf-8", "replace"), *size)


def parse_tsv(tsv: str, width: int, height: int) -> list[TextBox]:
    """Group Tesseract TSV words into lines with their bounding boxes."""
    lines: dict[tuple[str, str, str], list[tuple[str, int, int, int, int, float]]] = {}
    for row in tsv.splitlines()[1:]:
        cols = row.split("\t")
        if len(cols) < 12 or cols[0] != "5" or not cols[11].strip():
            continue
        try:
            left, top, w, h = (int(c) for c in cols[6:10])
            conf = float(cols[10])
        except ValueError:
            continue
        lines.setdefault((cols[2], cols[3], cols[4]), []).append(
            (cols[11].strip(), left, top, w, h, conf)
        )
    out: list[TextBox] = []
    for words in lines.values():
        x0 = min(w[1] for w in words)
        y0 = min(w[2] for w in words)
        x1 = max(w[1] + w[3] for w in words)
        y1 = max(w[2] + w[4] for w in words)
        conf = sum(w[5] for w in words) / len(words) / 100.0
        out.append(
            TextBox(
                text=" ".join(w[0] for w in words),
                box=BBox(
                    x=min(1.0, x0 / width),
                    y=min(1.0, y0 / height),
                    w=min(1.0, (x1 - x0) / width),
                    h=min(1.0, (y1 - y0) / height),
                ),
                confidence=max(0.0, min(1.0, conf)),
            )
        )
    return out


def available_reader() -> TextReader | None:
    """A :class:`TesseractReader` when ``tesseract`` is on PATH, else ``None``."""
    binary = shutil.which("tesseract")
    return TesseractReader(binary) if binary else None


def normalise(text: str) -> str:
    """Lower-case, punctuation-insensitive, single-spaced text for matching."""
    return " ".join(re.sub(r"[^\w\s%$€£.-]", " ", text.lower()).split())


def _inside(box: BBox, regions: Sequence[BBox]) -> bool:
    cx, cy = box.x + box.w / 2, box.y + box.h / 2
    return any(r.x <= cx <= r.x + r.w and r.y <= cy <= r.y + r.h for r in regions)


def check_copy(
    frames: Sequence[Sequence[TextBox]],
    expected: Sequence[str],
    *,
    copy_regions: Sequence[BBox] = (),
) -> list[QACheck]:
    """Every expected line of spec copy appears on screen; no extra claims in copy regions.

    ``frames`` holds the OCR lines of each frame. Extra text is only judged inside
    ``copy_regions`` (title, caption and CTA boxes), because a captured product
    screen legitimately carries text the spec does not list.
    """
    lines = [(i, b) for i, boxes in enumerate(frames) for b in boxes]
    seen = " \n ".join(normalise(b.text) for _, b in lines if b.confidence >= MIN_CONFIDENCE)
    pairs = [(e, normalise(e)) for e in expected if normalise(e)]
    wanted = [norm for _, norm in pairs]
    out: list[QACheck] = []
    for raw, norm in pairs:
        found = norm in seen
        out.append(
            QACheck(
                name="copy.present",
                status="pass" if found else "fail",
                detail=f"{raw!r} " + ("on screen" if found else "is missing from the frames"),
            )
        )
    for i, box in lines:
        if box.confidence < MIN_CONFIDENCE or not _inside(box.box, copy_regions):
            continue
        text = normalise(box.text)
        if text and not any(text in w or w in text for w in wanted):
            out.append(
                QACheck(
                    name="copy.unexpected",
                    status="fail",
                    detail=f"on-screen text {box.text!r} is not in the spec's copy",
                    frame=i,
                    bbox=box.box,
                )
            )
    if not out:
        out.append(QACheck(name="copy.present", status="pass", detail="no copy expected"))
    return out
