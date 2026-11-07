# SPDX-License-Identifier: AGPL-3.0-or-later
"""Screen states: a live capture composited into notch-aware device chrome.

Not a department. :func:`compose` renders a ``file://`` chrome page (full screen
MacBook, windowed MacBook or iPhone, under ``spectracut/studio/screens``) through
the ``@spectracut/capture`` CLI, with the raw capture's frames as the page's
content, frame-locked on the capture virtual clock. The result is a
:class:`ScreenState`: one beat's real product run as the device screen.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import Field, StringConstraints

from spectracut.spec._base import NonEmptyStr, StrictModel

__all__ = [
    "CHROME_PAGES",
    "CHROME_SIZES",
    "CaptureFrame",
    "Chrome",
    "ScreenState",
    "frame_at",
]

DeviceId = Literal["laptop", "phone"]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Chrome = Literal["fullscreen", "windowed", "iphone"]

SCREENS_ROOT: Final = Path(__file__).resolve().parents[1] / "screens"
CHROME_PAGES: Final[dict[str, Path]] = {
    "fullscreen": SCREENS_ROOT / "fullscreen.html",
    "windowed": SCREENS_ROOT / "windowed.html",
    "iphone": SCREENS_ROOT / "iphone.html",
}
"""Chrome name to its template page."""

NOTCH_BAND_PX: Final = 37
"""Logical height of the black notch safe-area band (full screen MacBook)."""


class CaptureFrame(StrictModel):
    """One PNG and its sha256."""

    path: NonEmptyStr
    sha256: Sha256


class ScreenSize(StrictModel):
    """Composited screen size: logical px and pixel ratio."""

    width: int = Field(gt=0)
    height: int = Field(gt=0)
    scale: float = Field(gt=0)


class Rect(StrictModel):
    """A logical-px rectangle."""

    x: int
    y: int
    width: int = Field(gt=0)
    height: int = Field(gt=0)


CHROME_SIZES: Final[dict[str, ScreenSize]] = {
    "fullscreen": ScreenSize(width=1728, height=1117, scale=2),
    "windowed": ScreenSize(width=1728, height=1117, scale=2),
    "iphone": ScreenSize(width=402, height=874, scale=3),
}


class ScreenState(StrictModel):
    """One beat's live capture composited into device chrome."""

    state: NonEmptyStr
    """The capture key (``beat:<beat>`` or ``proof:<claim>``)."""
    beat: str = ""
    device: DeviceId
    chrome: Chrome
    size: ScreenSize
    content_rect: Rect
    """Where the live page sits inside the chrome (logical px)."""
    clip: list[CaptureFrame] = Field(min_length=1)
    """Composited frames, in order."""
    raw_still_sha256: Sha256
    values: list[str] = Field(default_factory=list)
    """Strings under the anchor and the product's text selectors, from the recorded
    run's final ``text.json`` record (never typed in)."""


def frame_at(state: ScreenState, t: float) -> CaptureFrame:
    """The composited frame at progress ``t`` in [0, 1]."""
    if not (0.0 <= t <= 1.0) or math.isnan(t):
        raise ValueError(f"t must be in [0, 1], got {t}")
    n = len(state.clip)
    return state.clip[min(n - 1, math.floor(t * n))]
