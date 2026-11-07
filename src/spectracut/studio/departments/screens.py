# SPDX-License-Identifier: AGPL-3.0-or-later
"""Screen states: a live capture composited into notch-aware device chrome.

Not a department. :func:`compose` renders a ``file://`` chrome page (full screen
MacBook, windowed MacBook or iPhone, under ``spectracut/studio/screens``) through
the ``@spectracut/capture`` CLI, with the raw capture's frames as the page's
content, frame-locked on the capture virtual clock. The result is a
:class:`ScreenState`: one beat's real product run as the device screen.
"""

from __future__ import annotations

import hashlib
import json
import math
import struct
import zlib
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Final, Literal
from urllib.parse import quote, urlsplit

from pydantic import Field, JsonValue, StringConstraints

from spectracut.qa.palette import load_rgb
from spectracut.queue.queue import Job
from spectracut.spec._base import NonEmptyStr, StrictModel

if TYPE_CHECKING:
    from spectracut.queue.executors.capture import CaptureExecutor
    from spectracut.studio.departments.capture import ScreenCapture

__all__ = [
    "CHROME_PAGES",
    "CHROME_SIZES",
    "CaptureFrame",
    "Chrome",
    "ScreenState",
    "check_state",
    "compose",
    "compose_spec",
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

_PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"
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


CONTENT_RECTS: Final[dict[str, Rect]] = {
    "fullscreen": Rect(x=0, y=NOTCH_BAND_PX, width=1728, height=1080),
    "windowed": Rect(x=164, y=122, width=1400, height=875),
    "iphone": Rect(x=0, y=54, width=402, height=820),
}
"""Where the live page sits in each chrome page (logical px); matches the HTML."""


def compose_spec(raw: ScreenCapture, chrome: Chrome) -> dict[str, JsonValue]:
    """The capture-CLI spec that composites ``raw``'s clip into ``chrome``."""
    size = CHROME_SIZES[chrome]
    frames = [Path(f.path).resolve().as_uri() for f in raw.clip]
    frames_json = quote(json.dumps(frames, separators=(",", ":")), safe="")
    url = (
        f"{CHROME_PAGES[chrome].resolve().as_uri()}?s={quote(raw.key, safe='')}"
        f"&frames={frames_json}&fps={raw.fps}&host={quote(urlsplit(raw.url).netloc, safe='')}"
    )
    return {
        "url": url,
        "viewport": {"width": size.width, "height": size.height},
        "deviceScaleFactor": size.scale,
        "fps": raw.fps,
        "script": [{"do": "wait", "frames": len(raw.clip)}],
        "clean": False,
        "legibility": False,
    }


def _values(text_path: Path) -> list[str]:
    """Strings under the anchor and text selectors, from the final text.json record."""
    try:
        records = json.loads(text_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read recorded text {text_path}: {exc}") from None
    if not isinstance(records, list) or not records or not isinstance(records[-1], dict):
        return []
    by_selector: Any = records[-1].get("selectors")
    found: list[str] = []
    if isinstance(by_selector, Mapping):
        for texts in by_selector.values():
            for text in texts if isinstance(texts, list) else []:
                if isinstance(text, str) and text.strip() and text not in found:
                    found.append(text)
    return found


def compose(
    executor: CaptureExecutor, raw: ScreenCapture, chrome: Chrome, out: Path
) -> ScreenState:
    """Render ``raw`` into ``chrome`` through the capture CLI and check the result."""
    job = Job(
        id=f"screen-{out.parent.name}-{out.name}",
        kind="capture",
        payload={"spec": compose_spec(raw, chrome), "out_dir": str(out)},
    )
    result = executor.run(job)
    if not result.frames:
        raise ValueError(f"screen {raw.key!r} in {chrome} chrome wrote no frames")
    clip = [
        CaptureFrame(path=str(f), sha256=hashlib.sha256(Path(f).read_bytes()).hexdigest())
        for f in result.frames
    ]
    state = ScreenState(
        state=raw.key,
        beat=raw.beat,
        device=raw.device,
        chrome=chrome,
        size=CHROME_SIZES[chrome],
        content_rect=CONTENT_RECTS[chrome],
        clip=clip,
        raw_still_sha256=raw.still.sha256,
        values=_values(Path(raw.text)),
    )
    check_state(state)
    return state


def _header(png: bytes) -> tuple[int, int, int]:
    """Width, height and colour type of a PNG, from its IHDR."""
    if not png.startswith(_PNG_SIGNATURE) or png[12:16] != b"IHDR":
        raise ValueError("not a PNG")
    width, height, _depth, colour = struct.unpack(">IIBB", png[16:26])
    return width, height, colour


def _chunk(kind: bytes, body: bytes) -> bytes:
    crc = struct.pack(">I", zlib.crc32(kind + body))
    return struct.pack(">I", len(body)) + kind + body + crc


def _top_rows(png: bytes, rows: int) -> bytes:
    """A PNG of just the top ``rows`` rows (rows only depend on those above them)."""
    width, _height, colour = _header(png)
    idat, pos = bytearray(), len(_PNG_SIGNATURE)
    while pos + 8 <= len(png):
        length, kind = struct.unpack(">I4s", png[pos : pos + 8])
        if kind == b"IDAT":
            idat += png[pos + 8 : pos + 8 + length]
        pos += 12 + length
    stride = width * {0: 1, 2: 3, 4: 2, 6: 4}[colour] + 1
    prefix = zlib.decompressobj().decompress(bytes(idat), stride * rows)
    head = struct.pack(">IIBBBBB", width, rows, 8, colour, 0, 0, 0)
    return (
        _PNG_SIGNATURE
        + _chunk(b"IHDR", head)
        + _chunk(b"IDAT", zlib.compress(prefix))
        + _chunk(b"IEND", b"")
    )


def check_state(state: ScreenState) -> None:
    """The composited frames match the chrome size; a full screen's notch band is black.

    Raises :class:`ValueError` otherwise.
    """
    want = (round(state.size.width * state.size.scale), round(state.size.height * state.size.scale))
    last = b""
    for index in (0, len(state.clip) - 1):
        frame = state.clip[index]
        try:
            last = Path(frame.path).read_bytes()
            got = _header(last)[:2]
        except (OSError, ValueError) as exc:
            raise ValueError(
                f"screen {state.state!r}: unreadable frame {frame.path}: {exc}"
            ) from None
        if got != want:
            raise ValueError(
                f"screen {state.state!r}: frame is {got[0]}x{got[1]}, "
                f"{state.chrome} chrome needs {want[0]}x{want[1]}"
            )
    if state.chrome != "fullscreen":
        return
    band = round(NOTCH_BAND_PX * state.size.scale)
    try:
        top = load_rgb(_top_rows(last, band))
    except (zlib.error, struct.error, KeyError, ValueError):
        top = None
    if top is None:
        raise ValueError(f"screen {state.state!r}: cannot read the notch band of the still")
    if any(pixel != (0, 0, 0) for pixel in top.pixels):
        raise ValueError(
            f"screen {state.state!r}: the top {NOTCH_BAND_PX}px band is not pure black"
        )
