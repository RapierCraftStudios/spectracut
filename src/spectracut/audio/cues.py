# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sound cues from on-screen events (spike § Sound design and mix).

Each :class:`~spectracut.audio.events.SoundEvent` becomes a
:class:`~spectracut.audio.recipe.Cue` on the effects bus: a sound family, a size, layered
procedural sounds and a timing offset, with the event's parameters driving the sound:

- cuts get a short hit, transitions and speed ramps a whoosh as long as the overlap;
- a camera move gets a whoosh that is longer and brighter the faster the camera moves;
- clicks, keystrokes, scrolls, navigations and drags get UI sounds on their exact frame;
- titles get an impact sized by the title; effects get a riser that peaks exactly on the
  event frame, followed by an impact.

Every sound is a :data:`PROCEDURAL` :class:`~spectracut.audio.recipe.SoundSpec` rendered
by :class:`~spectracut.audio.procedural.ProceduralGenerator` (or SuperCollider). Cue ids
and seeds derive from the event alone, so the same events always give the same cues.
"""

from __future__ import annotations

import json
import math
import zlib
from collections.abc import Iterable
from fractions import Fraction
from typing import Literal

from pydantic import JsonValue

from spectracut.audio.events import SoundEvent
from spectracut.audio.recipe import AudioRecipe, Cue, Layer, Provenance, SoundSpec
from spectracut.edit.timeline import Edit, FrameRate

Size = Literal["small", "medium", "large"]
SIZES: tuple[Size, ...] = ("small", "medium", "large")

PROCEDURAL = Provenance(
    source="procedural",
    origin="spectracut.audio.procedural",
    licence="AGPL-3.0-or-later (generated)",
    commercial=True,
)

RISER_LEAD_S = 2.0
RISER_TAIL_S = 0.08
MAX_WHOOSH_S = 4.0


def whoosh_shape(speed: float) -> tuple[float, float]:
    """Duration (s) and low-pass cutoff (Hz) of a camera-move whoosh at ``speed`` units/s.

    Both grow monotonically with speed: a faster move is longer and brighter.
    """
    s = max(0.0, speed)
    duration = min(MAX_WHOOSH_S, 0.35 + 0.45 * math.log2(1.0 + s))
    cutoff = min(14000.0, 800.0 * (1.0 + s) ** 0.8)
    return duration, cutoff


def _number(event: SoundEvent, name: str, default: float) -> float:
    value = event.params.get(name, default)
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return default


def event_size(event: SoundEvent) -> Size:
    """``params["size"]`` when given, else from how big the event is."""
    given = event.params.get("size")
    if isinstance(given, str) and given in SIZES:
        return given
    if event.kind == "camera_move":
        speed = _number(event, "speed", 0.0)
        return "small" if speed < 1.0 else "medium" if speed < 4.0 else "large"
    if event.kind in ("title", "effect"):
        return "medium"
    if event.kind in ("cut", "transition", "speed_ramp"):
        return "medium"
    return "small"


def _seed(event: SoundEvent) -> int:
    blob = json.dumps(
        [event.kind, event.frame, event.duration_frames, event.params], sort_keys=True
    )
    return zlib.crc32(blob.encode())


def _sound(kind: str, seconds: float, seed: int, **params: JsonValue) -> SoundSpec:
    return SoundSpec(
        kind=kind,
        duration_seconds=round(min(max(seconds, 0.005), 600.0), 6),
        params=params,
        seed=seed,
        provenance=PROCEDURAL,
    )


_SCALE: dict[Size, float] = {"small": 0.7, "medium": 1.0, "large": 1.5}
_GAIN: dict[Size, float] = {"small": -6.0, "medium": -3.0, "large": 0.0}


def _layers(event: SoundEvent, rate: FrameRate, sample_rate: int) -> tuple[list[Layer], int]:
    """The cue's layers and its ``offset_frames``."""
    seed = _seed(event)
    size = event_size(event)
    k = _SCALE[size]
    span = float(rate.seconds(event.duration_frames))
    kind = event.kind
    if kind == "cut":
        return [
            Layer(id="body", sound=_sound("hit", 0.3 * k, seed, freq=60.0)),
            Layer(
                id="air",
                sound=_sound("whoosh", 0.2 * k, seed + 1, cutoff_hz=6000.0),
                gain_db=-12.0,
            ),
        ], 0
    if kind in ("transition", "speed_ramp"):
        speed = abs(_number(event, "speed_to", 1.0) - _number(event, "speed_from", 1.0))
        cutoff = 2500.0 if kind == "transition" else whoosh_shape(1.0 + speed)[1]
        seconds = min(MAX_WHOOSH_S, max(0.3, span))
        return [Layer(id="body", sound=_sound("whoosh", seconds, seed, cutoff_hz=cutoff))], 0
    if kind == "camera_move":
        seconds, cutoff = whoosh_shape(_number(event, "speed", 0.0))
        return [
            Layer(id="body", sound=_sound("whoosh", seconds, seed, cutoff_hz=round(cutoff, 3)))
        ], 0
    if kind == "click":
        return [Layer(id="body", sound=_sound("ui_click", 0.06, seed))], 0
    if kind == "keystroke":
        return [Layer(id="body", sound=_sound("ui_key", 0.05, seed), gain_db=-4.0)], 0
    if kind == "scroll":
        return [Layer(id="body", sound=_sound("ui_scroll", 0.15, seed), gain_db=-6.0)], 0
    if kind == "navigation":
        return [Layer(id="body", sound=_sound("ui_nav", 0.3, seed))], 0
    if kind == "drag":
        phase = event.params.get("phase")
        if phase in ("start", "end"):
            return [Layer(id="body", sound=_sound("ui_click", 0.05, seed, freq=1600.0))], 0
        return [Layer(id="body", sound=_sound("ui_scroll", 0.08, seed), gain_db=-12.0)], 0
    impact = _sound("impact", 0.8 * k, seed, freq=50.0 if size == "large" else 60.0)
    if kind == "title":
        return [
            Layer(id="body", sound=impact, gain_db=_GAIN[size]),
            Layer(id="transient", sound=_sound("ui_click", 0.04, seed + 1), gain_db=-6.0),
        ], 0
    # effect: a riser that peaks exactly on the event frame, then the impact.
    lead_s = _number(event, "lead_s", RISER_LEAD_S)
    lead = min(rate.frames(Fraction(lead_s)), event.frame)
    if lead <= 0:
        return [Layer(id="body", sound=impact, gain_db=_GAIN[size])], 0
    peak = rate.seconds(lead)
    peak_samples = round(peak * sample_rate)
    peak_s = peak_samples / sample_rate
    riser = _sound("riser", peak_s + RISER_TAIL_S, seed + 2, peak_s=peak_s)
    return [
        Layer(id="riser", sound=riser, gain_db=-3.0),
        Layer(id="body", sound=impact, gain_db=_GAIN[size], offset_samples=peak_samples),
    ], -lead


def event_cue(
    event: SoundEvent, rate: FrameRate, sample_rate: int = 48000, cue_id: str | None = None
) -> Cue:
    """The cue for one event, anchored on the event's frame."""
    layers, offset = _layers(event, rate, sample_rate)
    return Cue(
        id=cue_id or f"{event.kind}-{event.frame}",
        bus="fx",
        frame=event.frame,
        offset_frames=offset,
        layers=layers,
    )


def cues_for_events(
    events: Iterable[SoundEvent], rate: FrameRate, sample_rate: int = 48000
) -> list[Cue]:
    """One cue per event, with unique deterministic ids (``<kind>-<frame>[-n]``)."""
    used: dict[str, int] = {}
    cues: list[Cue] = []
    for event in events:
        base = f"{event.kind.replace('_', '-')}-{event.frame}"
        n = used.get(base, 0)
        used[base] = n + 1
        cues.append(event_cue(event, rate, sample_rate, base if n == 0 else f"{base}-{n}"))
    return cues


def _fit(cue: Cue, start: int, total: int, sample_rate: int) -> Cue | None:
    """``cue`` with layers trimmed to end by sample ``total``; None if nothing is left."""
    layers: list[Layer] = []
    for layer in cue.layers:
        at = start + layer.offset_samples
        avail = total - at
        if avail < 1:
            continue
        if layer.sound.samples(sample_rate) > avail:
            sound = {**layer.sound.model_dump(), "duration_seconds": (avail - 0.25) / sample_rate}
            layer = Layer.model_validate({**layer.model_dump(), "sound": sound})
        layers.append(layer)
    if not layers:
        return None
    return Cue.model_validate({**cue.model_dump(), "layers": [x.model_dump() for x in layers]})


def cue_recipe(
    edit: Edit, events: Iterable[SoundEvent], recipe_id: str, sample_rate: int = 48000
) -> AudioRecipe:
    """An :class:`AudioRecipe` with a cue for every event, trimmed to fit the edit."""
    duration = edit.duration
    if duration <= 0:
        raise ValueError(f"edit {edit.id!r} is empty")
    shell = AudioRecipe(
        id=recipe_id, sample_rate=sample_rate, rate=edit.rate, duration_frames=duration
    )
    total = shell.duration_samples
    cues: list[Cue] = []
    for cue in cues_for_events(events, edit.rate, sample_rate):
        assert cue.frame is not None
        start = shell.frame_sample(cue.frame + cue.offset_frames)
        fitted = _fit(cue, start, total, sample_rate)
        if fitted is not None:
            cues.append(fitted)
    return AudioRecipe(
        id=recipe_id, sample_rate=sample_rate, rate=edit.rate, duration_frames=duration, cues=cues
    )
