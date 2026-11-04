# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``Edit``: one timeline from the animatic through finishing.

An ``Edit`` is the typed, frozen view of an OpenTimelineIO Timeline (spike § Production
pipeline; § Built on proven open source). It maps one-to-one onto OTIO through
:mod:`spectracut.edit.otio_io`; SpectraCut-only fields (ids, shot hashes, speed ramps,
audio links) travel in OTIO ``metadata["spectracut"]``, so there is no custom format.

Timing follows OTIO semantics and is held in **integer frames** at the edit's rational
:class:`FrameRate` (23.976 fps is exactly 24000/1001), so it never drifts:

- a clip's ``source_in``/``source_out`` are its in/out points in source frames (OTIO
  ``source_range``); it occupies ``source_out - source_in`` frames on its track, and
  ``speed`` scales how fast the media plays inside that range (OTIO ``LinearTimeWarp``);
- a transition overlaps its neighbours and does not change the track's duration;
- a clip may nest another ``Edit`` (shot → scene → sequence → episode). Durations are
  derived, never stored, so a changed shot changes every enclosing duration.
"""

from __future__ import annotations

from fractions import Fraction
from math import gcd
from typing import Annotated, Literal

from pydantic import Field, model_validator

from spectracut.spec._base import ContentHash, Slug, StrictModel, content_hash, ensure_unique_ids

Level = Literal["shot", "scene", "sequence", "episode"]
TrackKind = Literal["video", "audio"]
TransitionKind = Literal["dissolve", "wipe", "custom"]

_NTSC = (24000, 30000, 48000, 60000, 120000)


class FrameRate(StrictModel):
    """An exact frame rate ``numerator / denominator`` frames per second, in lowest terms."""

    numerator: int = Field(gt=0)
    denominator: int = Field(default=1, gt=0)

    @model_validator(mode="after")
    def _lowest_terms(self) -> FrameRate:
        if gcd(self.numerator, self.denominator) != 1:
            raise ValueError("frame rate must be in lowest terms")
        return self

    @property
    def fps(self) -> Fraction:
        return Fraction(self.numerator, self.denominator)

    def __float__(self) -> float:
        return self.numerator / self.denominator

    def seconds(self, frames: int) -> Fraction:
        """Exact time of ``frames`` frames."""
        return Fraction(frames) / self.fps

    def frames(self, seconds: Fraction | int) -> int:
        """Nearest frame to ``seconds`` (round half to even)."""
        return round(Fraction(seconds) * self.fps)

    @classmethod
    def of(cls, fps: Fraction | int) -> FrameRate:
        f = Fraction(fps)
        return cls(numerator=f.numerator, denominator=f.denominator)

    @classmethod
    def from_float(cls, rate: float) -> FrameRate:
        """Recover an exact rate from a float such as OTIO's ``23.976023976023978``."""
        if rate <= 0:
            raise ValueError(f"frame rate must be positive, got {rate}")
        for num in _NTSC:
            if abs(rate - num / 1001) < 1e-3:
                return cls(numerator=num, denominator=1001)
        if abs(rate - round(rate)) < 1e-6:
            return cls(numerator=round(rate))
        return cls.of(Fraction(rate).limit_denominator(1001))


FPS_23_976 = FrameRate(numerator=24000, denominator=1001)
FPS_24 = FrameRate(numerator=24)
FPS_25 = FrameRate(numerator=25)
FPS_30 = FrameRate(numerator=30)
FPS_60 = FrameRate(numerator=60)

Frame = Annotated[int, Field(ge=0)]


class Marker(StrictModel):
    """A named point or range; frames are in the owner's time space (clip: source frames)."""

    name: str = ""
    frame: Frame
    duration: Frame = 0
    color: str = "RED"
    note: str = ""


class SpeedRamp(StrictModel):
    """A speed change across record frames ``start``–``end`` of a clip (relative to its in)."""

    start: Frame
    end: Frame
    speed_from: float
    speed_to: float

    @model_validator(mode="after")
    def _ordered(self) -> SpeedRamp:
        if self.end <= self.start:
            raise ValueError("speed ramp end must be after start")
        return self


class Clip(StrictModel):
    """A shot (or nested edit) placed on a track with in/out points."""

    type: Literal["clip"] = "clip"
    id: Slug
    name: str = ""
    shot_id: Slug | None = None
    shot_hash: ContentHash | None = None
    media_ref: str = ""
    source_in: Frame = 0
    source_out: int | None = None
    """Exclusive out point; ``None`` only for a nested edit, meaning its full length."""
    speed: float = 1.0
    ramps: list[SpeedRamp] = Field(default_factory=list)
    markers: list[Marker] = Field(default_factory=list)
    nested: Edit | None = None
    link: Slug | None = None
    """On an audio clip: id of the picture clip it belongs to (J/L cuts)."""

    @model_validator(mode="after")
    def _range(self) -> Clip:
        if self.source_out is None:
            if self.nested is None:
                raise ValueError("source_out is required unless the clip nests an edit")
            if self.source_in != 0:
                raise ValueError("a nested clip without source_out must start at 0")
        elif self.source_out <= self.source_in:
            raise ValueError("source_out must be after source_in")
        if self.nested is not None and self.media_ref:
            raise ValueError("a nested clip has no media_ref")
        for ramp in self.ramps:
            if ramp.end > self.duration:
                raise ValueError("speed ramp runs past the end of the clip")
        return self

    @property
    def duration(self) -> int:
        """Frames the clip occupies on its track."""
        if self.source_out is None:
            assert self.nested is not None
            return self.nested.duration
        return self.source_out - self.source_in


class Gap(StrictModel):
    """Empty frames on a track."""

    type: Literal["gap"] = "gap"
    duration: int = Field(gt=0)


class Transition(StrictModel):
    """An overlap between the clips before and after it."""

    type: Literal["transition"] = "transition"
    name: str = ""
    kind: TransitionKind = "dissolve"
    in_offset: Frame = 0
    out_offset: Frame = 0


Item = Annotated[Clip | Gap | Transition, Field(discriminator="type")]


class Track(StrictModel):
    """An ordered run of clips, gaps and transitions."""

    id: Slug
    kind: TrackKind = "video"
    items: list[Item] = Field(default_factory=list)

    @model_validator(mode="after")
    def _items(self) -> Track:
        ensure_unique_ids([i for i in self.items if isinstance(i, Clip)], "clip")
        for n, item in enumerate(self.items):
            if not isinstance(item, Transition):
                continue
            before = self.items[n - 1] if n > 0 else None
            after = self.items[n + 1] if n + 1 < len(self.items) else None
            if not isinstance(before, Clip) or not isinstance(after, Clip):
                raise ValueError(f"track {self.id}: a transition must sit between two clips")
            if item.in_offset > before.duration or item.out_offset > after.duration:
                raise ValueError(f"track {self.id}: transition is longer than its clips")
        return self

    @property
    def duration(self) -> int:
        return sum(i.duration for i in self.items if not isinstance(i, Transition))

    def clips(self) -> list[Clip]:
        return [i for i in self.items if isinstance(i, Clip)]

    def record_start(self, clip_id: str) -> int:
        """Frame on this track where clip ``clip_id`` starts."""
        at = 0
        for item in self.items:
            if isinstance(item, Clip) and item.id == clip_id:
                return at
            if not isinstance(item, Transition):
                at += item.duration
        raise KeyError(clip_id)


class Edit(StrictModel):
    """A timeline: tracks of clips at one frame rate, nestable into larger edits."""

    id: Slug
    name: str = ""
    rate: FrameRate = FPS_24
    level: Level | None = None
    start_frame: Frame = 0
    """Timeline start (OTIO ``global_start_time``), e.g. 01:00:00:00 from Resolve."""
    tracks: list[Track] = Field(default_factory=list)
    markers: list[Marker] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistent(self) -> Edit:
        ensure_unique_ids(self.tracks, "track")
        video = {c.id for t in self.tracks if t.kind == "video" for c in t.clips()}
        for track in self.tracks:
            for clip in track.clips():
                if clip.nested is not None and clip.nested.rate != self.rate:
                    raise ValueError(f"nested edit in clip {clip.id} must share rate")
                if clip.link is not None and (track.kind != "audio" or clip.link not in video):
                    raise ValueError(f"clip {clip.id}: link must name a picture clip")
        return self

    @property
    def duration(self) -> int:
        """Length in frames: the longest track."""
        return max((t.duration for t in self.tracks), default=0)

    @property
    def seconds(self) -> Fraction:
        return self.rate.seconds(self.duration)

    @property
    def content_hash(self) -> str:
        return content_hash(self)

    def audio_offset(self, clip_id: str) -> int:
        """Frames an audio clip starts relative to its linked picture clip.

        Negative is a J cut (sound leads the picture), positive an L cut (sound trails).
        """
        audio_start: int | None = None
        link: str | None = None
        for track in self.tracks:
            if track.kind == "audio":
                for clip in track.clips():
                    if clip.id == clip_id:
                        audio_start, link = track.record_start(clip_id), clip.link
        if audio_start is None or link is None:
            raise KeyError(f"no linked audio clip {clip_id!r}")
        for track in self.tracks:
            if track.kind == "video" and any(c.id == link for c in track.clips()):
                return audio_start - track.record_start(link)
        raise KeyError(link)


Clip.model_rebuild()
