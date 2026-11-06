# SPDX-License-Identifier: AGPL-3.0-or-later
"""The animatic: boards cut to temp VO and music on an :class:`Edit`, at locked timing."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from math import ceil

from spectracut.edit.timeline import Clip, Edit, FrameRate, Gap, Track
from spectracut.spec._base import Slug
from spectracut.storyboard.models import Storyboard


@dataclass(frozen=True)
class TempAudio:
    """A temp VO line or music bed on disk and how long it is."""

    media_ref: str
    duration_s: float


_TIMECODE = r"(\d+(?::\d+)+(?:\.\d+)?)"
_RANGE = re.compile(rf"^\s*{_TIMECODE}(?:\s*[-\u2013\u2014]+\s*{_TIMECODE})?(?=\s|$)\s*")
_LABEL_END = re.compile(r"[.|(,]")


def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()


def _seconds(code: str) -> float:
    total = 0.0
    for part in code.split(":"):
        total = total * 60 + float(part)
    return total


def _range(beat: str) -> tuple[float, float | None] | None:
    """The leading ``start–end`` (or lone ``start``) timecode of ``beat``, in seconds."""
    match = _RANGE.match(beat)
    if match is None:
        return None
    end = match.group(2)
    return _seconds(match.group(1)), _seconds(end) if end else None


def _label(beat: str) -> str:
    """``beat`` after its leading timecode (range), cut at the first ``. | ( ,``."""
    match = _RANGE.match(beat)
    rest = beat[match.end() :] if match else beat
    return _norm(_LABEL_END.split(rest, maxsplit=1)[0])


def _resolve(beat: str, beats: Sequence[str]) -> str:
    if not beat.strip():
        return ""
    norm = _norm(beat)
    for candidate in beats:
        if _norm(candidate) == norm:
            return candidate
    label = _label(beat)
    best = ""
    best_len = 0
    for candidate in beats:
        prefix = _label(candidate)
        if prefix and label.startswith(prefix) and len(prefix) > best_len:
            best, best_len = candidate, len(prefix)
    if best:
        return best
    span = _range(beat)
    if span is not None:
        for candidate in beats:
            window = _range(candidate)
            if window is not None and window[1] is not None and window[0] <= span[0] < window[1]:
                return candidate
    return ""


def resolve_beats(storyboard: Storyboard, beats: Sequence[str]) -> Storyboard:
    """``storyboard`` with each board's ``beat`` rewritten to the script beat it covers.

    A board matches a script beat by normalised text (case and whitespace), else by
    label (the text after a leading ``0:00–0:03.5`` range, cut at the first ``.``,
    ``|``, ``(`` or ``,``) starting with the script beat's label (longest wins), else
    by its start timecode falling in the script beat's range. Unmatched boards get
    beat ``""``.
    """
    boards = [b.model_copy(update={"beat": _resolve(b.beat, beats)}) for b in storyboard.boards]
    return storyboard.model_copy(update={"boards": boards})


def build_animatic(
    edit_id: Slug,
    storyboard: Storyboard,
    vo: dict[str, TempAudio],
    music: TempAudio | None = None,
    *,
    fps: int = 24,
) -> Edit:
    """Cut the boards into an Edit.

    ``vo`` maps a script beat to its temp VO. A beat may span several consecutive
    boards: its VO clip starts on the first board of the beat, links to that picture
    clip and runs its full length across the beat's boards. When the VO is longer than
    the beat's boards, the beat's last board holds for the difference, so no line is
    cut short. Gaps fill the VO track so every track is as long as the picture. Music
    runs from frame 0 and is trimmed to the picture.
    """
    rate = FrameRate.of(fps)
    boards = storyboard.boards
    frames = [max(1, ceil(b.duration_s * fps)) for b in boards]
    runs: list[tuple[int, int, TempAudio | None]] = []
    voiced: set[str] = set()
    i = 0
    while i < len(boards):
        beat = boards[i].beat
        j = i + 1
        while beat and j < len(boards) and boards[j].beat == beat:
            j += 1
        line = vo.get(beat) if beat and beat not in voiced else None
        if line is not None:
            voiced.add(beat)
            length = max(1, round(line.duration_s * fps))
            short = length - sum(frames[i:j])
            if short > 0:
                frames[j - 1] += short
        runs.append((i, j, line))
        i = j
    picture: list[Clip | Gap] = [
        Clip(
            id=f"pic-{board.shot_id}",
            shot_id=board.shot_id,
            media_ref=board.image or f"board:{board.shot_id}",
            source_out=n,
        )
        for board, n in zip(boards, frames, strict=True)
    ]
    voice: list[Clip | Gap] = []
    for start, stop, line in runs:
        span = sum(frames[start:stop])
        if line is None:
            voice.append(Gap(duration=span))
            continue
        used = max(1, round(line.duration_s * fps))
        first = boards[start].shot_id
        voice.append(
            Clip(id=f"vo-{first}", media_ref=line.media_ref, source_out=used, link=f"pic-{first}")
        )
        if used < span:
            voice.append(Gap(duration=span - used))
    tracks = [Track(id="picture", kind="video", items=picture)]
    tracks.append(Track(id="vo", kind="audio", items=voice))
    total = tracks[0].duration
    if music is not None:
        bed = min(total, max(1, ceil(music.duration_s * fps)))
        items: list[Clip | Gap] = [Clip(id="music-bed", media_ref=music.media_ref, source_out=bed)]
        if bed < total:
            items.append(Gap(duration=total - bed))
        tracks.append(Track(id="music", kind="audio", items=items))
    return Edit(id=edit_id, rate=rate, level="sequence", tracks=tracks)


def check_locked_timing(edit: Edit, storyboard: Storyboard) -> list[str]:
    """Problems that stop the animatic playing at locked timing (empty means locked)."""
    problems: list[str] = []
    picture = next((t for t in edit.tracks if t.id == "picture"), None)
    if picture is None:
        return ["no picture track"]
    clips = picture.clips()
    if [c.shot_id for c in clips] != [b.shot_id for b in storyboard.boards]:
        problems.append("picture clips do not follow the storyboard's shot order")
    for track in edit.tracks:
        if track.kind == "audio" and track.duration != picture.duration:
            problems.append(
                f"track {track.id} is {track.duration} frames, picture {picture.duration}"
            )
    for track in edit.tracks:
        for clip in track.clips():
            if clip.link and track.record_start(clip.id) != picture.record_start(clip.link):
                problems.append(f"{clip.id} does not start with {clip.link}")
    for clip, board in zip(clips, storyboard.boards, strict=False):
        if clip.duration < ceil(board.duration_s * edit.rate.fps) - 1:
            problems.append(f"{clip.id} is shorter than its board")
    return problems
