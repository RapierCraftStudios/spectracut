# SPDX-License-Identifier: AGPL-3.0-or-later
"""Edit timeline: OTIO round trip, Resolve import, nested conform and frame-exact timing."""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import opentimelineio as otio
import pytest
from pydantic import ValidationError

from spectracut.edit import (
    FPS_23_976,
    FPS_24,
    FPS_25,
    FPS_30,
    FPS_60,
    Clip,
    Edit,
    FrameRate,
    Gap,
    Marker,
    SpeedRamp,
    Track,
    Transition,
    find_shot,
    from_otio,
    read_otio,
    replace_shot,
    to_otio,
    write_otio,
)

FIXTURES = Path(__file__).parent / "fixtures"
RATES = [FPS_23_976, FPS_24, FPS_25, FPS_30, FPS_60]
RATE_IDS = ["23.976", "24", "25", "30", "60"]


def _hash(c: str) -> str:
    return "sha256:" + c * 64


def _shot(clip_id: str, shot_id: str, src_in: int, src_out: int, **kw: object) -> Clip:
    return Clip.model_validate(
        {
            "id": clip_id,
            "name": clip_id.title(),
            "shot_id": shot_id,
            "shot_hash": _hash(shot_id[-1]),
            "media_ref": f"renders/{shot_id}.exr",
            "source_in": src_in,
            "source_out": src_out,
            **kw,
        }
    )


def _episode(rate: FrameRate) -> Edit:
    """Episode → sequence → two scenes → shots, with every feature the Edit carries."""
    scene1 = Edit(
        id="sc1",
        name="Scene 1",
        rate=rate,
        level="scene",
        tracks=[
            Track(
                id="v1",
                items=[
                    _shot("a", "shot-1", 0, 48, markers=[Marker(name="cue", frame=12)]),
                    Transition(name="Dissolve", in_offset=6, out_offset=6),
                    _shot(
                        "b",
                        "shot-2",
                        100,
                        172,
                        speed=2.0,
                        ramps=[SpeedRamp(start=10, end=40, speed_from=1.0, speed_to=0.25)],
                    ),
                    _shot("c", "shot-3", 7, 37),
                ],
            ),
            Track(
                id="a1",
                kind="audio",
                items=[
                    Gap(duration=40),
                    Clip(id="b-snd", link="b", media_ref="audio/b.wav", source_in=0, source_out=80),
                ],
            ),
            Track(
                id="a2",
                kind="audio",
                items=[
                    Gap(duration=128),
                    Clip(id="c-snd", link="c", media_ref="audio/c.wav", source_in=0, source_out=22),
                ],
            ),
        ],
        markers=[Marker(name="beat", frame=24, duration=2, color="GREEN", note="downbeat")],
    )
    scene2 = Edit(
        id="sc2",
        rate=rate,
        level="scene",
        tracks=[Track(id="v1", items=[_shot("d", "shot-4", 0, 1001)])],
    )
    sequence = Edit(
        id="seq1",
        rate=rate,
        level="sequence",
        tracks=[
            Track(
                id="v1",
                items=[
                    Clip(id="sc1", name="Scene 1", nested=scene1),
                    Clip(id="sc2", name="Scene 2", nested=scene2),
                ],
            )
        ],
    )
    return Edit(
        id="ep1",
        name="Episode 1",
        rate=rate,
        level="episode",
        start_frame=rate.frames(3600),
        tracks=[
            Track(
                id="v1",
                items=[
                    Clip(id="seq1", nested=sequence),
                    Gap(duration=24),
                    Clip(id="recap", nested=sequence, source_in=10, source_out=50),
                ],
            )
        ],
        markers=[Marker(name="act break", frame=500)],
    )


# --- AC: export to OTIO and re-import yields an identical Edit ----------------------------


@pytest.mark.parametrize("rate", RATES, ids=RATE_IDS)
def test_otio_round_trip_is_identical(rate: FrameRate, tmp_path: Path) -> None:
    edit = _episode(rate)
    assert from_otio(to_otio(edit)) == edit

    path = tmp_path / "episode.otio"
    write_otio(edit, path)
    back = read_otio(path)
    assert back == edit
    assert back.content_hash == edit.content_hash


def test_export_is_an_otio_timeline_with_shot_hashes_in_metadata() -> None:
    timeline = to_otio(_episode(FPS_24))
    assert isinstance(timeline, otio.schema.Timeline)
    clips = {c.name: c for c in timeline.find_clips()}
    assert clips["A"].metadata["spectracut"]["shot_hash"] == _hash("1")
    assert clips["A"].metadata["spectracut"]["shot_id"] == "shot-1"
    # Nesting is native OTIO: a nested edit is a Stack inside the parent track.
    assert isinstance(timeline.tracks[0][0], otio.schema.Stack)
    effects = clips["B"].effects
    assert isinstance(effects[0], otio.schema.LinearTimeWarp) and effects[0].time_scalar == 2.0


def test_round_trip_preserves_j_and_l_cuts() -> None:
    scene = _episode(FPS_24).tracks[0].clips()[0].nested
    assert scene is not None
    scene1 = scene.tracks[0].clips()[0].nested
    assert scene1 is not None
    assert scene1.audio_offset("b-snd") == -8  # J cut: sound leads picture b (starts at 48)
    assert scene1.audio_offset("c-snd") == 8  # L cut: sound trails picture c (starts at 120)
    back = from_otio(to_otio(scene1))
    assert back.audio_offset("b-snd") == -8 and back.audio_offset("c-snd") == 8


# --- AC: an OTIO file edited in Resolve imports with retimed cuts applied -----------------


def test_resolve_edited_otio_imports_with_retimed_cuts() -> None:
    edit = read_otio(FIXTURES / "resolve_retimed.otio")

    assert edit.rate == FPS_23_976  # recovered from Resolve's float 23.976023976023978
    assert edit.start_frame == 86400  # 01:00:00:00
    video, audio = edit.tracks
    assert (video.id, video.kind, audio.id, audio.kind) == ("video-1", "video", "audio-1", "audio")

    opener, dissolve, hero, pickup = video.items
    assert isinstance(opener, Clip) and isinstance(hero, Clip) and isinstance(pickup, Clip)
    # The editor's trims win over the in/out points SpectraCut originally exported.
    assert (opener.source_in, opener.source_out) == (0, 40)  # was 0..48
    assert (hero.source_in, hero.source_out, hero.speed) == (96, 180, 0.5)  # was 100..172 @1x
    # SpectraCut identity survives the trip through Resolve.
    assert (opener.id, opener.shot_id, opener.shot_hash) == ("open", "s-open", _hash("a"))
    assert (hero.id, hero.shot_id, hero.shot_hash) == ("hero", "s-hero", _hash("b"))
    # A clip added in Resolve has no SpectraCut metadata and gets an id from its name.
    assert (pickup.id, pickup.shot_id, pickup.source_in, pickup.source_out) == (
        "pickup-01",
        None,
        12,
        36,
    )
    assert pickup.media_ref == "file:///media/pickup01.mov"
    assert dissolve == Transition(name="Cross Dissolve", kind="dissolve", in_offset=6, out_offset=6)

    assert video.duration == 40 + 84 + 24
    assert edit.duration == 148
    assert edit.markers == [Marker(name="Music hit", frame=88, duration=1, color="BLUE",
                                   note="hit on the cut")]  # fmt: skip


# --- AC: nested timelines conform ---------------------------------------------------------


def test_shot_change_updates_scene_sequence_and_episode_durations() -> None:
    episode = _episode(FPS_24)
    seq = episode.tracks[0].clips()[0].nested
    assert seq is not None
    scene1 = seq.tracks[0].clips()[0].nested
    assert scene1 is not None
    before = (scene1.duration, seq.duration, episode.duration)
    assert before == (150, 150 + 1001, 1151 + 24 + 40)

    assert find_shot(episode, "shot-2") == [("seq1", "sc1", "b"), ("recap", "sc1", "b")]
    conformed = replace_shot(episode, "shot-2", source_out=182, shot_hash=_hash("9"))

    new_seq = conformed.tracks[0].clips()[0].nested
    assert new_seq is not None
    new_scene1 = new_seq.tracks[0].clips()[0].nested
    assert new_scene1 is not None
    # +10 frames on shot-2 → scene, sequence and episode each grow by exactly 10.
    # The trimmed recap keeps its fixed 40-frame window.
    assert (new_scene1.duration, new_seq.duration, conformed.duration) == (
        before[0] + 10,
        before[1] + 10,
        before[2] + 10,
    )
    hero = new_scene1.tracks[0].clips()[1]
    assert (hero.source_out, hero.shot_hash) == (182, _hash("9"))
    recap = conformed.tracks[0].clips()[1].nested
    assert recap is not None and recap.duration == new_seq.duration  # every use conformed
    assert episode.duration == before[2]  # the original is untouched (frozen)
    assert from_otio(to_otio(conformed)) == conformed


def test_conform_unknown_shot_raises() -> None:
    with pytest.raises(KeyError, match="shot-x"):
        replace_shot(_episode(FPS_24), "shot-x", source_out=10)


def test_conform_rejects_invalid_change() -> None:
    with pytest.raises(ValidationError):
        replace_shot(_episode(FPS_24), "shot-2", source_out=50)  # before source_in 100


# --- AC: timing is frame-exact at 23.976, 24, 25, 30 and 60 fps ---------------------------


@pytest.mark.parametrize("rate", RATES, ids=RATE_IDS)
def test_timing_is_frame_exact(rate: FrameRate) -> None:
    # An hour of cuts with awkward lengths: any float drift would show up as a lost frame.
    lengths = [1, 1001, 7, 1439, 24, 3] * 120
    items: list[Clip | Gap | Transition] = []
    at = 0
    for n, length in enumerate(lengths):
        items.append(_shot(f"c{n}", f"shot-{n}", at, at + length))
        at += length
    edit = Edit(
        id="long", rate=rate, start_frame=rate.frames(3600), tracks=[Track(id="v1", items=items)]
    )

    total = sum(lengths)
    assert edit.duration == total
    assert edit.seconds == Fraction(total * rate.denominator, rate.numerator)
    assert rate.frames(edit.seconds) == total

    text = otio.adapters.write_to_string(to_otio(edit))
    timeline = otio.adapters.read_from_string(text)
    otio_frames = timeline.duration().rescaled_to(float(rate)).value
    assert abs(otio_frames - total) < 1e-6
    back = from_otio(timeline)
    assert back == edit
    assert [c.source_out for c in back.tracks[0].clips()][-1] == total


@pytest.mark.parametrize(
    ("value", "rate"),
    [
        (23.976023976023978, FPS_23_976),
        (23.976, FPS_23_976),
        (24.0, FPS_24),
        (25.0, FPS_25),
        (29.97002997002997, FrameRate(numerator=30000, denominator=1001)),
        (30.0, FPS_30),
        (59.94, FrameRate(numerator=60000, denominator=1001)),
        (60.0, FPS_60),
    ],
)
def test_frame_rate_recovered_exactly_from_float(value: float, rate: FrameRate) -> None:
    assert FrameRate.from_float(value) == rate


def test_23_976_one_hour_is_exact() -> None:
    assert FPS_23_976.frames(3600) == 86314  # 3600 * 24000 / 1001 = 86313.69 → 86314
    assert FPS_23_976.seconds(24000) == 1001


# --- model validation ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        {"numerator": 48, "denominator": 2},
        {"numerator": 0},
    ],
)
def test_frame_rate_rejects_bad_input(bad: dict[str, int]) -> None:
    with pytest.raises(ValidationError):
        FrameRate.model_validate(bad)


def test_transition_must_sit_between_clips() -> None:
    with pytest.raises(ValidationError, match="between two clips"):
        Track(id="v1", items=[Transition(in_offset=1, out_offset=1), _shot("a", "s-a", 0, 10)])


def test_clip_needs_out_point_after_in_point() -> None:
    with pytest.raises(ValidationError, match="source_out"):
        _shot("a", "s-a", 10, 10)
    with pytest.raises(ValidationError, match="source_out is required"):
        Clip(id="a", source_in=0)


def test_audio_link_must_name_a_picture_clip() -> None:
    with pytest.raises(ValidationError, match="link"):
        Edit(
            id="e",
            tracks=[
                Track(id="a1", kind="audio", items=[Clip(id="s", link="nope", source_out=5)]),
            ],
        )


def test_nested_edit_must_share_rate() -> None:
    inner = Edit(id="in", rate=FPS_25, tracks=[Track(id="v1", items=[_shot("a", "s-a", 0, 5)])])
    with pytest.raises(ValidationError, match="share rate"):
        Edit(id="out", rate=FPS_24, tracks=[Track(id="v1", items=[Clip(id="n", nested=inner)])])
