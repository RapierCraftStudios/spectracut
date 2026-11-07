# SPDX-License-Identifier: AGPL-3.0-or-later
"""The editor's craft pass: beat-synced cuts, J/L cuts, match cuts, ramps and titles.

:func:`editorial_pass` re-cuts a conformed animatic to the music. The total length and
every VO line's record frame stay fixed (the mix and the music are built from the
animatic's timing); only picture cuts move:

- each interior picture cut snaps to the nearest beat frame (``round(beat * fps)``)
  within one bar, keeping every clip within ``[min_frames, board_max]``. A clip's
  ``board_max`` is its animatic length (board plus the VO hold the animatic added) plus
  one beat period, the snap tolerance. A cut that cannot land on a beat, or would split
  a VO line beyond the J/L window, stays and is reported as ``vo-override``;
- a VO line whose picture cut moved becomes a J cut (sound leads) or L cut (sound
  trails) of at most :data:`JL_WINDOW` frames, linked to its picture clip;
- each device <-> abstract-interlude boundary gets a ``match-cut`` edit marker and a
  0.5 -> 1.0 speed ramp on the head of the incoming device clip;
- each clip is labelled ``device:<laptop|phone>`` or ``interlude`` from its board, and
  a board's ``text`` gets a ``title`` marker held for its reading time.

:func:`pace_cutdown` re-paces a 15 s or 30 s cut-down on the beat (rather than trimming
the master), and :func:`check_editorial` lists what breaks the craft rules. Everything
here is frame arithmetic: no media is read and nothing outside the timeline is called.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from fractions import Fraction
from typing import Literal

from pydantic import Field

from spectracut.edit.timeline import Clip, Edit, Marker, SpeedRamp, Track
from spectracut.spec._base import StrictModel
from spectracut.storyboard.models import Board, Storyboard

JL_WINDOW = 12
"""Most frames a VO line may lead (J) or trail (L) its picture cut."""
MIN_FRAMES = 12
"""Shortest clip the pass will cut (half a second at 24 fps)."""
BEATS_PER_BAR = 4
RAMP_FRAMES = 12
"""Length of the 0.5 -> 1.0 speed ramp on a device clip's head."""
TITLE_SETTLE_FRAMES = 6
"""Frames a title waits after its clip starts, so the eye lands on the picture first."""
WORDS_PER_SECOND = 3.0
TITLE_MIN_S = 1.5
TITLE_MAX_S = 5.0

CutReason = Literal["beat", "vo-override"]


class CutPoint(StrictModel):
    """One interior picture cut: where it was, where it is and why."""

    clip_id: str
    """The picture clip that starts at this cut."""
    original: int
    frame: int
    reason: CutReason
    beat_frame: int | None = None
    """The beat the cut landed on (``None`` for a VO override)."""


class VoCut(StrictModel):
    """A VO line's relation to its picture cut: J (leads), L (trails) or in sync."""

    clip_id: str
    link: str
    offset: int
    """Audio start minus picture start (``Edit.audio_offset``): negative is a J cut."""
    kind: Literal["J", "L", "sync"]


class EditorialReport(StrictModel):
    """What the editorial pass did, for the critic and :func:`check_editorial`."""

    beat_frames: list[int] = Field(default_factory=list)
    cuts: list[CutPoint] = Field(default_factory=list)
    vo_cuts: list[VoCut] = Field(default_factory=list)
    board_max: dict[str, int] = Field(default_factory=dict)
    """Longest each picture clip may run, by clip id."""
    match_cuts: list[int] = Field(default_factory=list)
    """Frames of the device <-> interlude cuts."""
    titles: dict[str, int] = Field(default_factory=dict)
    """Frames each titled clip holds its text, by clip id."""


# ------------------------------------------------------------------- helpers


def _picture_track(edit: Edit) -> Track | None:
    """The track named ``picture``, else the first video track with a shot clip."""
    video = [t for t in edit.tracks if t.kind == "video"]
    named = next((t for t in video if t.id == "picture"), None)
    if named is not None:
        return named
    return next((t for t in video if any(c.shot_id for c in t.clips())), None)


def beat_frames(beats_s: Sequence[float], edit: Edit) -> list[int]:
    """Each beat as an edit frame (``round(beat * fps)``), sorted and unique."""
    fps = edit.rate.fps
    return sorted({round(Fraction(b) * fps) for b in beats_s if b >= 0})


def beat_period(beats_s: Sequence[float]) -> float:
    """Median seconds between beats. Raises ``ValueError`` with fewer than two beats."""
    ordered = sorted(set(beats_s))
    gaps = [b - a for a, b in zip(ordered, ordered[1:], strict=False) if b > a]
    if not gaps:
        raise ValueError("need at least two beats to know the tempo")
    return float(statistics.median(gaps))


def _kind(board: Board | None) -> str:
    if board is None:
        return "unknown"
    return f"device:{board.device}" if board.device else "interlude"


def _is_device(board: Board | None) -> bool | None:
    return None if board is None else board.device is not None


def title_frames(text: str, fps: Fraction) -> int:
    """Reading time of ``text``: words / 3 per second + 1 s, clamped to 1.5–5 s."""
    seconds = len(text.split()) / WORDS_PER_SECOND + 1.0
    seconds = min(TITLE_MAX_S, max(TITLE_MIN_S, seconds))
    return max(1, round(Fraction(seconds) * fps))


def ramp_source(length: int, ramp: int) -> int:
    """Source frames a clip of ``length`` record frames reads with a 0.5 -> 1.0 head ramp."""
    return math.ceil(Fraction(3, 4) * ramp) + (length - ramp)


def _ours(marker: Marker) -> bool:
    """A clip marker this pass writes (so a re-run replaces it)."""
    return marker.name in ("title", "interlude") or marker.name.startswith("device:")


def _vo_spans(vo_track: Track | None) -> list[tuple[Clip, int, int]]:
    if vo_track is None:
        return []
    return [
        (c, vo_track.record_start(c.id), vo_track.record_start(c.id) + c.duration)
        for c in vo_track.clips()
    ]


# ------------------------------------------------------------------ the pass


def editorial_pass(
    animatic: Edit,
    beats_s: Sequence[float],
    *,
    storyboard: Storyboard | None = None,
    vo_track: Track | None = None,
    min_frames: int = MIN_FRAMES,
) -> tuple[Edit, EditorialReport]:
    """Re-cut ``animatic`` on the beat; see the module docstring for the rules.

    ``vo_track`` is the VO track to keep in place (default: the edit's ``vo`` track).
    Raises ``ValueError`` when a titled clip is too short to hold its title.
    """
    picture = _picture_track(animatic)
    if picture is None or not picture.clips():
        return animatic, EditorialReport()
    if vo_track is None:
        vo_track = next((t for t in animatic.tracks if t.id == "vo"), None)
    fps = animatic.rate.fps
    beats = beat_frames(beats_s, animatic)
    clips = picture.clips()
    starts = [picture.record_start(c.id) for c in clips]
    end = starts[-1] + clips[-1].duration
    period = round(Fraction(beat_period(beats_s)) * fps) if len(set(beats_s)) > 1 else 0
    bar = max(1, period * BEATS_PER_BAR)
    longest = [c.duration + period for c in clips]
    shortest = [min(min_frames, c.duration) for c in clips]
    spans = _vo_spans(vo_track)

    def vo_ok(original: int, frame: int) -> bool:
        for _, a, b in spans:
            if a == original and abs(frame - a) > JL_WINDOW:
                return False  # the line's J/L offset would pass the window
            if a < frame < b and min(frame - a, b - frame) > JL_WINDOW:
                return False  # the cut would land inside the line
        return True

    cuts: list[CutPoint] = []
    placed = [0]
    for k in range(1, len(clips)):
        x = starts[k]
        nxt = starts[k + 1] if k + 1 < len(clips) else end
        prev = placed[-1]
        near = sorted((b for b in beats if abs(b - x) <= bar), key=lambda b: (abs(b - x), b))
        chosen: int | None = None
        for b in near:
            left, right = b - prev, nxt - b
            if not shortest[k - 1] <= left <= longest[k - 1]:
                continue
            if not shortest[k] <= right <= longest[k]:
                continue
            if vo_ok(x, b):
                chosen = b
                break
        if chosen is None:
            placed.append(x)
            cuts.append(CutPoint(clip_id=clips[k].id, original=x, frame=x, reason="vo-override"))
        else:
            placed.append(chosen)
            cuts.append(
                CutPoint(
                    clip_id=clips[k].id, original=x, frame=chosen, reason="beat", beat_frame=chosen
                )
            )
    placed.append(end)

    by_shot = {b.shot_id: b for b in storyboard.boards} if storyboard is not None else {}
    report = EditorialReport(
        beat_frames=beats,
        cuts=cuts,
        board_max={c.id: longest[n] for n, c in enumerate(clips)},
    )
    new_clips: dict[str, Clip] = {}
    for n, clip in enumerate(clips):
        length = placed[n + 1] - placed[n]
        board = by_shot.get(clip.shot_id or "")
        markers = [m for m in clip.markers if not _ours(m)]
        ramps = list(clip.ramps)
        if board is not None:
            markers.append(Marker(name=_kind(board), frame=0, color="BLUE"))
            previous = by_shot.get(clips[n - 1].shot_id or "") if n > 0 else None
            if n > 0 and previous is not None and _is_device(previous) != _is_device(board):
                report.match_cuts.append(placed[n])
                ramp = min(RAMP_FRAMES, length // 2)
                if board.device is not None and ramp >= 2:
                    # 0.5 -> 1.0 reads 0.75 source frames per record frame, so the
                    # ramped clip never needs more source than it has.
                    ramps = [
                        SpeedRamp(start=0, end=ramp, speed_from=0.5, speed_to=1.0),
                        *[r for r in ramps if r.start >= ramp],
                    ]
                    if ramp_source(length, ramp) > length:
                        raise AssertionError("speed ramp consumes more source than the clip")
            if board.text.strip():
                hold = title_frames(board.text, fps)
                settle = min(TITLE_SETTLE_FRAMES, max(0, length - hold))
                if settle + hold > length:
                    raise ValueError(
                        f"clip {clip.id!r} is {length} frames; its title needs "
                        f"{settle + hold} ({board.text!r})"
                    )
                markers.append(
                    Marker(
                        name="title", frame=settle, duration=hold, color="YELLOW", note=board.text
                    )
                )
                report.titles[clip.id] = hold
        assert clip.source_out is not None or clip.nested is not None
        source_out = clip.source_in + length if clip.source_out is not None else None
        new_clips[clip.id] = Clip.model_validate(
            {
                **clip.model_dump(mode="json"),
                "source_out": source_out,
                "ramps": [r.model_dump(mode="json") for r in ramps],
                "markers": [m.model_dump(mode="json") for m in markers],
            }
        )

    # VO: same record frames; each line linked to the picture clip it was cut with.
    containing: dict[int, str] = {}
    for _, a, _b in spans:
        for n, clip in enumerate(clips):
            if starts[n] <= a < starts[n] + clip.duration:
                containing[a] = clip.id
    vo_clips: dict[str, Clip] = {}
    for clip, a, _b in spans:
        link = clip.link if clip.link in new_clips else containing.get(a)
        if link is not None:
            vo_clips[clip.id] = clip.model_copy(update={"link": link})

    tracks: list[Track] = []
    for track in animatic.tracks:
        if track is picture:
            items = [new_clips.get(i.id, i) if isinstance(i, Clip) else i for i in track.items]
            track = Track(id=track.id, kind=track.kind, items=list(items))
        elif vo_track is not None and track.id == vo_track.id:
            items = [vo_clips.get(i.id, i) if isinstance(i, Clip) else i for i in track.items]
            track = Track(id=track.id, kind=track.kind, items=list(items))
        tracks.append(track)
    markers = [m for m in animatic.markers if m.name != "match-cut"]
    markers += [Marker(name="match-cut", frame=f, color="GREEN") for f in report.match_cuts]
    edit = Edit.model_validate(
        {
            **animatic.model_dump(mode="json"),
            "tracks": [t.model_dump(mode="json") for t in tracks],
            "markers": [m.model_dump(mode="json") for m in markers],
        }
    )
    for clip_id, clip in vo_clips.items():
        assert clip.link is not None
        offset = edit.audio_offset(clip_id)
        kind: Literal["J", "L", "sync"] = "J" if offset < 0 else "L" if offset > 0 else "sync"
        report.vo_cuts.append(VoCut(clip_id=clip_id, link=clip.link, offset=offset, kind=kind))
    return edit, report


# ------------------------------------------------------------------ cut-downs


def pace_cutdown(
    master: Edit, beats_s: Sequence[float], length_s: float, end_card: str
) -> list[tuple[str, int]]:
    """Shots and frames of a ``length_s`` cut-down re-paced on the beat.

    The hook (the master's first shot) leads, then the master's shots in order, each a
    whole number of beat periods (``round(n * period)`` frames) and never longer than
    in the master. The per-shot target is shorter at 15 s than at 30 s; clips the
    editorial pass labelled ``interlude`` are 1-beat (15 s) or 2-beat (30 s) punctuation.
    The end card closes and takes the remainder, so the frames sum to exactly
    ``length_s`` seconds. Raises ``ValueError`` when the master cannot fill the length on the beat.
    """
    picture = _picture_track(master)
    clips = [c for c in (picture.clips() if picture else []) if c.shot_id is not None]
    if not clips:
        raise ValueError(f"master {master.id!r} has no picture clips")
    fps = master.rate.fps
    period = Fraction(beat_period(beats_s)) * fps
    if period < 1:
        raise ValueError("the beat period is shorter than a frame")
    target = master.rate.frames(Fraction(length_s))
    card = next((c for c in clips if c.shot_id == end_card), None)
    card_cap = card.duration if card is not None else 0
    # Seconds per shot: quick at 15 s, room to breathe at 30 s.
    per_shot_s = Fraction(3, 2) if length_s <= 15 else Fraction(5, 2)
    full = max(1, round(per_shot_s * fps / period))

    def frames(n: int) -> int:
        return round(n * period)

    body = [c for c in clips if c.shot_id != end_card]
    seen: set[str] = set()
    order: list[Clip] = []
    for clip in body:
        if clip.shot_id not in seen:
            seen.add(clip.shot_id or "")
            order.append(clip)
    caps = [int(Fraction(c.duration) / period) for c in order]
    punctuation = 1 if length_s <= 15 else 2
    interlude = [any(m.name == "interlude" for m in c.markers) for c in order]
    goals = [min(full, punctuation) if i else full for i in interlude]
    low = target - card_cap if card is not None else target
    counts = [0] * len(order)

    def total() -> int:
        return sum(frames(n) for n in counts)

    for i in range(len(order)):
        while total() < low and counts[i] < min(goals[i], caps[i]):
            counts[i] += 1
        if total() >= low:
            break
    while total() < low:
        # Device shots stretch first; interludes stay punctuation unless nothing else can.
        grow = [i for i, n in enumerate(counts) if 0 < n < caps[i] and not interlude[i]]
        grow = grow or [i for i, n in enumerate(counts) if n == 0 < caps[i]][:1]
        grow = grow or [i for i, n in enumerate(counts) if 0 < n < caps[i]]
        if not grow:
            raise ValueError(
                f"master {master.id!r} cannot fill a {length_s:g} s cut-down on the beat"
            )
        for i in grow:
            if total() >= low:
                break
            counts[i] += 1
    picked = [(c.shot_id or "", frames(n)) for c, n in zip(order, counts, strict=True) if n]
    rest = target - total()
    if card is None:
        if rest != 0:
            raise ValueError(f"no end card to close the {length_s:g} s cut-down on the beat")
        return picked
    if not 0 < rest <= card_cap:
        raise ValueError(
            f"the end card needs {rest} frames to close the {length_s:g} s cut-down; "
            f"the master has {card_cap}"
        )
    return [*picked, (end_card, rest)]


# --------------------------------------------------------------------- checks


def check_editorial(
    edit: Edit, report: EditorialReport, storyboard: Storyboard | None = None
) -> list[str]:
    """Problems with an edit cut by :func:`editorial_pass` (empty when it holds up)."""
    problems: list[str] = []
    picture = _picture_track(edit)
    clips = picture.clips() if picture is not None else []
    overrides = {c.frame for c in report.cuts if c.reason == "vo-override"}
    if report.beat_frames:
        for clip in clips[1:]:
            assert picture is not None
            at = picture.record_start(clip.id)
            off = min(abs(at - b) for b in report.beat_frames)
            if off > 1 and at not in overrides:
                problems.append(f"cut into {clip.id!r} at frame {at} is {off} frames off the beat")
    for clip in clips:
        cap = report.board_max.get(clip.id)
        if cap is not None and clip.duration > cap:
            problems.append(f"clip {clip.id!r} runs {clip.duration} frames; its board allows {cap}")
    if storyboard is not None:
        by_shot = {b.shot_id: b for b in storyboard.boards}
        cut = [_kind(by_shot.get(c.shot_id or "")) for c in clips if c.shot_id in by_shot]
        boards = [_kind(b) for b in storyboard.boards if b.shot_id in {c.shot_id for c in clips}]
        if cut != boards:
            problems.append(f"picture order {cut} differs from the storyboard's {boards}")
    return problems
