# SPDX-License-Identifier: AGPL-3.0-or-later
"""The automated QA checks (#27): regression fixtures fail as expected, clean frames pass."""

from __future__ import annotations

import json
import math
import random
import struct
import wave
from pathlib import Path

import pytest

from spectracut.formats.linter import ShotLook
from spectracut.formats.safe_areas import Overlay
from spectracut.qa import look as look_qa
from spectracut.qa.audio import DELIVERY_SPECS, check_audio, integrated_loudness, true_peak
from spectracut.qa.brand_safety import ReferenceMark, check_logos, check_text
from spectracut.qa.common import BBox, Gray, load_gray
from spectracut.qa.copy_ocr import TextBox, check_copy, parse_tsv
from spectracut.qa.edit_checks import Shot, check_repetition, check_safe_areas
from spectracut.qa.legibility import TextRegion, check_legibility, check_read_time
from spectracut.spec.brand import Look, QAThresholds

FIXTURES = Path(__file__).parent / "fixtures"


def frame(name: str) -> Gray:
    gray = load_gray((FIXTURES / name).read_bytes())
    assert gray is not None
    return gray


def ocr(name: str) -> list[TextBox]:
    """The committed OCR sidecar of a fixture frame (what Tesseract would read)."""
    data = json.loads((FIXTURES / f"{name}.ocr.json").read_text("utf-8"))
    return [TextBox.model_validate(line) for line in data]


def marks() -> list[ReferenceMark]:
    return [ReferenceMark(name=m, image=frame(f"marks/{m}.png")) for m in ("ring", "bars")]


def failed(checks: list) -> list[str]:  # type: ignore[type-arg]
    return [c.name for c in checks if c.failed]


# --- regression fixtures -------------------------------------------------------


def test_dim_screen_fails_contrast_and_clean_screen_passes() -> None:
    regions = [TextRegion(name="screen", box=b.box) for b in ocr("dim_screen")]
    dim = check_legibility(frame("dim_screen.png"), regions, frame_index=3)
    assert failed(dim) == ["legibility.contrast.screen"]
    row = dim[0]
    assert row.value is not None and row.value < 4.5 and row.limit == 4.5
    assert row.frame == 3 and row.bbox == regions[0].box
    assert failed(check_legibility(frame("clean.png"), regions, frame_index=0)) == []


def test_small_text_fails_height() -> None:
    tiny = Gray(100, 100, tuple(1.0 if y == 50 else 0.0 for y in range(100) for x in range(100)))
    box = BBox(x=0.0, y=0.48, w=1.0, h=0.05)
    rows = check_legibility(tiny, [TextRegion(name="t", box=box)], frame_index=0)
    assert failed(rows) == ["legibility.height.t"]


def test_third_party_url_fails_and_allowlisted_domain_passes() -> None:
    rows = check_text([ocr("third_party_url")])
    assert failed(rows) == ["brand_safety.third_party_url"]
    assert "acme-cloud.io" in rows[0].detail and rows[0].bbox is not None
    assert failed(check_text([ocr("third_party_url")], allowed_domains=["acme-cloud.io"])) == []


def test_cookie_banner_fails() -> None:
    rows = check_text([ocr("cookie_banner")])
    assert failed(rows) == ["brand_safety.cookie_banner"]
    assert rows[0].bbox is not None and rows[0].bbox.y > 0.8


def test_logo_strip_fails_and_clean_frame_has_no_logo() -> None:
    rows = check_logos(frame("logo_strip.png"), marks(), frame_index=2)
    assert sorted(r.detail for r in rows) == [
        "third-party logo 'bars' in a logo strip",
        "third-party logo 'ring' in a logo strip",
    ]
    assert all(r.failed and r.frame == 2 and r.bbox is not None for r in rows)
    assert check_logos(frame("clean.png"), marks(), frame_index=0) == []


def test_hazy_earth_fails_the_looks_haze_limit() -> None:
    clean_look = Look(id="clean")
    hazy = look_qa.measure_look_metrics([frame("hazy_earth.png")])
    clear = look_qa.measure_look_metrics([frame("earth.png")])
    assert [c.metric for c in look_qa.look_gate(hazy, clean_look).failures()] == ["haze"]
    assert look_qa.look_gate(clear, clean_look).passed


# --- thresholds come from the Look ------------------------------------------------


def test_haze_and_grain_limits_come_from_the_shots_look() -> None:
    rng = random.Random(7)
    noisy = Gray(64, 64, tuple(0.5 + rng.uniform(-0.15, 0.15) for _ in range(64 * 64)))
    metrics = look_qa.measure_look_metrics([frame("hazy_earth.png"), noisy])
    assert metrics.grain > 0.1
    tight = Look(id="clean")
    loose = Look(
        id="filmic",
        grain=0.2,
        qa_thresholds=QAThresholds(grain_max=0.6, haze_max=0.4, bloom_max=0.1),
    )
    assert {c.metric for c in look_qa.look_gate(metrics, tight).failures()} == {"grain", "haze"}
    assert look_qa.look_gate(metrics, loose).passed


def test_bloom_counts_soft_halos_not_hard_edges() -> None:
    def glow(x: int, y: int) -> float:
        d = math.hypot(x - 32, y - 32)
        return 1.0 if d < 3 else max(0.0, 0.95 - (d - 3) * 0.05)

    soft = Gray(64, 64, tuple(glow(x, y) for y in range(64) for x in range(64)))
    hard = Gray(64, 64, tuple(1.0 if x < 32 else 0.0 for y in range(64) for x in range(64)))
    assert look_qa.bloom(soft) > 0.0
    assert look_qa.bloom(hard) == 0.0


def test_flicker_and_exposure_jumps_at_cuts() -> None:
    def flat(v: float) -> Gray:
        return Gray(8, 8, (v,) * 64)

    fade = [flat(0.1 * i) for i in range(6)]
    assert look_qa.flicker(fade) == pytest.approx(0.0, abs=1e-9)
    assert look_qa.flicker([flat(v) for v in (0.5, 0.6, 0.5, 0.6, 0.5)]) > look_qa.FLICKER_MAX
    shots = [flat(0.3), flat(0.3), flat(0.8), flat(0.8), flat(0.85)]
    assert look_qa.exposure_jumps(shots, [2, 4]) == [(2, 0.5)]


# --- copy ------------------------------------------------------------------------


def test_copy_missing_and_extra_claims_fail() -> None:
    title = BBox(x=0.0, y=0.0, w=1.0, h=0.2)
    boxes = [
        TextBox(text="Ship  faster.", box=BBox(x=0.1, y=0.05, w=0.5, h=0.05)),
        TextBox(text="#1 rated by analysts", box=BBox(x=0.1, y=0.12, w=0.5, h=0.05)),
        TextBox(text="Settings  Logout", box=BBox(x=0.1, y=0.6, w=0.5, h=0.05)),
    ]
    rows = check_copy([boxes], ["ship faster", "Book a demo"], copy_regions=[title])
    assert [(r.name, r.status) for r in rows] == [
        ("copy.present", "pass"),
        ("copy.present", "fail"),
        ("copy.unexpected", "fail"),
    ]
    assert "#1 rated" in rows[2].detail


def test_tesseract_tsv_groups_words_into_lines() -> None:
    header = "\t".join(
        "level page_num block_num par_num line_num word_num left top width height conf text".split()
    )
    rows = [
        "5\t1\t1\t1\t1\t1\t10\t20\t40\t10\t96\tShip",
        "5\t1\t1\t1\t1\t2\t55\t20\t50\t10\t90\tfaster",
        "4\t1\t1\t1\t1\t0\t10\t20\t95\t10\t-1\t",
    ]
    (line,) = parse_tsv("\n".join([header, *rows]), 200, 100)
    assert line.text == "Ship faster" and line.confidence == pytest.approx(0.93)
    assert (line.box.x, line.box.y, line.box.w, line.box.h) == (0.05, 0.2, 0.475, 0.1)


# --- brand safety ----------------------------------------------------------------


def test_oem_marks_forbidden_words_and_secrets() -> None:
    key = "sk-ant-" + "a1B2c3D4" * 4
    lines = [
        TextBox(text="Runs on any MacBook", box=BBox(x=0, y=0, w=0.5, h=0.1)),
        TextBox(text="The cheapest plan ever", box=BBox(x=0, y=0.2, w=0.5, h=0.1)),
        TextBox(text=f"ANTHROPIC_API_KEY={key}", box=BBox(x=0, y=0.4, w=0.5, h=0.1)),
        TextBox(text="token AKIAABCDEFGHIJKLMNOP", box=BBox(x=0, y=0.6, w=0.5, h=0.1)),
    ]
    rows = check_text([lines], forbidden_words=["cheapest"], allowed_marks=())
    assert failed(rows) == [
        "brand_safety.wordmark",
        "brand_safety.forbidden_word",
        "brand_safety.secret",
        "brand_safety.secret",
    ]
    assert all(key not in r.detail and "AKIAABCDEFGHIJKLMNOP" not in r.detail for r in rows)
    assert failed(check_text([lines[:1]], allowed_marks=["macbook"])) == []


# --- safe areas, repetition and read time ------------------------------------------


def test_safe_areas() -> None:
    inside = Overlay(name="cta", x=0.2, y=0.5, w=0.5, h=0.1)
    captions = Overlay(name="captions", x=0.1, y=0.75, w=0.8, h=0.1)
    rows = check_safe_areas("instagram_reels", "9:16", [inside, captions])
    assert [(r.name, r.status) for r in rows] == [
        ("safe_area.cta", "pass"),
        ("safe_area.captions", "fail"),
    ]
    assert failed(check_safe_areas("myspace", "9:16", [])) == ["safe_area"]


def test_repeated_shots_fail_within_the_window() -> None:
    earth, clean = frame("earth.png"), frame("clean.png")
    wide = ShotLook(scale="ws", lens_mm=24, move="static")
    close = ShotLook(scale="cu", lens_mm=85, move="push")
    rows = check_repetition(
        [
            Shot(id="a", frame=earth, look=wide),
            Shot(id="b", frame=clean, look=close),
            Shot(id="c", frame=earth, look=close, frame_index=9),
        ]
    )
    assert [r.detail for r in rows] == [
        "shot c repeats shot a: frames match (mean abs diff 0.000)",
        "shot c repeats shot b: same scale, lens and move",
    ]
    assert rows[0].frame == 9
    assert failed(check_repetition([Shot(id="a", frame=earth), Shot(id="b", frame=clean)])) == []


def test_text_read_time() -> None:
    assert check_read_time("title", 6, 2.5).status == "pass"
    short = check_read_time("title", 6, 2.0)
    assert short.failed and short.limit == 2.5


# --- audio -----------------------------------------------------------------------


def _wav(path: Path, samples: list[float], *, rate: int = 48000, width: int = 2) -> Path:
    full = float((1 << (8 * width - 1)) - 1)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(width)
        out.setframerate(rate)
        data = b"".join(struct.pack("<i", round(v * full))[:width] for v in samples)
        out.writeframes(data)
    return path


def test_loudness_matches_bs1770_reference_tone() -> None:
    rate = 48000
    tone = [math.sin(2 * math.pi * 997 * i / rate) for i in range(rate)]
    assert integrated_loudness(rate, [tone]) == pytest.approx(-3.01, abs=0.05)
    inter = [math.sin(2 * math.pi * (rate / 4) * i / rate + math.pi / 4) for i in range(4800)]
    assert max(abs(v) for v in inter) == pytest.approx(0.7071, abs=1e-3)
    assert true_peak([inter]) > -0.5  # the inter-sample peak, not the -3 dB sample peak


@pytest.mark.parametrize("width", [2, 3])
def test_check_audio_against_delivery_specs(tmp_path: Path, width: int) -> None:
    rate = 48000
    gain = 10 ** ((-14.0 + 3.01) / 20)  # a 997 Hz tone at -14 LUFS
    tone = [gain * math.sin(2 * math.pi * 997 * i / rate) for i in range(rate // 2)]
    path = _wav(tmp_path / "mix.wav", tone, width=width)
    assert failed(check_audio(path, "streaming")) == []
    assert failed(check_audio(path, DELIVERY_SPECS["ebu_r128"])) == ["audio.loudness"]
    hot = _wav(tmp_path / "hot.wav", [v / gain * 0.99 for v in tone], width=width)
    assert failed(check_audio(hot, "streaming")) == ["audio.loudness", "audio.true_peak"]
