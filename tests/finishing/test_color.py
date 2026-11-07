# SPDX-License-Identifier: AGPL-3.0-or-later
"""Scene-referred colour (#54): the OCIO pipeline, CDL/LUT maths and the colour-chart fixture."""

from __future__ import annotations

import math
import struct
import zlib
from collections.abc import Callable

import pytest

from spectracut.finishing.color import (
    COLORCHECKER_24,
    ColorPipeline,
    apply_cdl,
    apply_cube,
    chart_layout,
    delta_e2000,
    linear_to_lab,
    measure_chart,
    parse_cube,
    rec709_decode,
    rec709_encode,
    srgb_decode,
    srgb_encode,
)
from spectracut.spec.brand import CDL

# ---- the pipeline ----------------------------------------------------------------


def test_pipeline_defaults_to_agx_and_the_rec709_sdr_master() -> None:
    p = ColorPipeline()
    assert (p.view_transform, p.display, p.look) == ("AgX", "Rec.1886", None)
    steps = p.render_steps(start=4)
    assert [s.index for s in steps] == [4, 5, 6]
    assert {s.tool for s in steps} == {"render.set_property"}
    assert {s.args["property"]: s.args["value"] for s in steps} == {
        "view_settings.display_device": "Rec.1886",
        "view_settings.view_transform": "AgX",
        "view_settings.look": "None",
    }


def test_review_playback_uses_the_render_triple() -> None:
    p = ColorPipeline(look="AgX - Base Contrast")
    rendered = {s.args["property"]: s.args["value"] for s in p.render_steps()}
    assert p.playback() == {
        "display": rendered["view_settings.display_device"],
        "view": rendered["view_settings.view_transform"],
        "look": rendered["view_settings.look"],
    }


def test_aces2_is_per_project_and_needs_blender_5() -> None:
    p = ColorPipeline(view_transform="ACES 2.0")
    with pytest.raises(ValueError, match=r"Blender 4\.5\.15"):
        p.require_blender("4.5.15")
    p.require_blender("5.0.1")
    ColorPipeline().require_blender("4.5.15")
    with pytest.raises(ValueError, match="MAJOR.MINOR"):
        p.require_blender("five")
    with pytest.raises(ValueError, match="needs the AgX view"):
        ColorPipeline(view_transform="ACES 2.0", look="AgX - Punchy")


@pytest.mark.parametrize(
    ("encode", "decode"), [(rec709_encode, rec709_decode), (srgb_encode, srgb_decode)]
)
def test_transfer_functions_round_trip(
    encode: Callable[[float], float], decode: Callable[[float], float]
) -> None:
    for v in (0.0, 0.001, 0.18, 0.5, 1.0):
        assert decode(encode(v)) == pytest.approx(v, abs=1e-9)
    assert encode(-1.0) == 0.0 and encode(2.0) == pytest.approx(1.0)


def test_rec1886_is_a_pure_2_4_gamma() -> None:
    assert rec709_decode(0.5) == pytest.approx(0.5**2.4)
    assert ColorPipeline().eotf(0.5) == pytest.approx(0.5**2.4)
    assert ColorPipeline(display="sRGB").eotf(0.5) == pytest.approx(srgb_decode(0.5))


# ---- grades ----------------------------------------------------------------------


def test_cdl_slope_offset_clamp_power_then_saturation() -> None:
    assert apply_cdl((0.2, 0.4, 0.6), CDL()) == pytest.approx((0.2, 0.4, 0.6))
    cdl = CDL(slope=(2.0, 1.0, 1.0), offset=(0.1, -0.5, 0.0), power=(2.0, 2.0, 1.0))
    # r: (0.2*2+0.1)^2 = 0.25; g: clamp(0.4-0.5) = 0 -> 0; b: 0.6
    assert apply_cdl((0.2, 0.4, 0.6), cdl) == pytest.approx((0.25, 0.0, 0.6))
    grey = apply_cdl((0.2, 0.4, 0.6), CDL(saturation=0.0))
    luma = 0.2126 * 0.2 + 0.7152 * 0.4 + 0.0722 * 0.6
    assert grey == pytest.approx((luma, luma, luma))


def _identity_cube(n: int) -> str:
    rows = [
        f"{r / (n - 1)} {g / (n - 1)} {b / (n - 1)}"
        for b in range(n)
        for g in range(n)
        for r in range(n)
    ]
    return f'TITLE "id"\n# comment\nLUT_3D_SIZE {n}\n' + "\n".join(rows) + "\n"


def test_identity_3d_cube_is_identity_and_trilinear() -> None:
    lut = parse_cube(_identity_cube(3))
    assert (lut.dimensions, lut.size, lut.title) == (3, 3, "id")
    for rgb in ((0.0, 0.0, 0.0), (0.1, 0.55, 0.9), (1.0, 1.0, 1.0)):
        assert apply_cube(rgb, lut) == pytest.approx(rgb)
    # a red-only swap at the white corner: red-fastest ordering means index n^3 - 1
    text = _identity_cube(2).rstrip("\n").rsplit("\n", 1)[0] + "\n0 0 0\n"
    dark = parse_cube(text)
    assert apply_cube((1.0, 1.0, 1.0), dark) == pytest.approx((0.0, 0.0, 0.0))
    assert apply_cube((0.5, 0.5, 0.5), dark) == pytest.approx((0.375, 0.375, 0.375))


def test_1d_cube_is_per_channel_and_clamps_to_domain() -> None:
    lut = parse_cube("LUT_1D_SIZE 2\nDOMAIN_MIN 0 0 0\nDOMAIN_MAX 2 2 2\n0 0 1\n1 2 0\n")
    assert apply_cube((1.0, 1.0, 1.0), lut) == pytest.approx((0.5, 1.0, 0.5))
    assert apply_cube((5.0, -1.0, 2.0), lut) == pytest.approx((1.0, 0.0, 0.0))


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ("0 0 0\n", "no LUT_1D_SIZE"),
        ("LUT_3D_SIZE 2\n0 0 0\n", "declares 8 entries but has 1"),
        ("LUT_3D_SIZE 1\n0 0 0\n", "bad LUT_3D_SIZE"),
        ("LUT_1D_SIZE 2\nLUT_3D_SIZE 2\n", "one size"),
        ("LUT_1D_SIZE 2\n0 0\n1 1 1\n", "expected 3 numbers"),
        ("LUT_1D_SIZE 2\n0 0 x\n1 1 1\n", "not three numbers"),
        ("LUT_1D_SIZE 2\n0 0 nan\n1 1 1\n", "finite"),
        ("LUT_1D_SIZE 2\nFROB 1\n", "unknown keyword"),
        ("LUT_1D_SIZE 2\nDOMAIN_MAX 0 0 0\n0 0 0\n1 1 1\n", "DOMAIN_MAX must exceed"),
    ],
)
def test_malformed_cube_is_refused(text: str, needle: str) -> None:
    with pytest.raises(ValueError, match=needle):
        parse_cube(text)


# ---- CIELAB and ΔE2000 -----------------------------------------------------------


@pytest.mark.parametrize(
    ("lab1", "lab2", "want"),
    [  # Sharma, Wu and Dalal (2005), Table 1
        ((50.0, 2.6772, -79.7751), (50.0, 0.0, -82.7485), 2.0425),
        ((50.0, 3.1571, -77.2803), (50.0, 0.0, -82.7485), 2.8615),
        ((50.0, 2.5, 0.0), (50.0, 0.0, -2.5), 4.3065),
        ((50.0, 2.5, 0.0), (73.0, 25.0, -18.0), 27.1492),
        ((60.2574, -34.0099, 36.2677), (60.4626, -34.1751, 39.4387), 1.2644),
        ((2.0776, 0.0795, -1.1350), (0.9033, -0.0636, -0.5514), 0.9082),
    ],
)
def test_delta_e2000_matches_sharma_reference_pairs(
    lab1: tuple[float, float, float], lab2: tuple[float, float, float], want: float
) -> None:
    assert delta_e2000(lab1, lab2) == pytest.approx(want, abs=1e-4)
    assert delta_e2000(lab2, lab1) == pytest.approx(want, abs=1e-4)


def test_lab_of_d65_white_and_black() -> None:
    assert linear_to_lab((1.0, 1.0, 1.0)) == pytest.approx((100.0, 0.0, 0.0), abs=1e-3)
    assert linear_to_lab((0.0, 0.0, 0.0)) == pytest.approx((0.0, 0.0, 0.0), abs=1e-9)


# ---- the colour chart fixture ------------------------------------------------------


def _png(width: int, height: int, rgb: Callable[[int, int], tuple[int, int, int]]) -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        crc = zlib.crc32(kind + body) & 0xFFFFFFFF
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", crc)

    raw = b"".join(b"\0" + b"".join(bytes(rgb(x, y)) for x in range(width)) for y in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _chart_master(width: int, height: int, encode: Callable[[float], float]) -> bytes:
    """An 8-bit master of the chart: each patch's linear reference encoded by ``encode``."""
    layout = chart_layout(width, height)
    colours = [tuple(round(encode(c) * 255) for c in ref) for _, ref in COLORCHECKER_24]

    def pixel(x: int, y: int) -> tuple[int, int, int]:
        for rect, colour in zip(layout, colours, strict=True):
            if rect.x0 <= x < rect.x1 and rect.y0 <= y < rect.y1:
                return (colour[0], colour[1], colour[2])
        raise AssertionError((x, y))

    return _png(width, height, pixel)


def test_chart_has_24_named_patches_with_linear_references() -> None:
    names = [n for n, _ in COLORCHECKER_24]
    assert len(names) == len(set(names)) == 24
    assert names[0] == "dark_skin" and names[-1] == "black_2"
    for _, ref in COLORCHECKER_24:
        assert all(0.0 <= c <= 1.0 for c in ref)
    white = dict(COLORCHECKER_24)["white_9_5"]
    assert white[0] == pytest.approx(srgb_decode(243 / 255))
    layout = chart_layout(120, 80)
    assert len(layout) == 24 and (layout[0].x0, layout[-1].x1, layout[-1].y1) == (0, 120, 80)
    assert layout[0].inner() == type(layout[0])("dark_skin", 5, 5, 15, 15)


def test_rec709_sdr_master_measures_correctly_on_the_chart() -> None:
    """An 8-bit Rec.1886 master of the chart measures ΔE2000 < 2 on every patch."""
    pipeline = ColorPipeline()
    master = _chart_master(96, 64, rec709_encode)
    report = measure_chart(master, chart_layout(96, 64), pipeline)
    assert [p.name for p in report.patches] == [n for n, _ in COLORCHECKER_24]
    assert report.max_delta_e < 2.0 and report.passed()
    assert report.mean_delta_e <= report.max_delta_e
    assert math.isfinite(report.mean_delta_e)


def test_chart_catches_a_master_encoded_for_the_wrong_display() -> None:
    """The same chart encoded sRGB but read as Rec.1886 is off on the dark patches."""
    master = _chart_master(96, 64, srgb_encode)
    report = measure_chart(master, chart_layout(96, 64), ColorPipeline())
    assert not report.passed()
    worst = max(report.patches, key=lambda p: p.delta_e)
    assert worst.delta_e > 2.0
    ok = measure_chart(master, chart_layout(96, 64), ColorPipeline(display="sRGB"))
    assert ok.passed()


def test_measure_chart_refuses_a_layout_outside_the_master() -> None:
    master = _chart_master(48, 32, rec709_encode)
    with pytest.raises(ValueError, match="outside the 48x32 master"):
        measure_chart(master, chart_layout(96, 64), ColorPipeline())
    with pytest.raises(ValueError, match="too small"):
        chart_layout(5, 3)


ALTERLAB_SWATCHES = ["#0b1220", "#f5f7fa", "#2f6bff", "#18b47b", "#f2a33a", "#3a475c"]


@pytest.mark.parametrize("swatch", ALTERLAB_SWATCHES)
def test_hex_to_linear_round_trips_through_srgb_encode(swatch: str) -> None:
    from spectracut.finishing.color import hex_to_linear, srgb_encode

    encoded = "#" + "".join(f"{round(srgb_encode(c) * 255):02x}" for c in hex_to_linear(swatch))
    assert encoded == swatch


def test_hex_to_lab_matches_the_linearised_swatch() -> None:
    from spectracut.finishing.color import hex_to_lab, hex_to_linear, linear_to_lab

    assert hex_to_lab("#2f6bff") == pytest.approx(linear_to_lab(hex_to_linear("#2f6bff")), abs=1e-3)
    assert hex_to_lab("#000000") == pytest.approx((0.0, 0.0, 0.0), abs=1e-6)


def test_brand_pipeline_steps_pin_standard_srgb_and_exposure_zero() -> None:
    from spectracut.finishing.color import BRAND_PIPELINE

    steps = BRAND_PIPELINE.render_steps(start=2)
    assert [s.index for s in steps] == [2, 3, 4, 5]
    assert [(s.args["property"], s.args["value"]) for s in steps] == [
        ("view_settings.display_device", "sRGB"),
        ("view_settings.view_transform", "Standard"),
        ("view_settings.look", "None"),
        ("view_settings.exposure", 0.0),
    ]


def test_default_pipeline_steps_are_unchanged() -> None:
    steps = ColorPipeline().render_steps()
    assert len(steps) == 3
    assert all(s.args["property"] != "view_settings.exposure" for s in steps)


def test_pipeline_exposure_is_bounded() -> None:
    with pytest.raises(ValueError):
        ColorPipeline(exposure=11)
