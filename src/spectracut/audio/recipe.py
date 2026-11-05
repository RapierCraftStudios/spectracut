# SPDX-License-Identifier: AGPL-3.0-or-later
"""``AudioRecipe``: the declarative sound of an edit (spike § Sound design and mix).

An ``AudioRecipe`` sits on an :class:`~spectracut.edit.Edit` and describes every cue, its
layered sounds, the bus it feeds (dialogue ``dx``, music ``mx`` or effects ``fx``) and its
fader automation. :mod:`spectracut.audio.ardour` writes it as an Ardour session and
:mod:`spectracut.audio.render` renders the master and the DX/MX/FX stems from it.

Timing is exact: a cue is anchored on an edit marker (by name) or an absolute frame, and
its start sample is computed with :class:`fractions.Fraction` from the edit's rational
frame rate, so 23.976 and 29.97 fps never drift.

A sound's cache key (:func:`sound_key`) hashes only its :class:`SoundSpec`. Moving a cue,
changing its gain, bus or automation does not change the sound, so it is not generated
again; only the mix is rebuilt.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Annotated, Literal

from pydantic import Field, JsonValue, field_validator, model_validator

from spectracut.edit.timeline import FPS_24, Edit, Frame, FrameRate
from spectracut.spec._base import (
    NonEmptyStr,
    Slug,
    StrictModel,
    content_hash,
    ensure_finite,
    ensure_unique_ids,
)

BusName = Literal["dx", "mx", "fx"]
BUS_NAMES: tuple[BusName, ...] = ("dx", "mx", "fx")

Gain = Annotated[float, Field(ge=0.0, le=4.0)]
"""Linear fader gain (1.0 is unity; Ardour's fader tops out near +12 dB)."""

GainDb = Annotated[float, Field(ge=-120.0, le=24.0)]


def db_to_gain(db: float) -> float:
    """Linear amplitude of ``db`` decibels."""
    return float(10.0 ** (db / 20.0))


class Provenance(StrictModel):
    """Where a sound came from; recorded for every sound (spike: bookkeeping, not friction)."""

    source: Literal["generated", "procedural", "library"]
    origin: NonEmptyStr
    """Model and plan, library name, or procedural generator."""
    licence: NonEmptyStr
    commercial: bool


class SoundSpec(StrictModel):
    """Everything that determines one sound's audio, and nothing about where it is mixed."""

    kind: Slug
    duration_seconds: float = Field(gt=0.0, le=600.0)
    params: dict[str, JsonValue] = Field(default_factory=dict)
    seed: int = 0
    channels: Literal[1, 2] = 2
    provenance: Provenance

    @field_validator("params")
    @classmethod
    def _finite(cls, v: dict[str, JsonValue]) -> dict[str, JsonValue]:
        ensure_finite(v, "params")
        return v

    def samples(self, sample_rate: int) -> int:
        """Exact length in samples at ``sample_rate``."""
        return max(1, round(Fraction(self.duration_seconds) * sample_rate))


def sound_key(spec: SoundSpec) -> str:
    """Cache key of a sound: the content hash of its :class:`SoundSpec` alone."""
    return content_hash(spec)


class Layer(StrictModel):
    """One sound in a cue (e.g. the sub, body, transient or tail of an impact)."""

    id: Slug
    sound: SoundSpec
    gain_db: GainDb = 0.0
    offset_samples: int = Field(default=0, ge=0)
    """Delay of this layer after the cue start."""


class AutomationPoint(StrictModel):
    frame: Frame
    """Edit frame (absolute on the timeline)."""
    gain: Gain


class AutomationCurve(StrictModel):
    """Fader automation of a cue, as points on the edit timeline."""

    points: list[AutomationPoint] = Field(min_length=1)

    @model_validator(mode="after")
    def _sorted(self) -> AutomationCurve:
        frames = [p.frame for p in self.points]
        if any(b <= a for a, b in zip(frames, frames[1:], strict=False)):
            raise ValueError("automation points must have strictly increasing frames")
        return self


class Cue(StrictModel):
    """A sound event on the timeline, anchored on an edit marker or an absolute frame."""

    id: Slug
    bus: BusName
    marker: NonEmptyStr | None = None
    """Name of the edit marker this cue lands on."""
    frame: Frame | None = None
    """Absolute edit frame, when the cue is not anchored on a marker."""
    offset_frames: int = 0
    gain_db: GainDb = 0.0
    fader: Gain = 1.0
    """Static fader gain of the cue's tracks."""
    automation: AutomationCurve | None = None
    layers: list[Layer] = Field(min_length=1)

    @model_validator(mode="after")
    def _valid(self) -> Cue:
        if (self.marker is None) == (self.frame is None):
            raise ValueError(f"cue {self.id!r}: set exactly one of marker or frame")
        ensure_unique_ids(self.layers, f"layer (cue {self.id!r})")
        # Ardour renders the same session differently when an automation curve starts
        # away from the fader's static value (docs/engineering/audio-tool-check.md).
        if self.automation is not None and self.automation.points[0].gain != self.fader:
            raise ValueError(
                f"cue {self.id!r}: automation must start at the static fader value {self.fader}"
            )
        return self


class Bus(StrictModel):
    id: BusName
    gain_db: GainDb = 0.0


def _default_buses() -> list[Bus]:
    return [Bus(id=b) for b in BUS_NAMES]


class AudioRecipe(StrictModel):
    """The declarative mix of one edit: cues on the DX, MX and FX buses."""

    id: Slug
    name: str = ""
    sample_rate: int = Field(default=48000, ge=8000, le=192000)
    rate: FrameRate = FPS_24
    duration_frames: int = Field(gt=0)
    buses: list[Bus] = Field(default_factory=_default_buses)
    cues: list[Cue] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistent(self) -> AudioRecipe:
        if sorted(b.id for b in self.buses) != sorted(BUS_NAMES):
            raise ValueError("buses must be exactly dx, mx and fx")
        ensure_unique_ids(self.cues, "cue")
        for cue in self.cues:
            if cue.automation is not None:
                last = cue.automation.points[-1].frame
                if last > self.duration_frames:
                    raise ValueError(f"cue {cue.id!r}: automation runs past the recipe end")
        return self

    @property
    def content_hash(self) -> str:
        return content_hash(self)

    def bus(self, name: BusName) -> Bus:
        return next(b for b in self.buses if b.id == name)

    def frame_sample(self, frame: int) -> int:
        """Nearest sample to the start of ``frame`` (exact rational arithmetic)."""
        return round(self.rate.seconds(frame) * self.sample_rate)

    @property
    def duration_samples(self) -> int:
        return self.frame_sample(self.duration_frames)

    def cue(self, cue_id: str) -> Cue:
        for cue in self.cues:
            if cue.id == cue_id:
                return cue
        raise KeyError(cue_id)


def event_frame(cue: Cue, edit: Edit) -> int:
    """Edit frame of the event a cue is anchored on (before its ``offset_frames``)."""
    if cue.frame is not None:
        return cue.frame
    found = [m for m in edit.markers if m.name == cue.marker]
    if not found:
        raise KeyError(f"cue {cue.id!r}: no edit marker named {cue.marker!r}")
    if len(found) > 1:
        raise ValueError(f"cue {cue.id!r}: marker name {cue.marker!r} is ambiguous")
    return found[0].frame


def place_cues(recipe: AudioRecipe, edit: Edit) -> dict[str, int]:
    """Start sample of every cue: its event frame plus ``offset_frames``, exactly."""
    if recipe.rate != edit.rate:
        raise ValueError(
            f"recipe rate {recipe.rate.fps} fps does not match edit rate {edit.rate.fps} fps"
        )
    placed: dict[str, int] = {}
    for cue in recipe.cues:
        frame = event_frame(cue, edit) + cue.offset_frames
        if frame < 0:
            raise ValueError(f"cue {cue.id!r} starts before the timeline")
        start = recipe.frame_sample(frame)
        end = start + max(
            layer.offset_samples + layer.sound.samples(recipe.sample_rate) for layer in cue.layers
        )
        if end > recipe.duration_samples:
            raise ValueError(f"cue {cue.id!r} runs past the recipe end")
        placed[cue.id] = start
    return placed
