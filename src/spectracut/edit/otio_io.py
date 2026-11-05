# SPDX-License-Identifier: AGPL-3.0-or-later
"""Export an :class:`Edit` to OpenTimelineIO and import it back.

The mapping is one-to-one: ``Edit`` → ``Timeline`` (or a nested ``Stack``), ``Track`` →
``Track``, ``Clip`` → ``Clip`` (or ``Stack`` when it nests an edit), ``Gap`` → ``Gap``,
``Transition`` → ``Transition``, ``Marker`` → ``Marker`` and ``speed`` →
``LinearTimeWarp``. SpectraCut-only fields go under ``metadata["spectracut"]``; other
metadata (for example Resolve's) is ignored on import.

On import the OTIO ranges win over our metadata, so cuts retimed in DaVinci Resolve or
Premiere come back applied. Every time is rescaled to the edit rate and rounded to the
nearest whole frame.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import opentimelineio as otio

from spectracut.edit.timeline import (
    Clip,
    Edit,
    FrameRate,
    Gap,
    Item,
    Marker,
    SpeedRamp,
    Track,
    Transition,
)

NS = "spectracut"

_TRANSITION_TO_OTIO = {
    "dissolve": otio.schema.TransitionTypes.SMPTE_Dissolve,
    "wipe": "SMPTE_Wipe",
    "custom": otio.schema.TransitionTypes.Custom,
}
_TRANSITION_FROM_OTIO = {v: k for k, v in _TRANSITION_TO_OTIO.items()}


# --- export ------------------------------------------------------------------------------


def _rt(frames: int, rate: FrameRate) -> Any:
    return otio.opentime.RationalTime(frames, float(rate))


def _range(start: int, duration: int, rate: FrameRate) -> Any:
    return otio.opentime.TimeRange(_rt(start, rate), _rt(duration, rate))


def _markers_to_otio(markers: list[Marker], rate: FrameRate) -> list[Any]:
    return [
        otio.schema.Marker(
            name=m.name,
            marked_range=_range(m.frame, m.duration, rate),
            color=m.color,
            comment=m.note,
        )
        for m in markers
    ]


def _edit_meta(edit: Edit) -> dict[str, Any]:
    return {
        "id": edit.id,
        "level": edit.level,
        "rate": [edit.rate.numerator, edit.rate.denominator],
        "start_frame": edit.start_frame,
    }


def _tracks_to_otio(edit: Edit) -> list[Any]:
    out = []
    for track in edit.tracks:
        kind = otio.schema.TrackKind.Video if track.kind == "video" else otio.schema.TrackKind.Audio
        t = otio.schema.Track(name=track.id, kind=kind, metadata={NS: {"id": track.id}})
        for item in track.items:
            t.append(_item_to_otio(item, edit.rate))
        out.append(t)
    return out


def _item_to_otio(item: Item, rate: FrameRate) -> Any:
    if isinstance(item, Gap):
        return otio.schema.Gap(duration=_rt(item.duration, rate))
    if isinstance(item, Transition):
        return otio.schema.Transition(
            name=item.name,
            transition_type=_TRANSITION_TO_OTIO[item.kind],
            in_offset=_rt(item.in_offset, rate),
            out_offset=_rt(item.out_offset, rate),
        )
    meta: dict[str, Any] = {
        "id": item.id,
        "shot_id": item.shot_id,
        "shot_hash": item.shot_hash,
        "link": item.link,
        "ramps": [r.model_dump(mode="json") for r in item.ramps],
    }
    source_range = (
        None
        if item.source_out is None
        else _range(item.source_in, item.source_out - item.source_in, rate)
    )
    effects = [] if item.speed == 1.0 else [otio.schema.LinearTimeWarp(time_scalar=item.speed)]
    markers = _markers_to_otio(item.markers, rate)
    if item.nested is not None:
        meta["edit"] = {
            **_edit_meta(item.nested),
            "name": item.nested.name,
            "markers": [m.model_dump(mode="json") for m in item.nested.markers],
        }
        return otio.schema.Stack(
            name=item.name,
            children=_tracks_to_otio(item.nested),
            source_range=source_range,
            markers=markers,
            effects=effects,
            metadata={NS: meta},
        )
    ref = (
        otio.schema.ExternalReference(target_url=item.media_ref)
        if item.media_ref
        else otio.schema.MissingReference()
    )
    return otio.schema.Clip(
        name=item.name,
        media_reference=ref,
        source_range=source_range,
        metadata={NS: meta},
        effects=effects,
        markers=markers,
    )


def to_otio(edit: Edit) -> Any:
    """Return the edit as an ``opentimelineio.schema.Timeline``."""
    timeline = otio.schema.Timeline(
        name=edit.name,
        tracks=_tracks_to_otio(edit),
        global_start_time=_rt(edit.start_frame, edit.rate),
        metadata={NS: _edit_meta(edit)},
    )
    timeline.tracks.markers.extend(_markers_to_otio(edit.markers, edit.rate))
    return timeline


def write_otio(edit: Edit, path: str | Path) -> None:
    """Write the edit as an ``.otio`` file."""
    otio.adapters.write_to_file(to_otio(edit), str(path))


# --- import ------------------------------------------------------------------------------


def _frames(rt: Any, rate: FrameRate) -> int:
    value: float = rt.rescaled_to(float(rate)).value
    return round(value)


def _ns(obj: Any) -> dict[str, Any]:
    meta = obj.metadata.get(NS)
    return dict(meta) if meta is not None else {}


def _slug(text: str, fallback: str) -> str:
    slug = re.sub(r"[^a-z0-9_-]+", "-", text.lower()).strip("-_")
    return slug[:128] if slug else fallback


def _markers_from_otio(markers: Any, rate: FrameRate) -> list[Marker]:
    return [
        Marker(
            name=m.name,
            frame=_frames(m.marked_range.start_time, rate),
            duration=_frames(m.marked_range.duration, rate),
            color=m.color,
            note=m.comment,
        )
        for m in markers
    ]


def _speed(obj: Any) -> float:
    speed = 1.0
    for effect in obj.effects:
        if isinstance(effect, otio.schema.LinearTimeWarp):
            speed *= effect.time_scalar
    return speed


def _tracks_from_otio(children: Any, rate: FrameRate) -> list[Track]:
    tracks = []
    used: set[str] = set()
    for n, t in enumerate(children):
        if not isinstance(t, otio.schema.Track):
            continue
        kind = "audio" if t.kind == otio.schema.TrackKind.Audio else "video"
        tid = str(_ns(t).get("id") or _slug(t.name, f"{kind[0]}{n + 1}"))
        if tid in used:
            tid = f"{tid}-{n + 1}"
        used.add(tid)
        items = [_item_from_otio(child, rate, f"{tid}-{i + 1}") for i, child in enumerate(t)]
        tracks.append(Track(id=tid, kind=kind, items=items))
    return tracks


def _item_from_otio(obj: Any, rate: FrameRate, fallback_id: str) -> Item:
    if isinstance(obj, otio.schema.Gap):
        return Gap(duration=_frames(obj.duration(), rate))
    if isinstance(obj, otio.schema.Transition):
        return Transition(
            name=obj.name,
            kind=_TRANSITION_FROM_OTIO.get(obj.transition_type, "custom"),
            in_offset=_frames(obj.in_offset, rate),
            out_offset=_frames(obj.out_offset, rate),
        )
    meta = _ns(obj)
    source_in, source_out = 0, None
    trimmed = obj.source_range
    if trimmed is None and not isinstance(obj, otio.schema.Stack):
        trimmed = obj.trimmed_range()  # media's available range
    if trimmed is not None:
        source_in = _frames(trimmed.start_time, rate)
        source_out = source_in + _frames(trimmed.duration, rate)
    common: dict[str, Any] = {
        "id": meta.get("id") or _slug(obj.name, fallback_id),
        "name": obj.name,
        "shot_id": meta.get("shot_id"),
        "shot_hash": meta.get("shot_hash"),
        "link": meta.get("link"),
        "ramps": [SpeedRamp.model_validate(dict(r)) for r in meta.get("ramps") or []],
        "source_in": source_in,
        "source_out": source_out,
        "speed": _speed(obj),
        "markers": _markers_from_otio(obj.markers, rate),
    }
    if isinstance(obj, otio.schema.Stack):
        emeta = dict(meta.get("edit") or {})
        nested = Edit(
            id=emeta.get("id") or common["id"],
            name=emeta.get("name", obj.name),
            rate=rate,
            level=emeta.get("level"),
            start_frame=emeta.get("start_frame", 0),
            tracks=_tracks_from_otio(obj, rate),
            markers=[Marker.model_validate(dict(m)) for m in emeta.get("markers") or []],
        )
        return Clip(**common, nested=nested)
    ref = obj.media_reference
    media_ref = ref.target_url if isinstance(ref, otio.schema.ExternalReference) else ""
    return Clip(**common, media_ref=media_ref)


def _rate_of(timeline: Any) -> FrameRate:
    meta = _ns(timeline)
    if meta.get("rate"):
        num, den = meta["rate"]
        return FrameRate(numerator=int(num), denominator=int(den))
    if timeline.global_start_time is not None:
        return FrameRate.from_float(timeline.global_start_time.rate)
    for clip in timeline.find_clips():
        if clip.source_range is not None:
            return FrameRate.from_float(clip.source_range.duration.rate)
    return FrameRate(numerator=24)


def from_otio(timeline: Any, rate: FrameRate | None = None) -> Edit:
    """Build an :class:`Edit` from an ``opentimelineio.schema.Timeline``."""
    rate = rate or _rate_of(timeline)
    meta = _ns(timeline)
    start = timeline.global_start_time
    return Edit(
        id=meta.get("id") or _slug(timeline.name, "edit"),
        name=timeline.name,
        rate=rate,
        level=meta.get("level"),
        start_frame=_frames(start, rate) if start is not None else 0,
        tracks=_tracks_from_otio(timeline.tracks, rate),
        markers=_markers_from_otio(timeline.tracks.markers, rate),
    )


def read_otio(path: str | Path, rate: FrameRate | None = None) -> Edit:
    """Read an ``.otio`` file into an :class:`Edit`."""
    timeline = otio.adapters.read_from_file(str(path))
    if not isinstance(timeline, otio.schema.Timeline):
        raise ValueError(f"{path}: expected an OTIO Timeline, got {type(timeline).__name__}")
    return from_otio(timeline, rate)
