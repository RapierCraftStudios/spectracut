# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-platform safe areas and export presets.

Each platform overlays its own UI (captions, buttons, progress bars) on the
video. A cut-down's titles, captions, CTA and logo must sit inside the
platform's safe rect for the cut-down's aspect, so :func:`export_preset` refuses
an overlay outside it and a platform that does not take the aspect. It also
refuses a render whose QA report did not pass (#27).

Insets are fractions of the frame height (top, bottom) and width (left, right).
They are deliberately conservative roundings of each platform's published
creative guidance; when a platform changes its UI, change the table here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from pydantic import Field, model_validator

from spectracut.formats.cutdowns import CutDownSpec
from spectracut.spec._base import Slug, StrictModel
from spectracut.spec.spec import Aspect

if TYPE_CHECKING:
    from spectracut.qa.report import QAReport

_EPS = 1e-9

Inset = Annotated[float, Field(ge=0.0, lt=0.5)]


class SafeArea(StrictModel):
    """Margins kept clear of platform UI, as fractions of the frame."""

    top: Inset
    bottom: Inset
    left: Inset
    right: Inset


_TITLE_SAFE = SafeArea(top=0.05, bottom=0.05, left=0.05, right=0.05)
"""Broadcast title-safe (90% of the frame), for platforms without overlaid UI."""

PLATFORM_SAFE_AREAS: dict[str, dict[str, SafeArea]] = {
    # TikTok Ads creative specs (safe zone): profile/caption UI at the bottom,
    # action buttons down the right edge, tabs at the top.
    "tiktok": {"9:16": SafeArea(top=0.08, bottom=0.25, left=0.06, right=0.14)},
    # Meta Reels ads guidance: keep ~14% top, ~35% bottom and 6% sides clear.
    "instagram_reels": {"9:16": SafeArea(top=0.14, bottom=0.35, left=0.06, right=0.06)},
    # YouTube Shorts: title, channel and buttons at the bottom and right edge.
    "youtube_shorts": {"9:16": SafeArea(top=0.10, bottom=0.25, left=0.06, right=0.15)},
    # YouTube in-stream / watch page: player controls and title overlays.
    "youtube": {"16:9": SafeArea(top=0.05, bottom=0.10, left=0.05, right=0.05)},
    # Meta Feed (Facebook / Instagram feed): no overlaid UI, title-safe margins.
    "meta_feed": {"1:1": _TITLE_SAFE, "4:5": _TITLE_SAFE},
    # LinkedIn feed video: no overlaid UI, title-safe margins.
    "linkedin": {"1:1": _TITLE_SAFE, "16:9": _TITLE_SAFE},
}

RESOLUTIONS: dict[str, tuple[int, int]] = {
    "16:9": (1920, 1080),
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
    "4:5": (1080, 1350),
}


class SafeAreaError(ValueError):
    """An export breaks a platform's safe area or aspect support."""


class Overlay(StrictModel):
    """A graphic placed on the frame: box ``x, y, w, h`` as fractions of the frame."""

    name: Slug
    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    w: float = Field(gt=0.0, le=1.0)
    h: float = Field(gt=0.0, le=1.0)

    @model_validator(mode="after")
    def _in_frame(self) -> Overlay:
        if self.x + self.w > 1.0 + _EPS or self.y + self.h > 1.0 + _EPS:
            raise ValueError(f"overlay {self.name} runs off the frame")
        return self


class ExportPreset(StrictModel):
    platform: Slug
    aspect: Aspect
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    overlays: tuple[Overlay, ...] = ()


def safe_area(platform: str, aspect: str) -> SafeArea:
    """The safe area of ``platform`` at ``aspect``; :class:`SafeAreaError` if unsupported."""
    by_aspect = PLATFORM_SAFE_AREAS.get(platform)
    if by_aspect is None:
        raise SafeAreaError(f"unknown platform {platform!r}")
    area = by_aspect.get(aspect)
    if area is None:
        raise SafeAreaError(f"{platform} does not take {aspect} video")
    return area


def inside_safe_area(platform: str, aspect: str, overlay: Overlay) -> bool:
    area = safe_area(platform, aspect)
    return (
        overlay.x >= area.left - _EPS
        and overlay.y >= area.top - _EPS
        and overlay.x + overlay.w <= 1.0 - area.right + _EPS
        and overlay.y + overlay.h <= 1.0 - area.bottom + _EPS
    )


def export_preset(
    cutdown: CutDownSpec,
    overlays: tuple[Overlay, ...] | list[Overlay] = (),
    *,
    qa: QAReport,
) -> ExportPreset:
    """The export of ``cutdown`` for its platform, with every overlay checked.

    ``qa`` is the render's QA report: a report that did not pass raises
    :class:`~spectracut.qa.report.ExportBlocked` before anything else is checked.
    """
    # Imported here: qa.edit_checks imports this module.
    from spectracut.qa.report import ensure_exportable

    ensure_exportable(qa)
    platform, aspect = cutdown.platform, cutdown.aspect
    safe_area(platform, aspect)
    outside = [o.name for o in overlays if not inside_safe_area(platform, aspect, o)]
    if outside:
        raise SafeAreaError(
            f"{cutdown.id}: overlays outside the {platform} {aspect} safe area: "
            + ", ".join(outside)
        )
    width, height = RESOLUTIONS[aspect]
    return ExportPreset(
        platform=platform, aspect=aspect, width=width, height=height, overlays=tuple(overlays)
    )
