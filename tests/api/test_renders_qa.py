# SPDX-License-Identifier: AGPL-3.0-or-later
"""``GET /v1/renders/{id}/qa`` serves a render's QA report (#27)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from spectracut.api.app import create_app
from spectracut.qa.report import QAReport, QAReportStore, run_qa
from spectracut.spec.brand import Look

FRAME = Path(__file__).resolve().parents[1] / "qa" / "fixtures" / "hazy_earth.png"


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    store = QAReportStore(tmp_path)
    report = run_qa(
        "film-render-1",
        [FRAME.read_bytes()],
        Look(id="clean"),
        now=datetime(2026, 11, 5, tzinfo=UTC),
    )
    store.save(report)
    return TestClient(create_app(store))


def test_get_qa_returns_the_stored_report(client: TestClient) -> None:
    res = client.get("/v1/renders/film-render-1/qa")
    assert res.status_code == 200
    report = QAReport.model_validate(res.json())
    assert report.render_id == "film-render-1" and not report.passed
    haze = next(c for c in report.checks if c.name == "look.haze")
    assert haze.failed and haze.thumbnail is not None
    assert haze.thumbnail.startswith("thumbnails/")


def test_missing_report_is_404(client: TestClient) -> None:
    res = client.get("/v1/renders/nope/qa")
    assert res.status_code == 404
    assert res.json() == {"error": "no QA report for render nope"}


@pytest.mark.parametrize("bad", [".hidden", "a%20b", "-lead", "x%3By"])
def test_invalid_id_is_400(client: TestClient, bad: str) -> None:
    assert client.get(f"/v1/renders/{bad}/qa").status_code == 400


def test_only_get_is_routed(client: TestClient) -> None:
    assert client.post("/v1/renders/film-render-1/qa").status_code == 405
