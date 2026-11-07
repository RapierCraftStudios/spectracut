# SPDX-License-Identifier: AGPL-3.0-or-later
"""Device shot choreography (#369): multi-cut camera paths anchored to UI elements.

A :class:`ShotPath` is a device shot's camera as cuts of keys. Each key sets the
frame, camera location, aim point, focus point, f-stop, lens and roll; a point is
``[anchor, [fwd, side, up] metres]`` where the anchor is ``SC`` (the screen centre) or
a role the path binds to the capture's DOM anchors by selector pattern. The path
carries no screen coordinates: :func:`resolve_anchors` takes them from the capture
(``ScreenCapture.anchors``) and fails closed when a role matches no anchor or several.

The world basis follows the device kit's measured screen (``ScreenFrame``): ``fwd`` is
the horizontal screen normal, ``side = (-fwd.y, fwd.x, 0)``, ``up = Z``; on the screen
``scrX = side`` and ``scrUp = normal x side`` (pointing up), and the display size is the
kit's ``width`` x ``height``. :func:`path_keys` emits one ``camera.move`` key per path
key; the keys of consecutive cuts are one frame apart, so the host holds them
(``CONSTANT``) and the cut is hard. :func:`screen_timeline` plays the capture clip in
real time inside each cut, anchored to a capture event, and never ends the click cut
on a frame that went back to the pre-click state.

:func:`framing_problems` is the pure framing check of the preview gate and
:func:`twitch_frames` the frame-to-frame PSNR check of the rendered sequence.

Imports stay on models and templates: never the producer or the queue (see d141608).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal, Self

from pydantic import Field, JsonValue, model_validator

from spectracut.qa.common import load_gray
from spectracut.spec._base import NonEmptyStr, StrictModel
from spectracut.templates.device_kit import DeviceAsset, in_frame, look_rotation

if TYPE_CHECKING:
    from spectracut.studio.departments.capture import ScreenCapture
    from spectracut.studio.departments.screens import ScreenFrame as CaptureFrame

Vec3 = tuple[float, float, float]

SCREEN_CENTRE: Final = "SC"
"""The anchor of the screen centre (the capture's reserved ``SC``)."""
PASTE_KEY: Final = "⌘V"
"""The ``keystroke`` key the capture records for a paste."""

Point = tuple[str, tuple[float, float, float]]
"""``(anchor, (fwd, side, up))``: an anchor plus an offset in metres."""


class PathKey(StrictModel):
    """One camera key: frame, location, aim, focus (defaults to aim), f-stop, lens, roll."""

    f: int = Field(ge=1)
    loc: Point
    aim: Point
    focus: Point | None = None
    fstop: float = Field(ge=0.1, le=128)
    lens: float = Field(gt=0)
    roll: float = 0.0
    """Degrees, added to the look-at Euler Y."""

    def points(self) -> tuple[Point, ...]:
        return (self.loc, self.aim, self.focus if self.focus is not None else self.aim)


class ScreenSync(StrictModel):
    """How a cut's clip window anchors to the capture: the clip moment of ``event``
    lands on cut-local frame ``at`` (0 is the cut's first frame)."""

    event: Literal["start", "paste", "click", "end"]
    at: int = Field(ge=0)


class Cut(StrictModel):
    name: NonEmptyStr
    start: int = Field(ge=1)
    end: int = Field(ge=1)
    screen: ScreenSync
    keys: list[PathKey] = Field(min_length=2)


class ShotPath(StrictModel):
    """A device shot's camera path: cuts contiguous over frames 1..``frames``."""

    id: NonEmptyStr
    frames: int = Field(ge=2)
    fps: int = Field(default=24, gt=0)
    roles: dict[str, list[NonEmptyStr]] = Field(default_factory=dict)
    """Role -> case-insensitive selector substrings, tried in order."""
    cuts: list[Cut] = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if SCREEN_CENTRE in self.roles:
            raise ValueError(f"role name {SCREEN_CENTRE!r} is reserved for the screen centre")
        for role, patterns in self.roles.items():
            if not patterns:
                raise ValueError(f"role {role!r} has no selector pattern")
        expect = 1
        for cut in self.cuts:
            if cut.start != expect:
                raise ValueError(f"cut {cut.name!r} starts at {cut.start}, expected {expect}")
            if cut.end < cut.start:
                raise ValueError(f"cut {cut.name!r} ends before it starts")
            if cut.screen.at > cut.end - cut.start:
                raise ValueError(f"cut {cut.name!r}: screen sync frame is outside the cut")
            frames = [k.f for k in cut.keys]
            if frames != sorted(set(frames)):
                raise ValueError(f"cut {cut.name!r}: key frames must strictly increase")
            if frames[0] != cut.start or frames[-1] != cut.end:
                raise ValueError(
                    f"cut {cut.name!r}: keys must start at f{cut.start} and end at f{cut.end}"
                )
            for key in cut.keys:
                for anchor, _offset in key.points():
                    if anchor != SCREEN_CENTRE and anchor not in self.roles:
                        raise ValueError(
                            f"cut {cut.name!r} f{key.f}: unknown anchor {anchor!r} "
                            f"(roles: {', '.join(sorted(self.roles)) or 'none'})"
                        )
            expect = cut.end + 1
        if expect != self.frames + 1:
            raise ValueError(f"cuts cover frames 1..{expect - 1}, not 1..{self.frames}")
        return self

    def cut_starts(self) -> list[int]:
        return [c.start for c in self.cuts]

    def timing(self) -> list[dict[str, JsonValue]]:
        """``[{name, start, end}]`` per cut, as recorded on the shot."""
        return [{"name": c.name, "start": c.start, "end": c.end} for c in self.cuts]


def _key(
    f: int,
    loc: Point,
    aim: Point,
    fstop: float,
    lens: float,
    roll: float = 0.0,
    focus: Point | None = None,
) -> PathKey:
    return PathKey(f=f, loc=loc, aim=aim, focus=focus, fstop=fstop, lens=lens, roll=roll)


UI_RUN_3CUT: Final = ShotPath(
    id="ui-run-3cut",
    frames=132,
    fps=24,
    roles={
        "URL": ["#url", "url", "input"],
        "RUN": ["text=run", "run"],
        "CARDS": [".result-card", "card", "result"],
        "STATUS": ["text=status", "status"],
    },
    cuts=[
        Cut(
            name="A",
            start=1,
            end=40,
            screen=ScreenSync(event="paste", at=30),
            keys=[
                _key(
                    1,
                    ("SC", (0.42, -0.26, -0.085)),
                    ("SC", (0.12, -0.04, -0.115)),
                    2.8,
                    50,
                    focus=("SC", (0.24, -0.12, -0.12)),
                ),
                _key(40, ("SC", (0.36, -0.15, -0.035)), ("URL", (0, 0, 0)), 2.0, 60),
            ],
        ),
        Cut(
            name="B",
            start=41,
            end=72,
            screen=ScreenSync(event="click", at=8),
            keys=[
                _key(41, ("RUN", (0.2, -0.07, 0.02)), ("RUN", (0, 0, 0)), 2.8, 85, roll=-4),
                _key(72, ("RUN", (0.14, -0.05, 0.015)), ("RUN", (0, 0, 0)), 2.8, 85, roll=-2),
            ],
        ),
        Cut(
            name="C",
            start=73,
            end=132,
            screen=ScreenSync(event="end", at=59),
            keys=[
                _key(
                    73,
                    ("CARDS", (0.24, 0.34, 0.03)),
                    ("CARDS", (0, 0, 0)),
                    2.4,
                    55,
                    focus=("STATUS", (0, 0, 0)),
                ),
                _key(
                    132,
                    ("CARDS", (0.34, 0.1, 0.04)),
                    ("CARDS", (0, 0, 0)),
                    3.2,
                    60,
                    focus=("STATUS", (0, 0, 0)),
                ),
            ],
        ),
    ],
)
"""The approved three-cut UI run (#369, keys from the owner-approved shot01b): cut A a
macro rise from the deck edge with a focus pull onto the URL field, cut B an 85 mm push
on Run with -4 deg roll through the click and spinner, cut C an orbit onto the result
cards, focus on Status, landing at f/3.2."""

SHOT_PATHS: Final[dict[str, ShotPath]] = {UI_RUN_3CUT.id: UI_RUN_3CUT}
"""Every registered camera path, by id (the device shot variant that selects it)."""


# ---- anchors -----------------------------------------------------------------------------


def resolve_anchors(
    path: ShotPath, capture: ScreenCapture
) -> dict[str, tuple[str, tuple[float, float]]]:
    """Each role's capture anchor: ``role -> (selector, (u, v))``.

    A role takes the unique anchor whose selector contains its first pattern that
    matches any (case-insensitive). No match, or several, raises :class:`ValueError`
    naming the role and the captured selectors; there are no default coordinates.
    """
    named = {s: uv for s, uv in capture.anchors.items() if s != SCREEN_CENTRE}
    out: dict[str, tuple[str, tuple[float, float]]] = {}
    for role, patterns in path.roles.items():
        hits: list[str] = []
        for pattern in patterns:
            hits = [s for s in named if pattern.lower() in s.lower()]
            if hits:
                break
        if len(hits) != 1:
            why = "matches no anchor" if not hits else f"matches {len(hits)} anchors"
            raise ValueError(
                f"path {path.id!r} role {role!r} ({', '.join(patterns)}) {why}; captured "
                f"selectors: {', '.join(sorted(named)) or 'none'}"
            )
        u, v = named[hits[0]]
        out[role] = (hits[0], (float(u), float(v)))
    return out


def display_uv(uv: tuple[float, float], capture: ScreenCapture) -> tuple[float, float]:
    """Content-rect UV as display UV: through the chrome's content rect when the
    capture is composited into chrome, else unchanged."""
    if capture.screen is None:
        return uv
    from spectracut.studio.departments.screens import CHROME_SIZES, CONTENT_RECTS

    chrome = capture.screen.chrome
    rect, size = CONTENT_RECTS[chrome], CHROME_SIZES[chrome]
    return (
        (rect.x + uv[0] * rect.width) / size.width,
        (rect.y + uv[1] * rect.height) / size.height,
    )


def _sub(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _add(*vs: Vec3) -> Vec3:
    return (sum(v[0] for v in vs), sum(v[1] for v in vs), sum(v[2] for v in vs))


def _scale(v: Vec3, s: float) -> Vec3:
    return (v[0] * s, v[1] * s, v[2] * s)


def _unit(v: Vec3) -> Vec3:
    n = math.sqrt(v[0] ** 2 + v[1] ** 2 + v[2] ** 2)
    if n == 0:
        raise ValueError("degenerate screen basis")
    return (v[0] / n, v[1] / n, v[2] / n)


def _cross(a: Vec3, b: Vec3) -> Vec3:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def screen_basis(entry: DeviceAsset, device_location: Vec3) -> dict[str, Vec3]:
    """``SC``, ``fwd``, ``side``, ``up``, ``scrX`` and ``scrUp`` of the kit device's
    screen moved by ``device_location``."""
    frame = entry.frame
    normal = frame.normal
    fwd = _unit((normal[0], normal[1], 0.0))
    side = (-fwd[1], fwd[0], 0.0)
    scr_up = _unit(_cross(normal, side))
    if scr_up[2] < 0:
        scr_up = _scale(scr_up, -1.0)
    return {
        "SC": _add(frame.centre, device_location),
        "fwd": fwd,
        "side": side,
        "up": (0.0, 0.0, 1.0),
        "scrX": side,
        "scrUp": scr_up,
    }


def anchor_world(
    uv: tuple[float, float],
    capture: ScreenCapture,
    entry: DeviceAsset,
    device_location: Vec3,
) -> Vec3:
    """The world point of a content-rect ``uv`` on the device screen:
    ``SC + scrX*(u-0.5)*W + scrUp*(0.5-v)*H`` with the kit's display ``W`` x ``H``."""
    b = screen_basis(entry, device_location)
    u, v = display_uv(uv, capture)
    w, h = entry.frame.width, entry.frame.height
    return _add(b["SC"], _scale(b["scrX"], (u - 0.5) * w), _scale(b["scrUp"], (0.5 - v) * h))


def anchors_world(
    path: ShotPath,
    capture: ScreenCapture,
    entry: DeviceAsset,
    device_location: Vec3,
    anchors: Mapping[str, tuple[str, tuple[float, float]]] | None = None,
) -> dict[str, Vec3]:
    """``SC`` and every role of ``path`` in world space (roles via :func:`resolve_anchors`)."""
    resolved = anchors if anchors is not None else resolve_anchors(path, capture)
    out = {SCREEN_CENTRE: screen_basis(entry, device_location)["SC"]}
    for role, (_selector, uv) in resolved.items():
        out[role] = anchor_world(uv, capture, entry, device_location)
    return out


def point_world(point: Point, world: Mapping[str, Vec3], basis: Mapping[str, Vec3]) -> Vec3:
    """``anchor + fwd*F + side*S + up*U``."""
    anchor, (f, s, u) = point
    return _add(
        world[anchor], _scale(basis["fwd"], f), _scale(basis["side"], s), _scale(basis["up"], u)
    )


# ---- camera ------------------------------------------------------------------------------


def _r(v: float) -> float:
    return round(v, 5)


def path_keys(
    path: ShotPath, world: Mapping[str, Vec3], basis: Mapping[str, Vec3]
) -> list[JsonValue]:
    """One ``camera.move`` key per path key: frame, location, rotation (look-at with
    ``radians(roll)`` added to Euler Y), lens, focus distance (``|focus - loc|``), f-stop."""
    keys: list[JsonValue] = []
    for cut in path.cuts:
        for key in cut.keys:
            loc_p, aim_p, focus_p = key.points()
            loc = point_world(loc_p, world, basis)
            aim = point_world(aim_p, world, basis)
            focus = point_world(focus_p, world, basis)
            rx, ry, rz = look_rotation(loc, aim)
            keys.append(
                {
                    "frame": key.f,
                    "location": [_r(c) for c in loc],
                    "rotation": [_r(rx), _r(ry + math.radians(key.roll)), _r(rz)],
                    "lens": key.lens,
                    "focus_distance": _r(math.dist(loc, focus)),
                    "fstop": key.fstop,
                }
            )
    return keys


def framing_problems(
    path: ShotPath,
    world: Mapping[str, Vec3],
    basis: Mapping[str, Vec3],
    aspect: float,
) -> list[str]:
    """Keys whose aim or focus point projects outside the frame margin at their lens."""
    problems: list[str] = []
    for cut in path.cuts:
        for key in cut.keys:
            loc_p, aim_p, focus_p = key.points()
            loc = point_world(loc_p, world, basis)
            aim = point_world(aim_p, world, basis)
            focus = point_world(focus_p, world, basis)
            if not in_frame(loc, aim, key.lens, aspect, [aim, focus]):
                problems.append(
                    f"cut {cut.name} f{key.f}: aim {aim_p[0]} or focus {focus_p[0]} "
                    f"falls outside the frame at {key.lens:g} mm"
                )
    return problems


# ---- screen timeline -----------------------------------------------------------------------


def _event_frames(events: Sequence[object]) -> tuple[int | None, list[int]]:
    """``(paste frame, click frames)`` of a capture's ``events.json``."""
    paste: int | None = None
    clicks: list[int] = []
    for e in events:
        if not isinstance(e, dict) or not isinstance(e.get("frame"), int):
            continue
        if e.get("type") == "keystroke" and e.get("key") == PASTE_KEY and paste is None:
            paste = int(e["frame"])
        elif e.get("type") == "click":
            clicks.append(int(e["frame"]))
    return paste, sorted(clicks)


def screen_timeline(
    path: ShotPath, capture: ScreenCapture, events: Sequence[object]
) -> list[CaptureFrame]:
    """The capture frame on screen at every path frame (``path.frames`` of them).

    Each cut plays the clip in real time (``capture.fps`` -> ``path.fps``) from a window
    whose ``screen.event`` moment (the paste, the first click after it, or the clip's
    start or end) lands on the cut-local ``screen.at``, clamped to the clip. Inside a
    cut that crosses the click, a frame identical (sha256) to a pre-click frame is a
    reset and holds the last post-click frame instead, so the cut never ends on the
    reset state. Raises :class:`ValueError` when the sync event is not in the events.
    """
    clip = capture.screen.clip if capture.screen is not None else capture.clip
    last = len(clip) - 1
    paste, clicks = _event_frames(events)
    after = [c for c in clicks if paste is None or c >= paste]
    click = after[0] if after else None
    moments: dict[str, int | None] = {"start": 0, "end": last, "paste": paste, "click": click}
    pre = {f.sha256 for f in clip[:click]} if click is not None else set()
    out: list[CaptureFrame] = []
    for cut in path.cuts:
        moment = moments[cut.screen.event]
        if moment is None:
            raise ValueError(
                f"path {path.id!r} cut {cut.name!r} syncs to the {cut.screen.event} but the "
                f"capture {capture.key!r} recorded none"
            )
        picks: list[int] = []
        for i in range(cut.end - cut.start + 1):
            t = moment / capture.fps + (i - cut.screen.at) / path.fps
            picks.append(min(last, max(0, round(t * capture.fps))))
        held: CaptureFrame | None = None
        crosses = click is not None and picks[0] <= click < picks[-1]
        for idx in picks:
            frame = clip[idx]
            if crosses and click is not None and idx > click and frame.sha256 in pre:
                if held is not None:
                    frame = held
            held = frame
            out.append(frame)
    return out


# ---- twitch --------------------------------------------------------------------------------

PSNR_CAP: Final = 100.0


def psnr(a: Sequence[float], b: Sequence[float]) -> float:
    """Luma PSNR (dB) of two same-size frames in 0..1, capped at :data:`PSNR_CAP`."""
    if len(a) != len(b) or not a:
        return 0.0
    mse = sum((x - y) ** 2 for x, y in zip(a, b, strict=True)) / len(a)
    return PSNR_CAP if mse <= 0 else min(PSNR_CAP, 10 * math.log10(1.0 / mse))


def twitch_frames(
    frame_paths: Sequence[Path], cut_starts: Sequence[int], drop_db: float = 6.0
) -> list[int]:
    """Frames (1-based, ``frame_paths[0]`` is frame 1) a pair drops into: the PSNR of
    frame n-1 -> n is at least ``drop_db`` below both neighbouring pairs (the one there
    is at the ends) and n is not a cut start. Undecodable frames break the series."""
    grays = []
    for p in frame_paths:
        try:
            grays.append(load_gray(Path(p).read_bytes()))
        except OSError:
            grays.append(None)
    pairs: list[float | None] = []
    for a, b in zip(grays, grays[1:], strict=False):
        pairs.append(psnr(a.pixels, b.pixels) if a is not None and b is not None else None)
    starts = set(cut_starts)
    out: list[int] = []
    for i, value in enumerate(pairs):
        if value is None:
            continue
        frame = i + 2
        if frame in starts:
            continue
        neighbours = [pairs[j] for j in (i - 1, i + 1) if 0 <= j < len(pairs)]
        known = [n for n in neighbours if n is not None]
        if known and len(known) == len(neighbours) and all(value <= n - drop_db for n in known):
            out.append(frame)
    return out
