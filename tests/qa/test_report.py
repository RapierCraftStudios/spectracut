# SPDX-License-Identifier: AGPL-3.0-or-later
"""The QA report (#27): every run has thumbnails at every failure, and failing renders block."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from spectracut.qa.brand_safety import ReferenceMark
from spectracut.qa.common import load_gray, png_size
from spectracut.qa.copy_ocr import OCRError, TextBox
from spectracut.qa.report import (
    ExportBlocked,
    InvalidRenderId,
    QAReport,
    QAReportStore,
    ensure_exportable,
    run_qa,
)
from spectracut.spec.brand import Look, QAThresholds

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 11, 5, tzinfo=UTC)


class SidecarReader:
    """Reads the committed ``.ocr.json`` sidecar of each fixture frame."""

    def __init__(self, names: list[str]) -> None:
        self.by_bytes = {(FIXTURES / f"{n}.png").read_bytes(): n for n in names}

    def read(self, png: bytes) -> list[TextBox]:
        name = self.by_bytes.get(png)
        if name is None:
            raise OCRError("unknown frame")
        data = json.loads((FIXTURES / f"{name}.ocr.json").read_text("utf-8"))
        return [TextBox.model_validate(line) for line in data]


def pngs(names: list[str]) -> list[bytes]:
    return [(FIXTURES / f"{n}.png").read_bytes() for n in names]


def test_regression_fixtures_fail_with_a_thumbnail_at_every_failure(tmp_path: Path) -> None:
    names = ["clean", "dim_screen", "third_party_url", "cookie_banner", "hazy_earth"]
    report = run_qa(
        "film-render_final-abc123",
        pngs(names),
        Look(id="clean"),
        reader=SidecarReader(names),
        copy=["Revenue up 12%"],
        now=NOW,
    )
    assert not report.passed
    by_frame = {(c.name.split(".ocr")[0], c.frame) for c in report.failures()}
    assert ("legibility.contrast", 1) in by_frame
    assert ("brand_safety.third_party_url", 2) in by_frame
    assert ("brand_safety.cookie_banner", 3) in by_frame
    assert ("look.haze", 4) in by_frame
    for check in report.failures():
        assert check.frame is not None and check.thumbnail is not None
        png = report.thumbnails[check.thumbnail]
        size = png_size(png)
        assert size is not None and size[0] <= 160 and load_gray(png) is not None

    store = QAReportStore(tmp_path)
    path = store.save(report)
    assert path == tmp_path / "renders" / "film-render_final-abc123" / "qa.json"
    loaded = store.load(report.render_id)
    assert loaded is not None and loaded.model_dump() == report.model_dump()
    for check in report.failures():
        assert (path.parent / str(check.thumbnail)).is_file()
    with pytest.raises(ExportBlocked, match="look.haze"):
        ensure_exportable(loaded)


def test_logo_strip_render_fails() -> None:
    marks = [
        ReferenceMark(name=m, image=load_gray((FIXTURES / "marks" / f"{m}.png").read_bytes()))  # type: ignore[arg-type]
        for m in ("ring", "bars")
    ]
    report = run_qa(
        "r-logo",
        pngs(["logo_strip"]),
        Look(id="clean"),
        reader=SidecarReader(["logo_strip"]),
        marks=marks,
    )
    logos = [c for c in report.failures() if c.name == "brand_safety.logo"]
    assert len(logos) == 2 and all("logo strip" in c.detail and c.thumbnail for c in logos)


def test_clean_render_passes_and_haze_limit_follows_the_look() -> None:
    names = ["clean", "earth"]
    report = run_qa(
        "r-clean",
        pngs(names),
        Look(id="clean"),
        reader=SidecarReader(names),
        copy=["Revenue up 12%"],
    )
    assert report.passed, [c.name for c in report.failures()]
    ensure_exportable(report)
    hazy = Look(id="atmos", qa_thresholds=QAThresholds(haze_max=0.5))
    report = run_qa("r-atmos", pngs(["hazy_earth"]), hazy, reader=SidecarReader(["hazy_earth"]))
    assert report.passed and (report.look_id, report.look_version) == ("atmos", 1)


def test_no_reader_skips_text_checks_and_exr_frames_are_skipped() -> None:
    report = run_qa("r-1", [b"not a png", *pngs(["clean"])], Look(id="clean"), reader=None)
    statuses = {c.name: c.status for c in report.checks}
    assert statuses["copy"] == "skipped" and statuses["brand_safety.text"] == "skipped"
    assert statuses["frames.decode"] == "skipped"
    assert report.passed


def test_ocr_error_fails_rather_than_passing() -> None:
    report = run_qa("r-2", pngs(["clean"]), Look(id="clean"), reader=SidecarReader([]))
    assert [c.name for c in report.failures()] == ["ocr"]


@pytest.mark.parametrize("bad", ["../etc", "a/b", "..", ".hidden", "", "sha256:xyz"])
def test_render_id_rejects_path_traversal(tmp_path: Path, bad: str) -> None:
    with pytest.raises(InvalidRenderId):
        QAReportStore(tmp_path).load(bad)
    with pytest.raises(ValueError):
        run_qa(bad, [], Look(id="clean"))


def test_passed_must_match_the_checks() -> None:
    with pytest.raises(ValueError, match="passed"):
        QAReport(render_id="r", created_at=NOW, checks=[], passed=False)
    assert QAReportStore(Path("/nonexistent")).load("sha256:" + "0" * 64) is None
