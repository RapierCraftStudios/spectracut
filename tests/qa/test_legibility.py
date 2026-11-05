# SPDX-License-Identifier: AGPL-3.0-or-later
"""QA legibility check (#55): WCAG contrast of the device screen's UI text."""

from __future__ import annotations

import pytest

from spectracut.critique.benchmarks import png_bytes
from spectracut.qa.legibility import (
    AA_RATIO,
    linear_legibility,
    luminance_contrast,
    relative_luminance,
    screen_legibility,
)

W, H = 32, 24


def _ui(text: int, background: int) -> bytes:
    """A 32x24 frame: a dark bezel around an 8..24 x 4..20 screen with 2 px text strokes."""
    pixels = []
    for y in range(H):
        for x in range(W):
            if not (8 <= x < 24 and 4 <= y < 20):
                v = 10
            elif x % 6 < 2 and 6 <= y < 18:
                v = text
            else:
                v = background
            pixels.append((v, v, v))
    return png_bytes(pixels, W, H)


SCREEN = (8, 4, 16, 16)


def test_known_wcag_pairs() -> None:
    black, white = relative_luminance((0, 0, 0)), relative_luminance((255, 255, 255))
    assert black == 0.0 and white == pytest.approx(1.0)
    assert luminance_contrast(black, white) == pytest.approx(21.0)
    assert luminance_contrast(white, black) == pytest.approx(21.0)
    grey = relative_luminance((119, 119, 119))
    assert luminance_contrast(grey, grey) == 1.0
    assert luminance_contrast(grey, white) == pytest.approx(4.48, abs=0.01)
    with pytest.raises(ValueError, match="outside 0..255"):
        relative_luminance((300, 0, 0))


def test_crisp_text_passes_and_a_dim_screen_fails() -> None:
    crisp = screen_legibility(_ui(20, 235), SCREEN)
    assert crisp.passed and crisp.contrast > 10
    assert crisp.text_luminance < crisp.background_luminance
    inverted = screen_legibility(_ui(235, 20), SCREEN)
    assert inverted.passed and inverted.text_luminance > inverted.background_luminance
    dim = screen_legibility(_ui(60, 90), SCREEN)
    assert not dim.passed and dim.contrast < AA_RATIO


@pytest.mark.parametrize("rect", [(30, 0, 4, 4), (0, 0, 0, 4), (-1, 0, 4, 4), (0, 22, 4, 4)])
def test_a_rect_outside_the_image_is_refused(rect: tuple[int, int, int, int]) -> None:
    with pytest.raises(ValueError, match="not inside"):
        screen_legibility(_ui(20, 235), rect)
    with pytest.raises(ValueError, match="8-bit PNG"):
        screen_legibility(b"not a png", SCREEN)


def test_linear_legibility_over_scene_linear_rows() -> None:
    rows = [[(0.0, 0.0, 0.0), (1.0, 1.0, 1.0), (1.0, 1.0, 1.0)]] * 3
    assert linear_legibility(rows).contrast == pytest.approx(21.0)
    with pytest.raises(ValueError, match="at least one pixel"):
        linear_legibility([])
