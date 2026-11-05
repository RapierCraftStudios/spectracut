# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ardour session writer: generated Lua (unit) and a real render through Ardour (e2e, R1/R2).

The e2e test runs ``ardour8-lua`` from PATH or the pinned ``spectracut-audio:current``
image::

    SPECTRACUT_E2E=1 uv run pytest -q -m e2e tests/audio/test_ardour.py

With ``SPECTRACUT_E2E=1`` and no Ardour available it fails instead of skipping.
"""

from __future__ import annotations

import math
import os
from array import array
from fractions import Fraction
from pathlib import Path

import pytest

from spectracut.audio.ardour import (
    AUTOMATION_OK,
    BUILD_OK,
    EXPORT_PRESET_ID,
    ROUTING_OK,
    SOURCE_LINE,
    SessionError,
    build_lua,
    lua_str,
    parse_sources,
    reload_export_lua,
    swap_sources,
    write_export_preset,
)
from spectracut.audio.recipe import (
    AudioRecipe,
    AutomationCurve,
    AutomationPoint,
    Bus,
    Cue,
    Layer,
    Provenance,
    SoundSpec,
    event_frame,
    place_cues,
    sound_key,
)
from spectracut.audio.render import (
    ArdourRunner,
    Channels,
    SoundCache,
    ToneGenerator,
    read_wav,
    render_mix,
    stem_sum_error_db,
    write_wav,
)
from spectracut.edit.timeline import Edit, Marker

PROV = Provenance(source="procedural", origin="ToneGenerator", licence="AGPL-3.0", commercial=True)


def tone(freq: float, seconds: float = 0.25, **params: float) -> SoundSpec:
    return SoundSpec(
        kind="tone", duration_seconds=seconds, params={"freq": freq, **params}, provenance=PROV
    )


def _film() -> tuple[AudioRecipe, Edit]:
    edit = Edit(
        id="film",
        markers=[Marker(name="vo", frame=6), Marker(name="hit", frame=30)],
    )
    recipe = AudioRecipe(
        id="mix",
        duration_frames=48,
        buses=[Bus(id="dx"), Bus(id="mx", gain_db=-3.0), Bus(id="fx")],
        cues=[
            Cue(
                id="vo",
                bus="dx",
                marker="vo",
                fader=1.0,
                automation=AutomationCurve(
                    points=[AutomationPoint(frame=0, gain=1.0), AutomationPoint(frame=20, gain=0.5)]
                ),
                layers=[Layer(id="voice", sound=tone(330.0, seconds=0.5))],
            ),
            Cue(
                id="bed",
                bus="mx",
                frame=12,
                gain_db=-6.0,
                layers=[Layer(id="pad", sound=tone(110.0, seconds=1.0, release_s=0.1))],
            ),
            Cue(
                id="hit",
                bus="fx",
                marker="hit",
                offset_frames=-1,
                layers=[
                    Layer(id="sub", sound=tone(55.0, seconds=0.3)),
                    Layer(
                        id="air",
                        sound=SoundSpec(
                            kind="noise", duration_seconds=0.2, seed=7, provenance=PROV
                        ),
                        gain_db=-12.0,
                        offset_samples=240,
                    ),
                ],
            ),
        ],
    )
    return recipe, edit


# ------------------------------------------------------------------------------- unit


def test_build_lua_routes_layers_to_buses_with_automation(tmp_path: Path) -> None:
    recipe, edit = _film()
    placements = place_cues(recipe, edit)
    lua = build_lua(recipe, placements, tmp_path / "session")
    for bus in ("DX", "MX", "FX"):
        assert f'new_bus("{bus}",' in lua
    assert f'new_bus("MX", {10 ** (-3 / 20)!r})' in lua
    # each layer: its track, its bus, start sample, exact length and region gain
    assert f'new_layer("vo.voice", "DX", {placements["vo"]}, 24000, 1.0, 1.0, ' in lua
    assert f'new_layer("hit.air", "FX", {placements["hit"] + 240}, 9600, ' in lua
    vo_start = recipe.frame_sample(20)
    assert f"{{{{0, 1.0}}, {{{vo_start}, 0.5}}}})" in lua
    assert "ARDOUR.AutoState.Play" in lua
    assert lua.startswith("ARDOUR.config():set_output_auto_connect(0)\n")
    assert "master:input():disconnect_all(nil)" in lua
    assert "tr:output():disconnect_all(nil)" in lua
    assert lua.index("still routed to the master") < lua.index("s:save_state(")
    assert f"Temporal.timepos_t({recipe.duration_samples}))" in lua
    assert lua.rstrip().endswith(f'print("{BUILD_OK}")')


def test_reload_export_lua_checks_routing_and_exports_each_stem(tmp_path: Path) -> None:
    recipe, _ = _film()
    lua = reload_export_lua(recipe, tmp_path / "session", tmp_path / "export")
    assert "load_session(" in lua and "create_session" not in lua
    assert '{"vo.voice", "DX", 1.0, {{0, 1.0}, {40000, 0.5}}},' in lua
    assert '{"bed.pad", "MX", 1.0, {}},' in lua
    assert "bypasses its bus" in lua
    assert ROUTING_OK in lua and AUTOMATION_OK in lua
    assert f'e:set_preset("{EXPORT_PRESET_ID}")' in lua
    assert "mute_control():set_value(o == b and 0 or 1, ND)" in lua
    assert lua.index("ARDOUR.LuaAPI.usleep") < lua.index("e:run_export()")


def test_export_preset_is_deterministic(tmp_path: Path) -> None:
    write_export_preset(tmp_path)
    fmt = (tmp_path / "ardour8" / "export" / "spectracut-wav24.format").read_text()
    preset = (tmp_path / "ardour9" / "export" / "spectracut-wav24.preset").read_text()
    for want in ('value="SF_24"', 'value="D_None"', 'Normalize enabled="false"', "broadcast-info"):
        assert want in fmt
    assert EXPORT_PRESET_ID in preset


def test_lua_str_escapes_and_rejects_control_characters() -> None:
    assert lua_str('a"b\\c') == '"a\\"b\\\\c"'
    with pytest.raises(ValueError, match="control character"):
        lua_str("a\nb")


def _session_with_placeholder(tmp_path: Path, samples: int) -> tuple[Path, dict[str, list[str]]]:
    session = tmp_path / "session"
    audiofiles = session / "interchange" / "mix" / "audiofiles"
    silent = array("f", [0.0]) * samples
    for side in "LR":
        write_wav(audiofiles / f"ph_t%{side}.wav", [silent], 48000, float_format=True)
    (session / "peaks").mkdir(parents=True)
    (session / "peaks" / "ph_t%L.peak").write_bytes(b"x")
    log = f"{SOURCE_LINE}\tt\t1\tph_t%R.wav\n{SOURCE_LINE}\tt\t0\tph_t%L.wav\nnoise\n"
    return session, parse_sources(log)


def test_swap_sources_overwrites_placeholders_and_drops_peaks(tmp_path: Path) -> None:
    session, sources = _session_with_placeholder(tmp_path, 4)
    assert sources == {"t": ["ph_t%L.wav", "ph_t%R.wav"]}
    left, right = array("f", [0.1, 0.2, 0.3, 0.4]), array("f", [-0.1, -0.2, -0.3, -0.4])
    swap_sources(session, sources, {"t": [left, right]}, 48000)
    audiofiles = session / "interchange" / "mix" / "audiofiles"
    assert list(read_wav(audiofiles / "ph_t%R.wav")[1][0]) == list(right)
    assert not list((session / "peaks").iterdir())


def test_swap_sources_rejects_mismatches(tmp_path: Path) -> None:
    session, sources = _session_with_placeholder(tmp_path, 4)
    with pytest.raises(SessionError, match="sound has 5 samples, placeholder 4"):
        swap_sources(session, sources, {"t": [array("f", [0.0] * 5)]}, 48000)
    with pytest.raises(SessionError, match="not mono at 44100"):
        swap_sources(session, sources, {"t": [array("f", [0.0] * 4)]}, 44100)
    with pytest.raises(SessionError, match="no audio for track"):
        swap_sources(session, sources, {}, 48000)
    with pytest.raises(SessionError, match="no placeholder for tracks"):
        swap_sources(
            session, sources, {"t": [array("f", [0.0] * 4)], "u": [array("f", [0.0])]}, 48000
        )


def test_stem_sum_error_db() -> None:
    master = [array("f", [0.5, -0.5, 0.25])]
    exact = {"dx": [array("f", [0.25, -0.25, 0.0])], "mx": [array("f", [0.25, -0.25, 0.25])]}
    assert stem_sum_error_db(master, exact) == -math.inf
    off = {"dx": [array("f", [0.5, -0.5, 0.2])]}
    assert stem_sum_error_db(master, off) == pytest.approx(
        10 * math.log10(0.05**2 / (0.25 + 0.25 + 0.0625))
    )


# -------------------------------------------------------------------------------- e2e


def _runner() -> ArdourRunner:
    labels = os.environ.get("FORGE_DOCKER_LABELS", "").split()
    return ArdourRunner(docker_args=[arg for lab in labels for arg in ("--label", lab)])


def _onset(chans: list[array[float]], threshold: float = 1e-4) -> int:
    for i in range(len(chans[0])):
        if any(abs(c[i]) > threshold for c in chans):
            return i
    raise AssertionError("stem is silent")


@pytest.mark.e2e
@pytest.mark.skipif(os.environ.get("SPECTRACUT_E2E") != "1", reason="set SPECTRACUT_E2E=1")
def test_render_through_ardour(tmp_path: Path) -> None:
    runner = _runner()
    assert runner.available(), "SPECTRACUT_E2E=1 but no ardour8-lua and no spectracut-audio image"
    recipe, edit = _film()
    cache = SoundCache(tmp_path / "sounds")
    result = render_mix(recipe, edit, tmp_path / "work", runner=runner, cache=cache)
    assert len(result.generated) == 4

    # R2: the fresh-process reload found the recipe's routing and automation
    assert ROUTING_OK in result.log and AUTOMATION_OK in result.log

    # R1: master + three stems, equal length, stems sum to the master within -60 dB
    rate, master = read_wav(result.master)
    stems: dict[str, Channels] = {b: read_wav(p)[1] for b, p in result.stems.items()}
    assert rate == recipe.sample_rate and len(master) == 2
    assert len(master[0]) == recipe.duration_samples
    assert all(len(s) == 2 and len(s[0]) == len(master[0]) for s in stems.values())
    err = stem_sum_error_db(master, stems)
    assert err <= -60.0, f"stems sum to the master within {err:.1f} dB"

    # R4: each cue's onset in its stem is within one frame of its event
    one_frame = Fraction(recipe.sample_rate) / recipe.rate.fps
    for bus, cue_id in (("dx", "vo"), ("mx", "bed"), ("fx", "hit")):
        cue = recipe.cue(cue_id)
        event = recipe.frame_sample(event_frame(cue, edit) + cue.offset_frames)
        onset = _onset(stems[bus])
        assert abs(onset - event) < one_frame, (bus, onset, event)

    # R3: a second render after changing one cue regenerates only that cue's sound
    new = tone(70.0, seconds=0.3)
    hit = recipe.cue("hit")
    changed_hit = hit.model_copy(
        update={"layers": [hit.layers[0].model_copy(update={"sound": new}), hit.layers[1]]}
    )
    changed = recipe.model_copy(
        update={"cues": [changed_hit if c.id == "hit" else c for c in recipe.cues]}
    )
    again = render_mix(changed, edit, tmp_path / "work", runner=runner, cache=cache)
    assert again.generated == [sound_key(new)]
    assert (
        stem_sum_error_db(
            read_wav(again.master)[1], {b: read_wav(p)[1] for b, p in again.stems.items()}
        )
        <= -60.0
    )


def test_tone_generator_is_the_default_shape() -> None:
    out = ToneGenerator().generate(tone(100.0, seconds=0.01), 48000)
    assert len(out) == 2 and len(out[0]) == 480
