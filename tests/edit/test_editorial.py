# SPDX-License-Identifier: AGPL-3.0-or-later
"""The editorial pass: cuts on the beat, J/L cuts, match cuts, titles, re-paced cut-downs."""

from __future__ import annotations

from fractions import Fraction

import pytest

from spectracut.edit import EditorialReport, check_editorial, editorial_pass, pace_cutdown
from spectracut.edit.editorial import JL_WINDOW, ramp_source, title_frames
from spectracut.edit.timeline import FPS_24, Clip, Edit, Gap, Marker, Track
from spectracut.storyboard.models import Board, Storyboard

PERIOD = 12  # 120 BPM at 24 fps
BEATS = [0.1 + 0.5 * k for k in range(140)]  # off the animatic's grid by 0.1 s
BEAT_FRAMES = sorted({round(Fraction(b) * 24) for b in BEATS})

# shot -> (animatic frames, device or None for an interlude, on-screen text)
SHOTS: list[tuple[str, int, str | None, str]] = [
    ("open", 50, "laptop", "Scrape anything"),
    ("swirl", 40, None, ""),
    ("phone", 70, "phone", ""),
    ("grid", 30, None, ""),
    ("close", 50, "laptop", ""),
]


def _board(shot: str, frames: int, device: str | None, text: str) -> Board:
    return Board.model_validate(
        {
            "shot_id": shot,
            "action": f"{shot} action",
            "duration_s": frames / 24,
            "device": device,
            "text": text,
        }
    )


def storyboard() -> Storyboard:
    return Storyboard(boards=[_board(*s) for s in SHOTS])


def animatic(vo_at: tuple[tuple[int, int], ...] = ((0, 40), (90, 30))) -> Edit:
    picture = [
        Clip(id=f"pic-{s}", shot_id=s, media_ref=f"render:{s}", source_out=n)
        for s, n, _, _ in SHOTS
    ]
    total = sum(n for _, n, _, _ in SHOTS)
    voice: list[Clip | Gap] = []
    at = 0
    for n, (start, length) in enumerate(vo_at):
        if start > at:
            voice.append(Gap(duration=start - at))
        voice.append(Clip(id=f"vo-{n}", media_ref=f"vo/{n}.wav", source_out=length))
        at = start + length
    if at < total:
        voice.append(Gap(duration=total - at))
    music = [Clip(id="music-bed", media_ref="score.wav", source_out=total)]
    return Edit(
        id="edit",
        rate=FPS_24,
        tracks=[
            Track(id="picture", kind="video", items=list(picture)),
            Track(id="vo", kind="audio", items=voice),
            Track(id="music", kind="audio", items=list(music)),
        ],
    )


def _starts(edit: Edit, track_id: str) -> dict[str, int]:
    track = next(t for t in edit.tracks if t.id == track_id)
    return {c.id: track.record_start(c.id) for c in track.clips()}


def _picture(edit: Edit) -> list[Clip]:
    return next(t for t in edit.tracks if t.id == "picture").clips()


def test_every_cut_lands_on_a_beat_within_a_frame() -> None:
    edit, report = editorial_pass(animatic(), BEATS, storyboard=storyboard())
    starts = _starts(edit, "picture")
    for clip in _picture(edit)[1:]:
        assert min(abs(starts[clip.id] - b) for b in BEAT_FRAMES) <= 1, clip.id
    assert {c.reason for c in report.cuts} == {"beat"}
    assert [c.frame for c in report.cuts] == [starts[c.id] for c in _picture(edit)[1:]]
    # The animatic's cuts were off the beat, so the pass really moved them.
    assert any(c.frame != c.original for c in report.cuts)
    assert check_editorial(edit, report, storyboard()) == []


def test_length_and_vo_record_frames_never_move() -> None:
    before = animatic()
    edit, _ = editorial_pass(before, BEATS, storyboard=storyboard())
    assert edit.duration == before.duration
    assert _starts(edit, "vo") == _starts(before, "vo")
    assert _starts(edit, "music") == _starts(before, "music")


def test_no_clip_runs_past_its_board_and_order_follows_the_storyboard() -> None:
    edit, report = editorial_pass(animatic(), BEATS, storyboard=storyboard())
    lengths = dict(zip([f"pic-{s}" for s, *_ in SHOTS], [n for _, n, *_ in SHOTS], strict=True))
    assert report.board_max == {k: n + PERIOD for k, n in lengths.items()}
    for clip in _picture(edit):
        assert clip.duration <= report.board_max[clip.id]
        assert clip.source_in == 0 and clip.source_out == clip.duration
    assert [c.shot_id for c in _picture(edit)] == [s for s, *_ in SHOTS]


def test_vo_lines_become_linked_j_or_l_cuts_within_the_window() -> None:
    edit, report = editorial_pass(animatic(), BEATS, storyboard=storyboard())
    assert {v.clip_id: v.link for v in report.vo_cuts} == {"vo-0": "pic-open", "vo-1": "pic-phone"}
    for v in report.vo_cuts:
        assert edit.audio_offset(v.clip_id) == v.offset
        assert abs(v.offset) <= JL_WINDOW
        assert v.kind == ("J" if v.offset < 0 else "L" if v.offset > 0 else "sync")
    # The phone shot's cut moved off frame 90 onto a beat, so its line is a J or L cut.
    assert next(v for v in report.vo_cuts if v.clip_id == "vo-1").kind in ("J", "L")


def test_a_cut_inside_a_vo_line_beyond_the_window_stays_as_a_vo_override() -> None:
    # One long line covers frames 60..200: the cuts at 90, 160 and 190 sit deep inside it.
    edit, report = editorial_pass(animatic(((60, 140),)), BEATS, storyboard=storyboard())
    overrides = {c.original: c for c in report.cuts if c.reason == "vo-override"}
    assert 90 in overrides and 160 in overrides
    assert all(c.frame == c.original and c.beat_frame is None for c in overrides.values())
    assert _starts(edit, "picture")["pic-phone"] == 90
    assert check_editorial(edit, report, storyboard()) == []


def test_device_interlude_boundaries_get_match_cuts_and_head_ramps() -> None:
    edit, report = editorial_pass(animatic(), BEATS, storyboard=storyboard())
    starts = _starts(edit, "picture")
    boundaries = [starts[f"pic-{s}"] for s in ("swirl", "phone", "grid", "close")]
    assert report.match_cuts == boundaries
    assert sorted(m.frame for m in edit.markers if m.name == "match-cut") == boundaries
    for clip in _picture(edit):
        if clip.shot_id in ("phone", "close"):  # a device clip entered from an interlude
            (ramp,) = clip.ramps
            assert (ramp.start, ramp.speed_from, ramp.speed_to) == (0, 0.5, 1.0)
            assert ramp_source(clip.duration, ramp.end) <= clip.duration
        else:
            assert clip.ramps == []


def test_clips_are_labelled_and_titles_hold_for_their_reading_time() -> None:
    edit, report = editorial_pass(animatic(), BEATS, storyboard=storyboard())
    labels = {c.shot_id: [m.name for m in c.markers if m.name != "title"] for c in _picture(edit)}
    assert labels == {
        "open": ["device:laptop"],
        "swirl": ["interlude"],
        "phone": ["device:phone"],
        "grid": ["interlude"],
        "close": ["device:laptop"],
    }
    (title,) = [m for m in _picture(edit)[0].markers if m.name == "title"]
    # Two words: 2/3 s + 1 s, clamped up to 1.5 s.
    assert title.duration == title_frames("Scrape anything", FPS_24.fps) == 40
    assert title.note == "Scrape anything" and title.frame + title.duration <= 50
    assert report.titles == {"pic-open": 40}


def test_a_clip_too_short_for_its_title_is_rejected() -> None:
    board = storyboard().boards
    wordy = board[3].model_copy(update={"text": "one two three four five six seven eight"})
    sb = Storyboard(boards=[*board[:3], wordy, board[4]])
    with pytest.raises(ValueError, match="title"):
        editorial_pass(animatic(), BEATS, storyboard=sb)


def test_without_beats_or_a_picture_track_nothing_changes() -> None:
    before = animatic()
    edit, report = editorial_pass(before, [], storyboard=storyboard())
    assert _starts(edit, "picture") == _starts(before, "picture")
    assert all(c.reason == "vo-override" for c in report.cuts)
    empty = Edit(id="e", tracks=[Track(id="vo", kind="audio")])
    assert editorial_pass(empty, BEATS) == (empty, EditorialReport())


def test_check_editorial_flags_off_beat_cuts_long_clips_and_reordered_kinds() -> None:
    edit, report = editorial_pass(animatic(), BEATS, storyboard=storyboard())
    problems = check_editorial(animatic(), report, storyboard())
    assert any("off the beat" in p for p in problems)
    tight = report.model_copy(update={"board_max": {**report.board_max, "pic-phone": 10}})
    assert any("board allows" in p for p in check_editorial(edit, tight, storyboard()))
    swapped = Storyboard(boards=[storyboard().boards[n] for n in (1, 0, 2, 3, 4)])
    assert any("differs" in p for p in check_editorial(edit, report, swapped))


# ------------------------------------------------------------------ cut-downs


def _master() -> Edit:
    """A ~60 s labelled master: device shots with interludes between, then the end card."""
    clips: list[Clip] = []
    for n in range(10):
        device = n % 2 == 0
        clips.append(
            Clip(
                id=f"pic-s{n}",
                shot_id=f"s{n}",
                media_ref=f"render:s{n}",
                source_out=168 if device else 96,
                markers=[Marker(name="device:laptop" if device else "interlude", frame=0)],
            )
        )
    clips.append(Clip(id="pic-end-card", shot_id="end-card", media_ref="c", source_out=72))
    return Edit(id="master", rate=FPS_24, tracks=[Track(id="picture", items=list(clips))])


@pytest.mark.parametrize("length", [15, 30])
def test_cutdowns_are_re_paced_on_the_beat_and_fill_the_length(length: int) -> None:
    paced = pace_cutdown(_master(), BEATS, length, "end-card")
    shots = [s for s, _ in paced]
    assert shots[0] == "s0" and shots[-1] == "end-card"
    assert sum(f for _, f in paced) == length * 24
    for shot, frames in paced[:-1]:
        assert frames % PERIOD == 0, shot  # a whole number of beats
        interlude = int(shot[1:]) % 2 == 1
        if interlude:
            assert frames == (1 if length == 15 else 2) * PERIOD
    assert 0 < paced[-1][1] <= 72


def test_the_15s_cut_is_quicker_per_shot_than_the_30s_and_not_a_trim() -> None:
    short = dict(pace_cutdown(_master(), BEATS, 15, "end-card"))
    long = dict(pace_cutdown(_master(), BEATS, 30, "end-card"))
    assert short["s0"] < long["s0"]
    master = {c.shot_id: c.duration for c in _master().tracks[0].clips()}
    # A proportional trim would keep the master's device:interlude ratio (168:96).
    assert short["s0"] * master["s1"] != short["s1"] * master["s0"]


def test_a_master_too_short_for_the_cut_down_is_rejected() -> None:
    short = Edit(
        id="m",
        rate=FPS_24,
        tracks=[
            Track(
                id="picture",
                items=[
                    Clip(id="a", shot_id="a", media_ref="a", source_out=48),
                    Clip(id="e", shot_id="end-card", media_ref="e", source_out=24),
                ],
            )
        ],
    )
    with pytest.raises(ValueError):
        pace_cutdown(short, BEATS, 15, "end-card")
