# SPDX-License-Identifier: AGPL-3.0-or-later
"""Palette QA on synthetic frames: background, lifted blacks, blown highlights, swatches."""

from __future__ import annotations

import math

from spectracut.qa.common import encode_png
from spectracut.qa.palette import (
    BLOWN_AREA_MAX,
    MAX_WIDTH,
    PaletteCheck,
    RGBFrame,
    load_rgb,
    palette_qa,
)

INK = "#0b1220"
ACCENT = "#2f6bff"
SURFACE = "#f5f7fa"
SWATCHES = {"ink": INK, "accent": ACCENT}
W, H = 160, 90


def rgb(hex_colour: str) -> tuple[int, int, int]:
    h = hex_colour.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def disc_frame(
    background: str,
    disc: str | None = ACCENT,
    radius: float = 20,
    core: str | None = None,
    core_radius: float = 0,
    width: int = W,
    height: int = H,
) -> RGBFrame:
    pixels = []
    for y in range(height):
        for x in range(width):
            d = math.hypot(x - width / 2, y - height / 2)
            if core is not None and d < core_radius:
                pixels.append(rgb(core))
            elif disc is not None and d < radius:
                pixels.append(rgb(disc))
            else:
                pixels.append(rgb(background))
    return RGBFrame(width, height, tuple(pixels))


def checks(frame: RGBFrame, swatches: dict[str, str] = SWATCHES) -> dict[str, PaletteCheck]:
    return {c.name: c for c in palette_qa(frame, swatches).checks}


def test_accent_disc_on_the_ink_void_passes_every_check() -> None:
    report = palette_qa(disc_frame(INK), SWATCHES)
    assert [c.name for c in report.checks] == [
        "background",
        "lifted_blacks",
        "blown_highlights",
        "swatch:accent",
    ]
    assert report.passed and report.failures() == [] and report.lines() == []
    accent = next(c for c in report.checks if c.name == "swatch:accent")
    assert accent.delta_e is not None and accent.delta_e < 1


def test_hex_over_255_void_fails_background_and_lifted_blacks() -> None:
    # #0b1220 fed to the renderer as hex/255 (not linearised) displays as #3a4b63.
    report = palette_qa(disc_frame("#3a4b63"), SWATCHES)
    assert not report.passed
    assert {c.name for c in report.failures()} == {"background", "lifted_blacks"}
    lines = report.lines()
    assert lines[0].startswith("palette background: measured #3a4b63 vs ink #0b1220, dE ")
    assert "> 10;" in lines[0]
    lifted = lines[1]
    assert lifted.startswith("palette lifted_blacks: measured #3a4b63 vs ink #0b1220, L* +")
    for fix in ("world strength", "world.set_haze", "view_settings.exposure", "brand_bottom"):
        assert fix in lifted


def test_white_cored_ice_blue_beam_fails_accent_and_blown_highlights() -> None:
    frame = disc_frame(INK, "#9ec5ff", radius=25, core="#ffffff", core_radius=15)
    found = checks(frame)
    blown = found["blown_highlights"]
    accent = found["swatch:accent"]
    assert not blown.passed and blown.value is not None and blown.value > BLOWN_AREA_MAX
    assert not accent.passed
    assert "washed out" in accent.advice
    assert "emission_strength at most 1" in accent.advice
    assert found["background"].passed
    line = next(x for x in palette_qa(frame, SWATCHES).lines() if "blown_highlights" in x)
    assert "emission_strength" in line


def test_small_white_core_stays_under_the_blown_area_limit() -> None:
    frame = disc_frame(INK, ACCENT, radius=20, core="#ffffff", core_radius=3)
    assert checks(frame)["blown_highlights"].passed


def test_light_background_does_not_run_lifted_blacks() -> None:
    found = checks(disc_frame(SURFACE), {**SWATCHES, "surface": SURFACE})
    assert "lifted_blacks" not in found
    assert found["background"].target == "surface"
    assert all(c.passed for c in found.values())


def test_frame_without_the_accent_skips_its_swatch_check() -> None:
    accent = checks(disc_frame(INK, disc=None))["swatch:accent"]
    assert accent.passed and accent.value is None
    assert accent.advice == "not in frame"


def test_wide_frames_are_downscaled_before_measuring() -> None:
    frame = disc_frame(INK, width=640, height=360, radius=80)
    assert frame.downscaled().width == MAX_WIDTH
    assert palette_qa(frame, SWATCHES).passed


def test_load_rgb_reads_a_png_and_refuses_other_bytes() -> None:
    frame = disc_frame(INK)
    png = encode_png(W, H, 3, bytes(v for p in frame.pixels for v in p))
    loaded = load_rgb(png)
    assert loaded == frame
    rgba = encode_png(1, 1, 4, bytes([1, 2, 3, 255]))
    assert load_rgb(rgba) == RGBFrame(1, 1, ((1, 2, 3),))
    assert load_rgb(b"not a png") is None


def test_from_linear_encodes_for_an_srgb_display() -> None:
    from spectracut.finishing.color import hex_to_linear

    frame = RGBFrame.from_linear(1, 1, [hex_to_linear(INK)])
    assert frame.pixels == (rgb(INK),)
