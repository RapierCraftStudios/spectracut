# SPDX-License-Identifier: AGPL-3.0-or-later
"""Event-driven sound cues: events from edit, recipe and capture; cues; procedural design."""

from __future__ import annotations

import json
import os
import shutil
from fractions import Fraction
from pathlib import Path

import pytest

from spectracut.audio.cues import PROCEDURAL, cue_recipe, cues_for_events, whoosh_shape
from spectracut.audio.events import (
    SoundEvent,
    capture_events,
    collect_events,
    edit_events,
    recipe_events,
)
from spectracut.audio.procedural.supercollider import SuperColliderGenerator, build_score
from spectracut.audio.procedural.synth import (
    PROCEDURAL_KINDS,
    ProceduralGenerator,
    active_span_seconds,
    peak_time_seconds,
    spectral_centroid,
)
from spectracut.audio.recipe import AudioRecipe, Cue, SoundSpec, place_cues, sound_key
from spectracut.audio.render import SoundCache
from spectracut.edit.timeline import FPS_23_976, FPS_24, Clip, Edit, FrameRate, Gap, Track
from spectracut.edit.timeline import Transition as Tr
from spectracut.spec.recipe import Recipe, RecipeStep

SR = 48000

CAPTURE = [
    {"type": "navigation", "frame": 0, "timeMs": 0, "url": "https://example.test/"},
    {"type": "click", "frame": 12, "timeMs": 500, "x": 10, "y": 20},
    {"type": "keystroke", "frame": 24, "timeMs": 1000, "key": "a", "scheduledMs": 1000},
    {"type": "keystroke", "frame": 27, "timeMs": 1125, "key": "b", "scheduledMs": 1125},
    {"type": "keystroke", "frame": 30, "timeMs": 1250, "key": "c", "scheduledMs": 1250},
    {"type": "scroll", "frame": 48, "timeMs": 2000, "deltaY": 300},
    {"type": "navigation", "frame": 72, "timeMs": 3000, "url": "https://example.test/next"},
    {"type": "drag", "frame": 96, "timeMs": 4000, "x": 1, "y": 1, "phase": "start"},
    {"type": "drag", "frame": 98, "timeMs": 4100, "x": 5, "y": 1, "phase": "move"},
    {"type": "drag", "frame": 101, "timeMs": 4200, "x": 9, "y": 1, "phase": "end"},
    {"type": "click", "frame": 216, "timeMs": 9000, "x": 0, "y": 0},  # after the clip's out
]


def clip(cid: str, length: int, source_in: int = 0) -> Clip:
    return Clip(id=cid, source_in=source_in, source_out=source_in + length)


def film(rate: FrameRate = FPS_24) -> Edit:
    """30 s at 24 fps: cut, transition, capture clip, cut to black, camera shot."""
    items = [
        clip("a", 120),
        clip("b", 120),
        Tr(in_offset=6, out_offset=6),
        clip("ui", 160),
        Gap(duration=40),
        clip("shot", 280, source_in=10),
    ]
    return Edit(id="film", rate=rate, tracks=[Track(id="v1", items=items)])


def camera_recipe() -> Recipe:
    keys = [
        {"frame": 10, "location": [0, 0, 0]},
        {"frame": 58, "location": [0, 0, 4]},  # 4 units in 2 s
        {"frame": 106, "location": [0, 0, 4]},  # hold
        {"frame": 130, "location": [6, 0, 4]},  # 6 units in 1 s
    ]
    steps = [
        RecipeStep(index=0, tool="camera.add", args={"name": "cam"}),
        RecipeStep(index=1, tool="camera.move", args={"keys": keys, "easing": "ease_in_out"}),
    ]
    return Recipe(id="shot-recipe", shot_id="shot", blender_version="4.2", steps=steps)


def by_frame(cues: list[Cue]) -> dict[int, list[Cue]]:
    out: dict[int, list[Cue]] = {}
    for c in cues:
        assert c.frame is not None
        out.setdefault(c.frame, []).append(c)
    return out


# ----------------------------------------------------------------- AC1: every event cued


def test_30s_edit_with_capture_gets_a_cue_on_every_cut_camera_move_and_ui_event() -> None:
    edit = film()
    assert edit.duration == 720 and edit.seconds == 30
    events = collect_events(edit, recipes={"shot": camera_recipe()}, captures={"ui": CAPTURE})

    cuts = sorted(e.frame for e in events if e.kind == "cut")
    assert cuts == [120, 400, 440]  # a|b, ui|black, black|shot
    transitions = [(e.frame, e.duration_frames) for e in events if e.kind == "transition"]
    assert transitions == [(234, 12)]
    moves = [e for e in events if e.kind == "camera_move"]
    assert [(m.frame, m.duration_frames) for m in moves] == [(440, 48), (536, 24)]
    assert [m.params["speed"] for m in moves] == pytest.approx([2.0, 6.0])
    ui = [e for e in events if e.source == "capture"]
    # capture clip starts at edit frame 240; the event after its out point is dropped
    assert [e.frame for e in ui] == [240, 252, 264, 267, 270, 288, 312, 336, 338, 341]
    assert {e.kind for e in ui} == {"click", "keystroke", "scroll", "navigation", "drag"}

    recipe = cue_recipe(edit, events, "film-fx", sample_rate=SR)
    placed = place_cues(recipe, edit)
    cued = by_frame(recipe.cues)
    for event in [*moves, *ui] + [e for e in events if e.kind in ("cut", "transition")]:
        kinds = [c.id.split("-")[0] for c in cued.get(event.frame, [])]
        assert event.kind.split("_")[0] in kinds, (event.kind, event.frame, kinds)
    assert len(recipe.cues) == len(events)
    for c in recipe.cues:
        assert c.frame is not None
        assert placed[c.id] == recipe.frame_sample(c.frame + c.offset_frames)
        assert c.bus == "fx"
        for layer in c.layers:
            assert layer.sound.provenance == PROCEDURAL
            assert layer.sound.kind in PROCEDURAL_KINDS


def test_every_cue_sound_renders_procedurally_with_the_exact_shape(tmp_path: Path) -> None:
    edit = film()
    extra = [
        SoundEvent(kind="title", frame=600, source="typography", params={"size": "large"}),
        SoundEvent(kind="effect", frame=660, source="recipe"),
    ]
    events = collect_events(
        edit, recipes={"shot": camera_recipe()}, captures={"ui": CAPTURE}, extra=extra
    )
    recipe = cue_recipe(edit, events, "film-fx", sample_rate=SR)
    place_cues(recipe, edit)
    cache = SoundCache(tmp_path)
    gen = ProceduralGenerator()
    specs = {sound_key(layer.sound): layer.sound for c in recipe.cues for layer in c.layers}
    for spec in specs.values():
        chans = cache.load(spec, gen, SR)
        assert len(chans) == spec.channels and len(chans[0]) == spec.samples(SR)
        assert 0.0 < max(abs(v) for v in chans[0]) <= 0.5 + 1e-6
    assert len(cache.generated) == len(specs)


def test_cue_recipe_trims_cues_at_the_end_of_the_edit() -> None:
    edit = film()
    late = [SoundEvent(kind="camera_move", frame=715, source="recipe", params={"speed": 9.0})]
    recipe = cue_recipe(edit, late, "fx", sample_rate=SR)
    place_cues(recipe, edit)  # would raise if the whoosh ran past the end
    (layer,) = recipe.cues[0].layers
    assert layer.sound.samples(SR) == recipe.duration_samples - recipe.frame_sample(715)


# ------------------------------------------------------------- AC2: faster = longer, brighter


def _whoosh(speed: float) -> tuple[float, float]:
    event = SoundEvent(
        kind="camera_move", frame=0, duration_frames=48, source="recipe", params={"speed": speed}
    )
    (cue,) = cues_for_events([event], FPS_24, SR)
    (layer,) = cue.layers
    assert layer.sound.kind == "whoosh"
    (chan, _) = ProceduralGenerator().generate(layer.sound, SR)
    return active_span_seconds(chan, SR), spectral_centroid(chan, SR)


def test_a_faster_camera_move_gives_a_longer_brighter_whoosh() -> None:
    slow_len, slow_centroid = _whoosh(1.0)
    fast_len, fast_centroid = _whoosh(8.0)
    assert fast_len > slow_len * 1.3, (slow_len, fast_len)
    assert fast_centroid > slow_centroid * 1.5, (slow_centroid, fast_centroid)


def test_whoosh_shape_is_monotonic_in_speed() -> None:
    shapes = [whoosh_shape(s) for s in (0.0, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0)]
    assert all(b[0] > a[0] and b[1] > a[1] for a, b in zip(shapes, shapes[1:], strict=False))


# ------------------------------------------------------------- AC3: riser peaks on target


@pytest.mark.parametrize("rate", [FPS_24, FPS_23_976], ids=["24", "23.976"])
@pytest.mark.parametrize("frame", [300, 30, 1])
def test_procedural_riser_peaks_within_one_frame_of_its_target(rate: FrameRate, frame: int) -> None:
    edit = film(rate)
    effect = SoundEvent(kind="effect", frame=frame, source="recipe")
    recipe = cue_recipe(edit, [effect], "fx", sample_rate=SR)
    start = place_cues(recipe, edit)[recipe.cues[0].id]
    riser = next(layer for layer in recipe.cues[0].layers if layer.id == "riser")
    (chan, _) = ProceduralGenerator().generate(riser.sound, SR)
    peak = start + riser.offset_samples + peak_time_seconds(chan, SR) * SR
    target = rate.seconds(frame) * SR
    one_frame = Fraction(SR) / rate.fps
    assert abs(peak - target) < one_frame, (peak, float(target), float(one_frame))
    impact = next(layer for layer in recipe.cues[0].layers if layer.id == "body")
    assert abs(start + impact.offset_samples - target) <= 1


def test_effect_on_frame_zero_has_no_riser() -> None:
    (cue,) = cues_for_events([SoundEvent(kind="effect", frame=0, source="recipe")], FPS_24)
    assert [layer.id for layer in cue.layers] == ["body"] and cue.offset_frames == 0


# --------------------------------------------------------------------- determinism, timing


def test_same_events_give_identical_cues_and_samples() -> None:
    edit = film()
    a = collect_events(edit, recipes={"shot": camera_recipe()}, captures={"ui": CAPTURE})
    b = collect_events(edit, recipes={"shot": camera_recipe()}, captures={"ui": CAPTURE})
    ra, rb = cue_recipe(edit, a, "fx"), cue_recipe(edit, b, "fx")
    assert ra.content_hash == rb.content_hash
    spec = next(layer.sound for c in ra.cues for layer in c.layers if layer.sound.kind == "whoosh")
    gen = ProceduralGenerator()
    assert gen.generate(spec, SR) == gen.generate(spec, SR)
    other = SoundSpec.model_validate({**spec.model_dump(), "seed": spec.seed + 1})
    assert gen.generate(other, SR) != gen.generate(spec, SR)


def test_capture_times_map_exactly_at_23_976(tmp_path: Path) -> None:
    edit = Edit(
        id="ntsc",
        rate=FPS_23_976,
        tracks=[Track(id="v1", items=[clip("pre", 100), clip("ui", 200, source_in=24)])],
    )
    path = tmp_path / "events.json"
    path.write_text(
        json.dumps(
            [
                {"type": "click", "frame": 24, "timeMs": 1001},  # exactly source frame 24
                {"type": "click", "frame": 24, "timeMs": 1022},  # 24.503 -> 25
                {"type": "click", "frame": 23, "timeMs": 980},  # before the clip's in
            ]
        )
    )
    events = capture_events(path, edit, "ui")
    assert [e.frame for e in events] == [100, 101]
    by_frame_only = capture_events([{"type": "scroll", "frame": 60}], edit, "ui", fps=30)
    assert [e.frame for e in by_frame_only] == [100 + round(Fraction(2) * FPS_23_976.fps) - 24]


def test_edit_events_cover_speed_ramps_and_skip_audio_tracks() -> None:
    from spectracut.edit.timeline import SpeedRamp

    ramped = Clip(
        id="r",
        source_in=0,
        source_out=96,
        ramps=[SpeedRamp(start=24, end=48, speed_from=1.0, speed_to=0.25)],
    )
    edit = Edit(
        id="e",
        tracks=[
            Track(id="v", items=[clip("x", 48), ramped]),
            Track(id="a", kind="audio", items=[clip("s1", 10), clip("s2", 10)]),
        ],
    )
    events = edit_events(edit)
    assert [(e.kind, e.frame, e.duration_frames) for e in events] == [
        ("cut", 48, 0),
        ("speed_ramp", 72, 24),
    ]


def test_recipe_keys_outside_the_clip_are_ignored() -> None:
    edit = film()
    # The shot clip shows source frames 10-289; a move from frame 0 starts before it.
    rec = Recipe(
        id="r",
        shot_id="shot",
        blender_version="4.2",
        steps=[
            RecipeStep(
                index=0,
                tool="camera.move",
                args={
                    "keys": [
                        {"frame": 0, "location": [0, 0, 0]},
                        {"frame": 20, "location": [1, 0, 0]},
                    ]
                },
            )
        ],
    )
    assert recipe_events(rec, edit, "shot") == []


# ------------------------------------------------------------------------- SuperCollider


def test_supercollider_score_has_only_numeric_params() -> None:
    spec = SoundSpec(
        kind="riser",
        duration_seconds=1.0,
        params={"peak_s": 0.9, "f0": 100},
        seed=7,
        provenance=PROCEDURAL,
    )
    score = build_score(spec, SR, Path("/tmp/x.wav"))
    assert "RandSeed.ir(1, 7)" in score and "XLine.ar(100.0, 2000.0, 0.9)" in score
    bad = SoundSpec(
        kind="riser", duration_seconds=1.0, params={"f0": "1).foo"}, provenance=PROCEDURAL
    )
    with pytest.raises(ValueError, match="must be a number"):
        build_score(bad, SR, Path("/tmp/x.wav"))
    with pytest.raises(ValueError, match="cannot make"):
        build_score(spec.model_copy(update={"kind": "speech"}), SR, Path("/tmp/x.wav"))


@pytest.mark.e2e
@pytest.mark.skipif(
    os.environ.get("SPECTRACUT_E2E") != "1" or shutil.which("sclang") is None,
    reason="set SPECTRACUT_E2E=1 on a host with SuperCollider",
)
def test_supercollider_riser_peaks_within_one_frame() -> None:
    edit = film()
    recipe: AudioRecipe = cue_recipe(
        edit, [SoundEvent(kind="effect", frame=300, source="recipe")], "fx"
    )
    riser = next(layer for layer in recipe.cues[0].layers if layer.id == "riser")
    (chan, _) = SuperColliderGenerator().generate(riser.sound, SR)
    start = place_cues(recipe, edit)[recipe.cues[0].id]
    peak = start + peak_time_seconds(chan, SR) * SR
    assert abs(peak - FPS_24.seconds(300) * SR) < SR / 24
