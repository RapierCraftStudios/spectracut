# SPDX-License-Identifier: AGPL-3.0-or-later
"""The QA report of a render: every check, a thumbnail at every failure, and the export gate.

:func:`run_qa` runs the legibility, brand-safety, copy, Look, motion, safe-area,
repetition, read-time and audio checks on a render's frames and returns a
:class:`QAReport`. :class:`QAReportStore` keeps it at
``<home>/renders/<render_id>/qa.json`` with its ``thumbnails/``, and
:func:`ensure_exportable` refuses a report that did not pass.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import PrivateAttr, model_validator

from spectracut.formats.safe_areas import Overlay
from spectracut.qa import look as look_qa
from spectracut.qa.audio import DeliverySpec, check_audio
from spectracut.qa.brand_safety import ReferenceMark, check_logos, check_text
from spectracut.qa.common import BBox, Gray, QACheck, downscale, gray_png, load_gray
from spectracut.qa.copy_ocr import OCRError, TextBox, TextReader, check_copy
from spectracut.qa.edit_checks import Shot, check_repetition, check_safe_areas
from spectracut.qa.legibility import TextRegion, check_legibility, check_read_time
from spectracut.spec._base import StrictModel
from spectracut.spec.brand import BrandKit, Look
from spectracut.studio.store import spectracut_home, write_atomic

THUMB_WIDTH = 160
THUMB_PAD = 0.05
"""Margin (fraction of the frame) kept around a failure's box in its thumbnail."""

_RENDER_ID = re.compile(r"^(?:[A-Za-z0-9][A-Za-z0-9_.-]{0,199}|sha256:[0-9a-f]{64})$")


class InvalidRenderId(ValueError):
    """A render id that is neither a slug nor a ``sha256:`` hash."""


def validate_render_id(render_id: str) -> str:
    if not _RENDER_ID.fullmatch(render_id):
        raise InvalidRenderId(f"invalid render id {render_id!r}")
    return render_id


class QAReport(StrictModel):
    """Every QA check of one render; ``passed`` is false when any check failed."""

    render_id: str
    created_at: datetime
    look_id: str | None = None
    look_version: int | None = None
    checks: list[QACheck]
    passed: bool

    _thumbnails: dict[str, bytes] = PrivateAttr(default_factory=dict)

    @model_validator(mode="after")
    def _consistent(self) -> QAReport:
        validate_render_id(self.render_id)
        if self.passed == any(c.failed for c in self.checks):
            raise ValueError("passed must be false exactly when a check failed")
        return self

    def failures(self) -> list[QACheck]:
        return [c for c in self.checks if c.failed]

    @property
    def thumbnails(self) -> dict[str, bytes]:
        """PNG bytes by relative path, for checks run in this process (not persisted state)."""
        return self._thumbnails


class ExportBlocked(Exception):
    """A render whose QA report did not pass may not be exported."""

    def __init__(self, report: QAReport) -> None:
        self.report = report
        names = ", ".join(c.name for c in report.failures())
        super().__init__(f"render {report.render_id} failed QA: {names}")


def ensure_exportable(report: QAReport) -> None:
    """Raise :class:`ExportBlocked` unless ``report`` passed."""
    if not report.passed:
        raise ExportBlocked(report)


def thumbnail(frame: Gray, bbox: BBox | None) -> bytes:
    """A downscaled greyscale PNG of ``bbox`` (padded) in ``frame``, or the whole frame."""
    region = frame
    if bbox is not None:
        x0, y0 = max(0.0, bbox.x - THUMB_PAD), max(0.0, bbox.y - THUMB_PAD)
        x1 = min(1.0, bbox.x + bbox.w + THUMB_PAD)
        y1 = min(1.0, bbox.y + bbox.h + THUMB_PAD)
        region = frame.crop(BBox(x=x0, y=y0, w=x1 - x0, h=y1 - y0))
    return gray_png(downscale(region, THUMB_WIDTH))


def _frame_metric_rows(frames: dict[int, Gray], look: Look) -> list[QACheck]:
    per: dict[str, list[tuple[int, float]]] = {"grain": [], "haze": [], "bloom": []}
    fns: dict[str, Callable[[Gray], float]] = {
        "grain": look_qa.grain,
        "haze": look_qa.haze,
        "bloom": look_qa.bloom,
    }
    for i, frame in frames.items():
        for metric, fn in fns.items():
            per[metric].append((i, fn(frame)))

    def avg(metric: str) -> float:
        return round(sum(v for _, v in per[metric]) / len(per[metric]), 4)

    metrics = look_qa.LookMetrics(grain=avg("grain"), haze=avg("haze"), bloom=avg("bloom"))
    gate = look_qa.look_gate(metrics, look)
    return [
        QACheck(
            name=f"look.{c.metric}",
            status="pass" if c.passed else "fail",
            value=c.value,
            limit=c.limit,
            detail="" if c.passed else f"{c.metric} exceeds the {look.id} Look's limit",
            frame=None if c.passed else max(per[c.metric], key=lambda p: p[1])[0],
        )
        for c in gate.checks
    ]


def _read_text(
    reader: TextReader | None, pngs: Sequence[bytes]
) -> tuple[list[list[TextBox]] | None, list[QACheck]]:
    if reader is None:
        return None, [
            QACheck(
                name=name,
                status="skipped",
                detail="no OCR reader available (install tesseract)",
            )
            for name in ("copy", "brand_safety.text", "legibility")
        ]
    try:
        return [reader.read(png) for png in pngs], []
    except OCRError as exc:
        return None, [QACheck(name="ocr", status="fail", detail=str(exc))]


def run_qa(
    render_id: str,
    frames: Sequence[bytes],
    look: Look,
    *,
    reader: TextReader | None = None,
    brand_kit: BrandKit | None = None,
    copy: Sequence[str] = (),
    copy_regions: Sequence[BBox] = (),
    text_regions: Sequence[TextRegion] = (),
    text_holds: Sequence[tuple[str, int, float]] = (),
    marks: Sequence[ReferenceMark] = (),
    allowed_domains: Sequence[str] = (),
    allowed_marks: Sequence[str] = (),
    cuts: Sequence[int] = (),
    shots: Sequence[Shot] = (),
    overlays: Sequence[Overlay] = (),
    platform: str | None = None,
    aspect: str | None = None,
    audio: Path | None = None,
    delivery: DeliverySpec | str = "streaming",
    now: datetime | None = None,
) -> QAReport:
    """Run every QA check on a render's PNG ``frames`` and attach failure thumbnails.

    Frames that are not 8-bit PNGs (scene-referred EXR) are skipped by the pixel
    checks and reported as ``skipped``. Text regions default to the OCR lines.
    """
    validate_render_id(render_id)
    decoded = {i: g for i, png in enumerate(frames) if (g := load_gray(png)) is not None}
    checks: list[QACheck] = []
    if len(decoded) < len(frames):
        checks.append(
            QACheck(
                name="frames.decode",
                status="skipped",
                detail=f"{len(frames) - len(decoded)} of {len(frames)} frames are not 8-bit PNG",
            )
        )

    ocr, ocr_rows = _read_text(reader, frames)
    checks += ocr_rows
    if ocr is not None:
        checks += check_copy(ocr, copy, copy_regions=copy_regions)
        checks += check_text(
            ocr,
            allowed_domains=allowed_domains,
            allowed_marks=allowed_marks,
            forbidden_words=brand_kit.forbidden_words if brand_kit else (),
        )

    for i, frame in decoded.items():
        regions = list(text_regions)
        if not regions and ocr is not None:
            regions = [
                TextRegion(name=f"ocr{n}", box=b.box) for n, b in enumerate(ocr[i]) if b.text
            ]
        checks += check_legibility(frame, regions, frame_index=i)
        if marks:
            checks += check_logos(frame, marks, frame_index=i)

    if decoded:
        checks += _frame_metric_rows(decoded, look)
        ordered = [decoded[i] for i in sorted(decoded)]
        bounds = [0, *sorted(c for c in cuts if 0 < c < len(frames)), len(frames)]
        for start, end in zip(bounds, bounds[1:], strict=False):
            shot = [decoded[i] for i in range(start, end) if i in decoded]
            value = look_qa.flicker(shot)
            ok = value <= look_qa.FLICKER_MAX
            checks.append(
                QACheck(
                    name="flicker",
                    status="pass" if ok else "fail",
                    value=round(value, 4),
                    limit=look_qa.FLICKER_MAX,
                    detail="" if ok else f"luma flickers in frames {start}..{end - 1}",
                    frame=None if ok else start,
                )
            )
        if len(ordered) == len(frames):
            for cut, delta in look_qa.exposure_jumps(ordered, cuts):
                checks.append(
                    QACheck(
                        name="exposure_jump",
                        status="fail",
                        value=delta,
                        limit=look_qa.EXPOSURE_JUMP,
                        detail=f"exposure jumps at the cut into frame {cut}",
                        frame=cut,
                    )
                )
    else:
        checks.append(QACheck(name="look", status="skipped", detail="no decodable frames"))

    if platform is not None and aspect is not None:
        checks += check_safe_areas(platform, aspect, overlays)
    if shots:
        checks += check_repetition(shots)
    checks += [check_read_time(name, words, held) for name, words, held in text_holds]
    if audio is not None:
        checks += check_audio(audio, delivery)

    thumbs: dict[str, bytes] = {}
    final: list[QACheck] = []
    for n, check in enumerate(checks):
        source = decoded.get(check.frame) if check.failed and check.frame is not None else None
        if source is not None:
            rel = f"thumbnails/{n:03d}-{re.sub(r'[^a-z0-9_.-]', '_', check.name.lower())}.png"
            thumbs[rel] = thumbnail(source, check.bbox)
            check = check.model_copy(update={"thumbnail": rel})
        final.append(check)
    report = QAReport(
        render_id=render_id,
        created_at=now or datetime.now(UTC),
        look_id=look.id,
        look_version=look.version,
        checks=final,
        passed=not any(c.failed for c in final),
    )
    report._thumbnails.update(thumbs)
    return report


class QAReportStore:
    """QA reports at ``<home>/renders/<render_id>/qa.json`` with their ``thumbnails/``."""

    def __init__(self, home: Path | None = None) -> None:
        self.root = (home if home is not None else spectracut_home()) / "renders"

    def dir(self, render_id: str) -> Path:
        return self.root / validate_render_id(render_id).replace(":", "_")

    def save(self, report: QAReport) -> Path:
        base = self.dir(report.render_id)
        for rel, data in report.thumbnails.items():
            write_atomic(base / rel, data)
        path = base / "qa.json"
        write_atomic(path, report.model_dump_json(indent=2).encode("utf-8"))
        return path

    def load(self, render_id: str) -> QAReport | None:
        """The stored report, or ``None`` when there is none."""
        path = self.dir(render_id) / "qa.json"
        if not path.is_file():
            return None
        return QAReport.model_validate_json(path.read_bytes())
