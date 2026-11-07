# SPDX-License-Identifier: AGPL-3.0-or-later
"""Music cut to picture: beat and bar analysis, edits at bar boundaries, stretch, stems.

The score is edited to the cut in three steps:

1. :func:`analyze` finds the tempo, beats, downbeats (bars) and sections of a track in
   pure Python. An :class:`Analyzer` (librosa, Essentia) can replace the default.
2. :func:`plan_edit` picks whole bars so the cut fits the target length and lands the
   track's final hit on the end card. Every edit point is a bar boundary, and one
   stretch ratio, within ``max_stretch`` (±8% unless the cue allows more), fits the
   chosen bars to the end card.
3. :func:`render_edit` joins the bars with short crossfades and stretches the result once
   with Rubber Band. :func:`separate_stems` runs Demucs when a track has no stems.

Rubber Band and Demucs are local command-line tools behind the :class:`Stretcher` and
:class:`Separator` protocols; nothing here reaches an external service.
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import subprocess
import tempfile
from array import array
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from spectracut.audio.recipe import Provenance, SoundSpec
from spectracut.audio.render import Channels, read_wav, write_wav

DEFAULT_MAX_STRETCH = 0.08
"""Largest time-stretch, as a fraction of the original length, unless the cue allows more."""
BEATS_PER_BAR = 4
MIN_BPM = 60.0
MAX_BPM = 200.0
HOP_PER_S = 200
"""Onset envelope resolution: one hop is 5 ms."""
HIT_TOLERANCE_S = 0.03
"""A hit this far before a downbeat still belongs to that bar (analysis jitter)."""
CROSSFADE_S = 0.01
TRIM_FADE_S = 0.25
SECTION_RATIO = 1.5
MIN_SECTION_BARS = 2
DEMUCS_MODEL = "htdemucs"


class MusicEditError(RuntimeError):
    """The track cannot be analysed, fitted to the cut, stretched or separated."""


@dataclass(frozen=True)
class BeatGrid:
    """Beats, bars and sections of one track, in seconds from its start."""

    duration_s: float
    tempo_bpm: float
    beats: tuple[float, ...]
    downbeats: tuple[float, ...]
    """Start of every bar; bar ``i`` runs to ``downbeats[i + 1]`` (the last, to the end)."""
    sections: tuple[int, ...]
    """Index of the first bar of every section."""
    hit_s: float
    """The track's final hit (last strong onset): the ending or stinger."""
    beats_per_bar: int = BEATS_PER_BAR


Analyzer = Callable[[Path], BeatGrid]
"""Plug in librosa or Essentia here: reads a track, returns its :class:`BeatGrid`."""


# --------------------------------------------------------------------------- analysis


def _mono(channels: Sequence[Sequence[float]]) -> list[float]:
    n = len(channels)
    if n == 1:
        return list(channels[0])
    return [sum(frame) / n for frame in zip(*channels, strict=True)]


def _envelope(mono: Sequence[float], rate: int) -> tuple[list[float], list[float], float]:
    """Per-hop RMS, its positive rise (the onset strength) and the hop length in seconds."""
    hop = max(1, rate // HOP_PER_S)
    rms = [
        math.sqrt(sum(x * x for x in mono[i : i + hop]) / hop)
        for i in range(0, len(mono) - hop + 1, hop)
    ]
    onset = [0.0] + [max(0.0, b - a) for a, b in zip(rms, rms[1:], strict=False)]
    return rms, onset, hop / rate


def _at(onset: Sequence[float], pos: float) -> float:
    j = round(pos)
    return max(onset[max(0, j - 1) : j + 2], default=0.0)


def _comb(onset: Sequence[float], period: float) -> tuple[float, float]:
    """Best (mean onset strength, phase) of a beat comb with ``period`` hops."""
    best = (-1.0, 0.0)
    for phase in range(math.ceil(period)):
        points = [phase + m * period for m in range(int((len(onset) - 1 - phase) / period) + 1)]
        score = sum(_at(onset, p) for p in points) / max(1, len(points))
        if score > best[0]:
            best = (score, float(phase))
    return best


def _tempo(onset: Sequence[float], hop_s: float, bpm: float | None) -> tuple[float, float]:
    """Tempo and beat phase (in hops) by comb search, coarse then fine."""
    lo, hi = (bpm * 0.92, bpm * 1.08) if bpm else (MIN_BPM, MAX_BPM)

    def score(candidate: float, weighted: bool) -> tuple[float, float]:
        s, phase = _comb(onset, 60.0 / candidate / hop_s)
        if weighted:  # prefer the musical octave around 120 BPM
            s *= math.exp(-0.5 * math.log2(candidate / 120.0) ** 2)
        return s, phase

    coarse = [lo + i for i in range(int(hi - lo) + 1)] or [lo]
    centre = max(coarse, key=lambda c: score(c, bpm is None)[0])
    fine = [centre + i / 10 for i in range(-10, 11)]
    best = max(fine, key=lambda c: score(c, False)[0])
    return best, score(best, False)[1]


def _bar_sections(
    rms: Sequence[float], downbeats: Sequence[float], hop_s: float
) -> tuple[int, ...]:
    ends = [*downbeats[1:], len(rms) * hop_s]
    levels = []
    for a, b in zip(downbeats, ends, strict=True):
        chunk = rms[int(a / hop_s) : max(int(a / hop_s) + 1, int(b / hop_s))]
        levels.append(sum(chunk) / max(1, len(chunk)))
    sections = [0]
    for i in range(1, len(levels)):
        prev, cur = levels[i - 1], levels[i]
        if i - sections[-1] >= MIN_SECTION_BARS and prev > 0 and cur > 0:
            ratio = cur / prev
            if ratio > SECTION_RATIO or ratio < 1 / SECTION_RATIO:
                sections.append(i)
    return tuple(sections)


def analyze(
    path: Path,
    *,
    analyzer: Analyzer | None = None,
    bpm: float | None = None,
    beats_per_bar: int = BEATS_PER_BAR,
) -> BeatGrid:
    """Tempo, beats, downbeats, sections and final hit of a track.

    ``bpm`` narrows the tempo search (±8%) when the cue or the library knows it.
    """
    if analyzer is not None:
        return analyzer(path)
    rate, channels = read_wav(path)
    if not channels or not channels[0]:
        raise MusicEditError(f"{path}: no audio")
    duration = len(channels[0]) / rate
    rms, onset, hop_s = _envelope(_mono(channels), rate)
    peak = max(onset, default=0.0)
    if peak <= 0.0:
        raise MusicEditError(f"{path}: no onsets to find a beat in")
    tempo, phase = _tempo(onset, hop_s, bpm)
    period_s = 60.0 / tempo
    beats = tuple(phase * hop_s + i * period_s for i in range(int(duration / period_s) + 1))
    beats = tuple(b for b in beats if b < duration)

    def accent(offset: int) -> float:
        picked = beats[offset::beats_per_bar]
        return sum(_at(onset, b / hop_s) for b in picked) / max(1, len(picked))

    first = max(range(min(beats_per_bar, len(beats))), key=accent)
    downbeats = beats[first::beats_per_bar]
    strong = [i for i, v in enumerate(onset) if v >= 0.5 * peak]
    return BeatGrid(
        duration_s=duration,
        tempo_bpm=tempo,
        beats=beats,
        downbeats=downbeats,
        sections=_bar_sections(rms, downbeats, hop_s),
        hit_s=strong[-1] * hop_s,
        beats_per_bar=beats_per_bar,
    )


# --------------------------------------------------------------------------- planning


@dataclass(frozen=True)
class EditPlan:
    """Which bars of the source make the cut, and how far they are stretched."""

    segments: tuple[tuple[float, float], ...]
    """Source ``(start, end)`` seconds, in cut order; every start is a bar boundary."""
    ratio: float
    """Output length over source length of the chosen bars (1.05 is 5% slower)."""
    hit_src_s: float
    hit_out_s: float
    """Where the final hit lands in the edit; the end card, to within a frame."""
    out_duration_s: float
    fps: float

    @property
    def stretch(self) -> float:
        return abs(self.ratio - 1.0)

    @property
    def edit_points(self) -> tuple[tuple[float, float], ...]:
        """Source ``(end, start)`` of every join between non-adjacent bars."""
        return tuple((a[1], b[0]) for a, b in zip(self.segments, self.segments[1:], strict=False))

    def content_hash(self) -> str:
        body = json.dumps(
            {
                "segments": [[round(a, 6), round(b, 6)] for a, b in self.segments],
                "ratio": round(self.ratio, 9),
                "hit": round(self.hit_src_s, 6),
                "out": round(self.out_duration_s, 6),
            },
            sort_keys=True,
        )
        return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _merge(spans: Sequence[tuple[float, float]]) -> tuple[tuple[float, float], ...]:
    merged: list[tuple[float, float]] = []
    for start, end in spans:
        if end <= start:
            continue
        if merged and abs(merged[-1][1] - start) < 1e-9:
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return tuple(merged)


def plan_edit(
    grid: BeatGrid,
    *,
    target_s: float,
    end_card_s: float | None = None,
    fps: float = 24.0,
    max_stretch: float = DEFAULT_MAX_STRETCH,
) -> EditPlan:
    """Fit the track to a ``target_s`` edit whose end card starts at ``end_card_s``.

    Whole bars are kept from the start and the end of the track (the cut falls on a
    section boundary when one is near), so every edit point is a bar boundary, and the
    final hit lands on ``end_card_s``. ``end_card_s`` defaults to leaving the track's
    own ring-out after the hit. The stretch stays within ``max_stretch``; a cue that
    allows more passes a larger value.
    """
    bars = grid.downbeats
    if not bars or target_s <= 0 or fps <= 0 or max_stretch < 0:
        raise MusicEditError("plan_edit needs bars, a positive target, fps and max_stretch")
    ending = max((i for i, b in enumerate(bars) if b <= grid.hit_s + HIT_TOLERANCE_S), default=-1)
    if ending < 0:
        raise MusicEditError(f"the final hit ({grid.hit_s:.2f} s) is before the first bar")
    tail = grid.duration_s - grid.hit_s
    card = end_card_s if end_card_s is not None else max(target_s - tail, 0.0)
    if not 0 < card <= target_s:
        raise MusicEditError(f"end card {card:.2f} s is outside the {target_s:.2f} s edit")
    hit_offset = grid.hit_s - bars[ending]
    lengths = [bars[i + 1] - bars[i] for i in range(ending)]
    prefix = [0.0]
    for length in lengths:
        prefix.append(prefix[-1] + length)
    marks = set(grid.sections)
    best: tuple[tuple[float, int, float], int, int, float] | None = None
    nearest = math.inf
    for keep in range(ending + 1):
        for head in range(keep + 1):
            tail_start = ending - (keep - head)
            span = prefix[head] + prefix[ending] - prefix[tail_start] + hit_offset
            if span <= 0:
                continue
            ratio = card / span
            nearest = min(nearest, abs(ratio - 1.0))
            if abs(ratio - 1.0) > max_stretch:
                continue
            joins = (0 < head < ending and head in marks) + (tail_start in marks)
            key = (round(abs(ratio - 1.0) / 0.01), -joins, abs(ratio - 1.0))
            if best is None or key < best[0]:
                best = (key, head, tail_start, ratio)
    if best is None:
        raise MusicEditError(
            f"no bar selection fits the end card at {card:.2f} s within ±{max_stretch:.0%}"
            f" (closest needs ±{nearest:.0%}); the cue must allow more stretch"
        )
    _, head, tail_start, ratio = best
    segments = _merge(
        [(bars[0], bars[head]), (bars[tail_start], bars[ending]), (bars[ending], grid.duration_s)]
    )
    kept = sum(b - a for a, b in segments)
    return EditPlan(
        segments=segments,
        ratio=ratio,
        hit_src_s=grid.hit_s,
        hit_out_s=card,
        out_duration_s=min(kept * ratio, target_s),
        fps=fps,
    )


# --------------------------------------------------------------------------- rendering


def cut_beats(plan: EditPlan, grid: BeatGrid) -> tuple[float, ...]:
    """Every source beat inside the plan's bars, placed on the cut score (seconds).

    A beat at ``b`` in segment ``(start, end)`` lands at the segment's output offset plus
    ``b - start``, times ``plan.ratio``. Beats past ``out_duration_s`` are dropped; the
    result is sorted with duplicates (shared bar boundaries) removed.
    """
    placed: set[float] = set()
    offset = 0.0
    for start, end in plan.segments:
        for beat in grid.beats:
            if start <= beat < end:
                at = (offset + beat - start) * plan.ratio
                if at <= plan.out_duration_s + 1e-9:
                    placed.add(round(min(at, plan.out_duration_s), 6))
        offset += end - start
    return tuple(sorted(placed))


class Stretcher(Protocol):
    def stretch(self, channels: Channels, rate: int, ratio: float) -> Channels:
        """Return ``channels`` with their length multiplied by ``ratio``, pitch kept."""
        ...


@dataclass(frozen=True)
class RubberBandStretcher:
    """Time-stretch with the ``rubberband`` command-line tool (Rubber Band, GPL)."""

    binary: str | None = None
    timeout: float = 600.0

    def stretch(self, channels: Channels, rate: int, ratio: float) -> Channels:
        exe = self.binary or shutil.which("rubberband")
        if exe is None:
            raise MusicEditError("rubberband is not on PATH; install Rubber Band to stretch")
        with tempfile.TemporaryDirectory(prefix="music_edit_") as tmp:
            src, dst = Path(tmp, "in.wav"), Path(tmp, "out.wav")
            write_wav(src, channels, rate, float_format=True)
            argv = [exe, "--quiet", "--time", f"{ratio:.8f}", str(src), str(dst)]
            try:
                proc = subprocess.run(
                    argv, capture_output=True, text=True, check=False, timeout=self.timeout
                )
            except subprocess.TimeoutExpired as exc:
                raise MusicEditError(f"rubberband timed out after {self.timeout} s") from exc
            if proc.returncode != 0 or not dst.exists():
                raise MusicEditError(
                    f"rubberband failed (exit {proc.returncode}): {proc.stderr[-500:]}"
                )
            _, out = read_wav(dst)
        return out


def _join(channels: Channels, rate: int, plan: EditPlan) -> Channels:
    """Concatenate the plan's bars, crossfading into each join without moving a bar."""
    fade = max(1, round(CROSSFADE_S * rate))
    pieces = [
        (round(start * rate), min(round(end * rate), len(channels[0])))
        for start, end in plan.segments
    ]
    leads = [fade if i > 0 and a >= fade else 0 for i, (a, _) in enumerate(pieces)]
    total = sum(b - a for a, b in pieces)
    out: Channels = [array("f", bytes(4 * total)) for _ in channels]
    pos = 0
    for index, (a, b) in enumerate(pieces):
        lead = leads[index]
        next_lead = leads[index + 1] if index + 1 < len(pieces) else 0
        for k in range(a - lead, b):
            gain = 1.0
            if k < a:  # the pre-roll fades in under the previous bar's fade-out
                gain = (k - (a - lead)) / lead
            elif next_lead and k >= b - next_lead:
                gain = 1.0 - (k - (b - next_lead)) / next_lead
            at = pos + (k - a)
            for c, chan in enumerate(channels):
                out[c][at] += chan[k] * gain
        pos += b - a
    return out


def render_edit(
    plan: EditPlan, src: Path, out: Path, *, stretcher: Stretcher | None = None
) -> Path:
    """Write the edit of ``src`` to ``out`` (32-bit float WAV) and return ``out``."""
    rate, channels = read_wav(src)
    joined = _join(channels, rate, plan)
    stretched = (stretcher or RubberBandStretcher()).stretch(joined, rate, plan.ratio)
    want = round(plan.out_duration_s * rate)
    trimmed = len(stretched[0]) > want
    fitted: Channels = []
    for chan in stretched:
        row = array("f", chan[:want])
        row.extend(array("f", bytes(4 * max(0, want - len(row)))))
        if trimmed:
            fade = min(want, round(TRIM_FADE_S * rate))
            for k in range(fade):
                row[want - fade + k] *= 1.0 - (k + 1) / fade
        fitted.append(row)
    write_wav(out, fitted, rate, float_format=True)
    return out


def edited_sound(plan: EditPlan, source: Provenance) -> SoundSpec:
    """A :class:`SoundSpec` for the edit: the plan's hash and the source's provenance."""
    return SoundSpec(
        kind="music-edit",
        duration_seconds=plan.out_duration_s,
        params={"plan": plan.content_hash(), "ratio": round(plan.ratio, 6)},
        provenance=Provenance(
            source=source.source,
            origin=f"music_edit({source.origin})",
            licence=source.licence,
            commercial=source.commercial,
        ),
    )


# --------------------------------------------------------------------------- stems


class Separator(Protocol):
    def separate(self, track: Path, outdir: Path) -> dict[str, Path]:
        """Split ``track`` into stems under ``outdir``; return stem name to file."""
        ...


@dataclass(frozen=True)
class DemucsSeparator:
    """Stem separation with the ``demucs`` command-line tool (MIT, runs locally)."""

    binary: str | None = None
    model: str = DEMUCS_MODEL
    timeout: float = 3600.0

    def separate(self, track: Path, outdir: Path) -> dict[str, Path]:
        exe = self.binary or shutil.which("demucs")
        if exe is None:
            raise MusicEditError("demucs is not on PATH; install Demucs to separate stems")
        argv = [exe, "-n", self.model, "-o", str(outdir), str(track)]
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, check=False, timeout=self.timeout
            )
        except subprocess.TimeoutExpired as exc:
            raise MusicEditError(f"demucs timed out after {self.timeout} s") from exc
        if proc.returncode != 0:
            raise MusicEditError(f"demucs failed (exit {proc.returncode}): {proc.stderr[-500:]}")
        found = {p.stem: p for p in sorted((outdir / self.model / track.stem).glob("*.wav"))}
        if not found:
            raise MusicEditError(f"demucs wrote no stems under {outdir / self.model}")
        return found


def separate_stems(
    track: Path,
    outdir: Path,
    *,
    stems: Mapping[str, Path] | None = None,
    separator: Separator | None = None,
) -> dict[str, Path]:
    """The track's own ``stems`` when it has them, else Demucs's separation of it."""
    if stems:
        missing = [name for name, p in stems.items() if not p.exists()]
        if missing:
            raise MusicEditError(f"stem files missing: {', '.join(sorted(missing))}")
        return dict(stems)
    return (separator or DemucsSeparator()).separate(track, outdir)


def render_stems(
    plan: EditPlan,
    stems: Mapping[str, Path],
    outdir: Path,
    *,
    stretcher: Stretcher | None = None,
) -> dict[str, Path]:
    """Cut every stem with the same plan, so the stems stay in sync with the mix."""
    return {
        name: render_edit(plan, path, outdir / f"{name}.wav", stretcher=stretcher)
        for name, path in stems.items()
    }
