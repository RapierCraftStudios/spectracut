# SPDX-License-Identifier: AGPL-3.0-or-later
"""The animatic: boards cut to temp VO and music on an :class:`Edit`, at locked timing."""

from __future__ import annotations

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


def build_animatic(
    edit_id: Slug,
    storyboard: Storyboard,
    vo: dict[str, TempAudio],
    music: TempAudio | None = None,
    *,
    fps: int = 24,
) -> Edit:
    """Cut the boards into an Edit.

    ``vo`` maps a script beat to its temp VO. A board lasts as long as the longer of
    its planned duration and its VO, so no line is cut short. Each VO clip starts on
    its board's first frame and links to that picture clip. Music runs from frame 0
    and is trimmed to the picture.
    """
    rate = FrameRate.of(fps)
    picture: list[Clip | Gap] = []
    voice: list[Clip | Gap] = []
    for board in storyboard.boards:
        line = vo.get(board.beat) if board.beat else None
        seconds = max(board.duration_s, line.duration_s if line else 0.0)
        frames = max(1, ceil(seconds * fps))
        pic_id = f"pic-{board.shot_id}"
        picture.append(
            Clip(
                id=pic_id,
                shot_id=board.shot_id,
                media_ref=board.image or f"board:{board.shot_id}",
                source_out=frames,
            )
        )
        if line is None:
            voice.append(Gap(duration=frames))
            continue
        used = min(frames, max(1, ceil(line.duration_s * fps)))
        voice.append(
            Clip(id=f"vo-{board.shot_id}", media_ref=line.media_ref, source_out=used, link=pic_id)
        )
        if used < frames:
            voice.append(Gap(duration=frames - used))
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
