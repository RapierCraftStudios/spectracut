# SPDX-License-Identifier: AGPL-3.0-or-later
"""On-screen events with exact edit frames: the raw material for sound cues.

SpectraCut knows what happens on screen (spike § Sound design and mix, "Where cues come
from"), so cue placement needs no guessing:

- the :class:`~spectracut.edit.Edit` gives cuts, transitions and speed ramps
  (:func:`edit_events`);
- a shot's :class:`~spectracut.spec.recipe.Recipe` gives camera moves from its
  ``camera.move`` keys, with speed and distance (:func:`recipe_events`);
- a live-UI capture's ``events.json`` (``packages/capture``) gives every click, keystroke,
  scroll, navigation and drag (:func:`capture_events`);
- motion typography and effects supply ``title`` and ``effect`` events directly.

Every event is mapped onto the edit timeline with :class:`fractions.Fraction`, so
23.976 fps and capture timestamps that fall between frames never drift.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping, Sequence
from fractions import Fraction
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, JsonValue, field_validator

from spectracut.edit.timeline import Clip, Edit, Frame, Gap, Track, Transition
from spectracut.spec._base import StrictModel, ensure_finite
from spectracut.spec.recipe import Recipe

EventKind = Literal[
    "cut",
    "transition",
    "speed_ramp",
    "camera_move",
    "click",
    "keystroke",
    "scroll",
    "navigation",
    "drag",
    "title",
    "effect",
]
EventSource = Literal["edit", "recipe", "capture", "typography"]
UI_KINDS: frozenset[str] = frozenset({"click", "keystroke", "scroll", "navigation", "drag"})

CaptureEvents = Sequence[Mapping[str, Any]] | Path | str
"""Parsed capture ``events.json`` entries, or the path of that file."""


class SoundEvent(StrictModel):
    """One timed on-screen event, at an absolute frame of the edit."""

    kind: EventKind
    frame: Frame
    duration_frames: int = Field(default=0, ge=0)
    source: EventSource
    params: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("params")
    @classmethod
    def _finite(cls, v: dict[str, JsonValue]) -> dict[str, JsonValue]:
        ensure_finite(v, "params")
        return v


def _sort_key(event: SoundEvent) -> tuple[int, str, int, str]:
    return (
        event.frame,
        event.kind,
        event.duration_frames,
        json.dumps(event.params, sort_keys=True),
    )


def _find_clip(edit: Edit, clip_id: str) -> tuple[Track, Clip]:
    for track in edit.tracks:
        for clip in track.clips():
            if clip.id == clip_id:
                return track, clip
    raise KeyError(f"no clip {clip_id!r} in edit {edit.id!r}")


def _source_to_edit(clip: Clip, start: int, source_frame: Fraction) -> int | None:
    """Edit frame of a source frame of ``clip`` (placed at ``start``); None if outside it."""
    rel = (source_frame - clip.source_in) / Fraction(clip.speed)
    frame = round(rel)
    if frame < 0 or frame >= clip.duration:
        return None
    return start + frame


# ------------------------------------------------------------------------------ edit


def edit_events(edit: Edit) -> list[SoundEvent]:
    """Cuts, transitions and speed ramps of the edit's picture tracks."""
    seen: set[tuple[str, int, int]] = set()
    events: list[SoundEvent] = []

    def add(event: SoundEvent) -> None:
        key = (event.kind, event.frame, event.duration_frames)
        if key not in seen:
            seen.add(key)
            events.append(event)

    for track in edit.tracks:
        if track.kind != "video":
            continue
        at = 0
        previous: Clip | Gap | None = None
        pending: Transition | None = None
        for item in track.items:
            if isinstance(item, Transition):
                pending = item
                continue
            if previous is not None:
                if pending is not None:
                    add(
                        SoundEvent(
                            kind="transition",
                            frame=at - pending.in_offset,
                            duration_frames=pending.in_offset + pending.out_offset,
                            source="edit",
                            params={"transition": pending.kind, "boundary": at},
                        )
                    )
                elif not (isinstance(previous, Gap) and isinstance(item, Gap)):
                    add(SoundEvent(kind="cut", frame=at, source="edit", params={"track": track.id}))
            pending = None
            if isinstance(item, Clip):
                for ramp in item.ramps:
                    add(
                        SoundEvent(
                            kind="speed_ramp",
                            frame=at + ramp.start,
                            duration_frames=ramp.end - ramp.start,
                            source="edit",
                            params={"speed_from": ramp.speed_from, "speed_to": ramp.speed_to},
                        )
                    )
            previous = item
            at += item.duration
    return sorted(events, key=_sort_key)


# ---------------------------------------------------------------------------- recipe


def _vec(value: Any) -> tuple[float, float, float] | None:
    if not isinstance(value, list | tuple) or len(value) != 3:
        return None
    if not all(isinstance(v, int | float) and not isinstance(v, bool) for v in value):
        return None
    return (float(value[0]), float(value[1]), float(value[2]))


def recipe_events(recipe: Recipe, edit: Edit, clip_id: str) -> list[SoundEvent]:
    """Camera moves of a shot recipe, placed where clip ``clip_id`` sits in the edit.

    Each pair of consecutive ``camera.move`` keys with a location change is one move;
    ``distance`` is in scene units and ``speed`` in scene units per second of edit time.
    """
    track, clip = _find_clip(edit, clip_id)
    start = track.record_start(clip_id)
    events: list[SoundEvent] = []
    for step in recipe.steps:
        if step.tool != "camera.move":
            continue
        keys = step.args.get("keys")
        if not isinstance(keys, list):
            continue
        located: list[tuple[int, tuple[float, float, float]]] = []
        for key in keys:
            if not isinstance(key, dict):
                continue
            frame, loc = key.get("frame"), _vec(key.get("location"))
            if isinstance(frame, int) and not isinstance(frame, bool) and loc is not None:
                located.append((frame, loc))
        located.sort(key=lambda k: k[0])
        for (f0, l0), (f1, l1) in zip(located, located[1:], strict=False):
            distance = math.dist(l0, l1)
            a = _source_to_edit(clip, start, Fraction(f0))
            b = _source_to_edit(clip, start, Fraction(f1))
            if distance <= 0.0 or a is None or b is None or b <= a:
                continue
            seconds = float(edit.rate.seconds(b - a))
            events.append(
                SoundEvent(
                    kind="camera_move",
                    frame=a,
                    duration_frames=b - a,
                    source="recipe",
                    params={
                        "distance": distance,
                        "speed": distance / seconds,
                        "easing": step.args.get("easing") or "linear",
                        "shot": recipe.shot_id,
                    },
                )
            )
    return sorted(events, key=_sort_key)


# --------------------------------------------------------------------------- capture


def _load_capture(events: CaptureEvents) -> list[Mapping[str, Any]]:
    if isinstance(events, str | Path):
        data = json.loads(Path(events).read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError(f"{events}: capture events must be a JSON list")
        return [e for e in data if isinstance(e, dict)]
    return list(events)


_CAPTURE_PARAMS = ("x", "y", "key", "scheduledMs", "deltaY", "url", "phase")


def capture_events(
    events: CaptureEvents, edit: Edit, clip_id: str, fps: int | Fraction | None = None
) -> list[SoundEvent]:
    """UI interactions of a capture, placed where clip ``clip_id`` sits in the edit.

    The capture clip's source frames run at the edit rate from capture time 0. An event's
    ``timeMs`` places it exactly; ``frame`` (at the capture ``fps``) is used when it has
    no ``timeMs``. Events outside the clip's in/out range are dropped.
    """
    track, clip = _find_clip(edit, clip_id)
    start = track.record_start(clip_id)
    out: list[SoundEvent] = []
    for raw in _load_capture(events):
        kind = raw.get("type")
        if kind not in UI_KINDS:
            raise ValueError(f"unknown capture event type {kind!r}")
        time_ms = raw.get("timeMs")
        if isinstance(time_ms, int | float) and not isinstance(time_ms, bool):
            seconds = Fraction(time_ms) / 1000
        elif isinstance(raw.get("frame"), int) and fps is not None:
            seconds = Fraction(raw["frame"]) / Fraction(fps)
        else:
            raise ValueError(f"capture event {raw!r} has no timeMs (or frame and fps)")
        frame = _source_to_edit(clip, start, seconds * edit.rate.fps)
        if frame is None:
            continue
        params: dict[str, JsonValue] = {
            name: raw[name] for name in _CAPTURE_PARAMS if raw.get(name) is not None
        }
        params["time_ms"] = float(seconds * 1000)
        out.append(SoundEvent(kind=kind, frame=frame, source="capture", params=params))
    return sorted(out, key=_sort_key)


# --------------------------------------------------------------------------- collect


def collect_events(
    edit: Edit,
    *,
    recipes: Mapping[str, Recipe] | None = None,
    captures: Mapping[str, CaptureEvents] | None = None,
    capture_fps: int | Fraction | None = None,
    extra: Iterable[SoundEvent] = (),
) -> list[SoundEvent]:
    """Every event of the edit, sorted by frame.

    ``recipes`` and ``captures`` are keyed by the id of the clip that shows them;
    ``extra`` carries events supplied directly (titles, effects).
    """
    events = edit_events(edit)
    for clip_id, recipe in (recipes or {}).items():
        events += recipe_events(recipe, edit, clip_id)
    for clip_id, capture in (captures or {}).items():
        events += capture_events(capture, edit, clip_id, capture_fps)
    events += list(extra)
    return sorted(events, key=_sort_key)
