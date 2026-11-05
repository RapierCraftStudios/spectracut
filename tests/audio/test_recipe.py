# SPDX-License-Identifier: AGPL-3.0-or-later
"""AudioRecipe: cue placement (R4), sound cache and partial re-render (R3), validation."""

from __future__ import annotations

from array import array
from fractions import Fraction
from pathlib import Path

import pytest
from pydantic import ValidationError

from spectracut.audio.ardour import BUILD_OK, EXPORT_OK, SOURCE_LINE, layer_starts, track_name
from spectracut.audio.recipe import (
    AudioRecipe,
    AutomationCurve,
    AutomationPoint,
    Cue,
    Layer,
    Provenance,
    SoundSpec,
    place_cues,
    sound_key,
)
from spectracut.audio.render import (
    Channels,
    SoundCache,
    ToneGenerator,
    read_wav,
    render_mix,
    write_wav,
)
from spectracut.edit.timeline import (
    FPS_23_976,
    FPS_24,
    FPS_25,
    FPS_30,
    FPS_60,
    Edit,
    FrameRate,
    Marker,
)

FPS_29_97 = FrameRate(numerator=30000, denominator=1001)
RATES = [FPS_23_976, FPS_24, FPS_25, FPS_29_97, FPS_30, FPS_60]
PROV = Provenance(source="procedural", origin="ToneGenerator", licence="AGPL-3.0", commercial=True)


def sound(kind: str = "tone", seconds: float = 0.1, **params: float) -> SoundSpec:
    return SoundSpec(kind=kind, duration_seconds=seconds, params=dict(params), provenance=PROV)


def cue(cid: str, bus: str = "fx", **kw: object) -> Cue:
    layers = kw.pop("layers", [Layer(id="body", sound=sound(freq=200.0 + len(cid)))])
    return Cue.model_validate({"id": cid, "bus": bus, "layers": layers, **kw})


# ------------------------------------------------------------------------ R4 placement


@pytest.mark.parametrize("rate", RATES, ids=lambda r: f"{float(r):.3f}")
def test_every_cue_is_placed_within_one_frame_of_its_event(rate: FrameRate) -> None:
    ten_min = int(600 * rate.fps)
    frames = [0, 1, 7, 1001, 1439, ten_min - 12]
    edit = Edit(
        id="film",
        rate=rate,
        markers=[Marker(name=f"m{i}", frame=f) for i, f in enumerate(frames)],
    )
    cues = [cue(f"c{i}", marker=f"m{i}") for i in range(len(frames))]
    cues += [cue(f"abs{i}", frame=f) for i, f in enumerate(frames)]
    recipe = AudioRecipe(id="mix", rate=rate, duration_frames=ten_min, cues=cues)
    placed = place_cues(recipe, edit)
    one_frame = Fraction(recipe.sample_rate) / rate.fps
    for i, f in enumerate(frames):
        event = rate.seconds(f) * recipe.sample_rate
        for cid in (f"c{i}", f"abs{i}"):
            assert abs(placed[cid] - event) < one_frame, (cid, placed[cid], event)
            assert abs(placed[cid] - event) <= Fraction(1, 2)  # nearest sample, no drift


@pytest.mark.parametrize("rate", RATES, ids=lambda r: f"{float(r):.3f}")
def test_offset_frames_shift_by_exact_frames(rate: FrameRate) -> None:
    edit = Edit(id="film", rate=rate, markers=[Marker(name="hit", frame=500)])
    recipe = AudioRecipe(
        id="mix",
        rate=rate,
        duration_frames=1000,
        cues=[
            cue("early", marker="hit", offset_frames=-2),
            cue("late", marker="hit", offset_frames=3),
        ],
    )
    placed = place_cues(recipe, edit)
    sr = recipe.sample_rate
    assert placed["early"] == round(rate.seconds(498) * sr)
    assert placed["late"] == round(rate.seconds(503) * sr)


def test_layer_offsets_follow_their_cue() -> None:
    c = cue(
        "hit",
        frame=24,
        layers=[
            Layer(id="sub", sound=sound(freq=50.0)),
            Layer(id="tail", sound=sound(freq=900.0), offset_samples=480),
        ],
    )
    recipe = AudioRecipe(id="mix", duration_frames=96, cues=[c])
    starts = layer_starts(recipe, place_cues(recipe, Edit(id="e")))
    assert starts == {"hit.sub": 48000, "hit.tail": 48480}


def test_placement_errors() -> None:
    edit = Edit(id="film", markers=[Marker(name="dup", frame=1), Marker(name="dup", frame=2)])
    with pytest.raises(KeyError, match="no edit marker"):
        place_cues(AudioRecipe(id="m", duration_frames=48, cues=[cue("a", marker="x")]), edit)
    with pytest.raises(ValueError, match="ambiguous"):
        place_cues(AudioRecipe(id="m", duration_frames=48, cues=[cue("a", marker="dup")]), edit)
    with pytest.raises(ValueError, match="does not match edit rate"):
        place_cues(AudioRecipe(id="m", rate=FPS_25, duration_frames=48), edit)
    with pytest.raises(ValueError, match="before the timeline"):
        bad = cue("a", frame=0, offset_frames=-1)
        place_cues(AudioRecipe(id="m", duration_frames=48, cues=[bad]), edit)
    with pytest.raises(ValueError, match="past the recipe end"):
        place_cues(AudioRecipe(id="m", duration_frames=2, cues=[cue("a", frame=1)]), edit)


# ----------------------------------------------------------------------- validation


def test_recipe_validation() -> None:
    with pytest.raises(ValidationError, match="exactly one of marker or frame"):
        Cue(id="a", bus="fx", layers=[Layer(id="l", sound=sound())])
    with pytest.raises(ValidationError, match="static fader value"):
        cue(
            "a",
            frame=0,
            fader=1.0,
            automation=AutomationCurve(points=[AutomationPoint(frame=0, gain=0.0)]),
        )
    with pytest.raises(ValidationError, match="strictly increasing"):
        AutomationCurve(
            points=[AutomationPoint(frame=5, gain=1.0), AutomationPoint(frame=5, gain=0.5)]
        )
    with pytest.raises(ValidationError, match="duplicate cue"):
        AudioRecipe(id="m", duration_frames=48, cues=[cue("a", frame=0), cue("a", frame=1)])
    with pytest.raises(ValidationError, match="buses must be"):
        AudioRecipe.model_validate({"id": "m", "duration_frames": 48, "buses": [{"id": "dx"}]})
    with pytest.raises(ValidationError):
        cue("a", bus="sfx", frame=0)


def test_sound_key_ignores_mix_placement_and_tracks_the_sound() -> None:
    s = sound(freq=300.0)
    a = Cue(id="a", bus="fx", frame=0, layers=[Layer(id="l", sound=s)])
    b = Cue(id="b", bus="mx", frame=40, gain_db=-6, layers=[Layer(id="x", sound=s, gain_db=3)])
    assert sound_key(a.layers[0].sound) == sound_key(b.layers[0].sound)
    assert sound_key(s) != sound_key(sound(freq=301.0))
    assert sound_key(s) != sound_key(s.model_copy(update={"seed": 1}))
    assert sound_key(s).startswith("sha256:")


# --------------------------------------------------------------- R3 partial re-render


class CountingGenerator(ToneGenerator):
    def __init__(self) -> None:
        self.calls: list[str] = []

    def generate(self, spec: SoundSpec, sample_rate: int) -> Channels:
        self.calls.append(sound_key(spec))
        return super().generate(spec, sample_rate)


class FakeArdour:
    """Stands in for ardour8-lua: makes the placeholders and the exports it would."""

    def __init__(self, recipe: AudioRecipe) -> None:
        self.recipe = recipe
        self.builds = 0

    def run(self, script: Path, workdir: Path, xdg: Path, marker: str) -> str:
        sr, rec = self.recipe.sample_rate, self.recipe
        if marker == BUILD_OK:
            self.builds += 1
            audiofiles = workdir / "session" / "interchange" / "session" / "audiofiles"
            lines = []
            for c in rec.cues:
                for layer in c.layers:
                    name = track_name(c, layer)
                    for ch, side in enumerate("LR"):
                        file = f"ph_{name}%{side}.wav"
                        silent = array("f", [0.0]) * layer.sound.samples(sr)
                        write_wav(audiofiles / file, [silent], sr, float_format=True)
                        lines.append(f"{SOURCE_LINE}\t{name}\t{ch}\t{file}")
            return "\n".join([*lines, BUILD_OK])
        silent = array("f", [0.0]) * rec.duration_samples
        for name in ("master", "dx", "mx", "fx"):
            write_wav(
                workdir / "export" / name / "mix.wav", [silent, silent], sr, float_format=False
            )
        return "SPECTRACUT_ROUTING_OK\nSPECTRACUT_AUTOMATION_OK\n" + EXPORT_OK


def _film() -> tuple[AudioRecipe, Edit]:
    edit = Edit(id="film", markers=[Marker(name="hit", frame=24), Marker(name="vo", frame=6)])
    recipe = AudioRecipe(
        id="mix",
        duration_frames=96,
        cues=[
            cue("vo", bus="dx", marker="vo"),
            cue("bed", bus="mx", frame=0, layers=[Layer(id="pad", sound=sound(seconds=2.0))]),
            cue(
                "hit",
                marker="hit",
                layers=[
                    Layer(id="sub", sound=sound(freq=40.0)),
                    Layer(id="air", sound=sound("noise", seed=3)),
                ],
            ),
        ],
    )
    return recipe, edit


def test_changing_one_cue_regenerates_only_its_sounds(tmp_path: Path) -> None:
    recipe, edit = _film()
    gen = CountingGenerator()
    cache = SoundCache(tmp_path / "sounds")
    fake = FakeArdour(recipe)

    first = render_mix(recipe, edit, tmp_path, generator=gen, runner=fake, cache=cache)  # type: ignore[arg-type]
    assert len(first.generated) == 4 and len(gen.calls) == 4
    assert first.master.is_file() and set(first.stems) == {"dx", "mx", "fx"}

    # re-render unchanged: nothing generated, mix still rebuilt
    again = render_mix(recipe, edit, tmp_path, generator=gen, runner=fake, cache=cache)  # type: ignore[arg-type]
    assert again.generated == [] and len(gen.calls) == 4 and fake.builds == 2

    # change one cue's sound: exactly that sound is generated
    hit = recipe.cue("hit")
    new_sub = hit.layers[0].model_copy(update={"sound": sound(freq=45.0)})
    changed_hit = hit.model_copy(update={"layers": [new_sub, hit.layers[1]]})
    changed = recipe.model_copy(
        update={"cues": [changed_hit if c.id == "hit" else c for c in recipe.cues]}
    )
    fake.recipe = changed
    third = render_mix(changed, edit, tmp_path, generator=gen, runner=fake, cache=cache)  # type: ignore[arg-type]
    assert third.generated == [sound_key(sound(freq=45.0))]
    assert gen.calls[4:] == third.generated and fake.builds == 3

    # mix-only changes (gain, offset, bus, fader automation) generate nothing
    vo = recipe.cue("vo")
    remixed_vo = vo.model_copy(
        update={
            "gain_db": -9.0,
            "offset_frames": 2,
            "bus": "fx",
            "automation": AutomationCurve(
                points=[AutomationPoint(frame=0, gain=1.0), AutomationPoint(frame=40, gain=0.2)]
            ),
        }
    )
    remixed = changed.model_copy(
        update={"cues": [remixed_vo if c.id == "vo" else c for c in changed.cues]}
    )
    fake.recipe = remixed
    fourth = render_mix(remixed, edit, tmp_path, generator=gen, runner=fake, cache=cache)  # type: ignore[arg-type]
    assert fourth.generated == [] and len(gen.calls) == 5 and fake.builds == 4
    assert fourth.placements["vo"] == first.placements["vo"] + 4000


def test_cache_survives_a_new_instance(tmp_path: Path) -> None:
    gen = CountingGenerator()
    s = sound("noise", seed=9)
    a = SoundCache(tmp_path).load(s, gen, 48000)
    b = SoundCache(tmp_path).load(s, gen, 48000)
    assert len(gen.calls) == 1 and list(a[0]) == list(b[0])


def test_tone_generator_is_deterministic_and_shaped() -> None:
    gen = ToneGenerator()
    noise = sound("noise", seconds=0.05, seed=4)
    assert list(gen.generate(noise, 48000)[0]) == list(gen.generate(noise, 48000)[0])
    mono = SoundSpec(kind="tone", duration_seconds=0.01, channels=1, provenance=PROV)
    out = gen.generate(mono, 48000)
    assert len(out) == 1 and len(out[0]) == 480
    with pytest.raises(ValueError, match="cannot make"):
        gen.generate(sound("riser"), 48000)


@pytest.mark.parametrize("float_format", [True, False])
def test_wav_round_trip(tmp_path: Path, float_format: bool) -> None:
    left = array("f", [0.0, 0.5, -0.5, 0.25, -1.0])
    right = array("f", [0.1, -0.1, 0.2, -0.2, 0.0])
    write_wav(tmp_path / "x.wav", [left, right], 44100, float_format=float_format)
    rate, chans = read_wav(tmp_path / "x.wav")
    assert rate == 44100 and len(chans) == 2
    for got, want in zip(chans, (left, right), strict=True):
        assert all(abs(g - w) < 1e-6 for g, w in zip(got, want, strict=True))
