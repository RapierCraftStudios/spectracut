# SPDX-License-Identifier: AGPL-3.0-or-later
"""Exports respect per-platform safe areas (#26)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from spectracut.formats.cutdowns import CutDownSpec
from spectracut.formats.safe_areas import (
    PLATFORM_SAFE_AREAS,
    RESOLUTIONS,
    Overlay,
    SafeAreaError,
    export_preset,
    inside_safe_area,
    safe_area,
)
from spectracut.qa.common import QACheck
from spectracut.qa.report import ExportBlocked, QAReport

PASSED = QAReport(
    render_id="r1",
    created_at=datetime(2026, 1, 1, tzinfo=UTC),
    checks=[QACheck(name="ok", status="pass")],
    passed=True,
)

CASES = [(p, a) for p, by in PLATFORM_SAFE_AREAS.items() for a in by]


def cutdown(platform: str, aspect: str) -> CutDownSpec:
    return CutDownSpec(
        id="ad",
        length_s=15,
        aspect=aspect,
        platform=platform,
        shots=["s"],
    )


def fill(platform: str, aspect: str, name: str = "cta") -> Overlay:
    a = safe_area(platform, aspect)
    return Overlay(name=name, x=a.left, y=a.top, w=1 - a.left - a.right, h=1 - a.top - a.bottom)


@pytest.mark.parametrize(("platform", "aspect"), CASES)
def test_overlay_filling_the_safe_area_exports(platform: str, aspect: str) -> None:
    preset = export_preset(cutdown(platform, aspect), [fill(platform, aspect)], qa=PASSED)
    assert (preset.width, preset.height) == RESOLUTIONS[aspect]
    assert preset.platform == platform and preset.aspect == aspect


@pytest.mark.parametrize(("platform", "aspect"), CASES)
@pytest.mark.parametrize("side", ["top", "bottom", "left", "right"])
def test_overlay_crossing_any_edge_is_rejected(platform: str, aspect: str, side: str) -> None:
    o = fill(platform, aspect)
    a = safe_area(platform, aspect)
    nudge = 0.01
    moved = {
        "top": {"y": a.top - nudge},
        "bottom": {"y": o.y + nudge},
        "left": {"x": a.left - nudge},
        "right": {"x": o.x + nudge},
    }[side]
    bad = o.model_copy(update=moved)
    assert not inside_safe_area(platform, aspect, bad)
    with pytest.raises(SafeAreaError, match="cta"):
        export_preset(cutdown(platform, aspect), [bad], qa=PASSED)


def test_reels_bottom_ui_band_is_unsafe_for_captions() -> None:
    captions = Overlay(name="captions", x=0.1, y=0.75, w=0.8, h=0.1)
    with pytest.raises(SafeAreaError):
        export_preset(cutdown("instagram_reels", "9:16"), [captions], qa=PASSED)


@pytest.mark.parametrize(
    ("platform", "aspect"), [("tiktok", "16:9"), ("youtube", "9:16"), ("myspace", "1:1")]
)
def test_unsupported_platform_or_aspect_is_rejected(platform: str, aspect: str) -> None:
    with pytest.raises(SafeAreaError):
        export_preset(cutdown(platform, aspect), qa=PASSED)


def test_overlay_must_stay_on_the_frame() -> None:
    with pytest.raises(ValueError):
        Overlay(name="x", x=0.9, y=0.0, w=0.2, h=0.1)


def test_failing_qa_report_blocks_export() -> None:
    failed = QAReport(
        render_id="r2",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        checks=[QACheck(name="ok", status="pass"), QACheck(name="look.haze", status="fail")],
        passed=False,
    )
    with pytest.raises(ExportBlocked, match=r"render r2 failed QA: look\.haze"):
        export_preset(cutdown("tiktok", "9:16"), [fill("tiktok", "9:16")], qa=failed)
