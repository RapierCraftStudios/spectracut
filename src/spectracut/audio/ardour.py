# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ardour session writer: an :class:`~spectracut.audio.recipe.AudioRecipe` as a real session.

The session is built by Ardour itself, from a generated Lua program run by
``ardour8-lua`` (the path proven by the audio tool check, #48), so the result is a
normal session a person can open in Ardour and keep mixing:

- three stereo buses ``DX``, ``MX`` and ``FX``, each feeding the master;
- one stereo track per cue layer, named ``<cue>.<layer>``, whose output is routed to
  its cue's bus (not the master);
- one region per track at the layer's start sample, with region gain = cue gain + layer
  gain and no default fades;
- the cue's static fader value and fader automation (Play mode) on each of its tracks.

Headless Ardour cannot import audio files (``SourceFactory`` and ``Editor:do_import`` are
GUI-only, docs/engineering/audio-tool-check.md), so each region is bounced as a
placeholder of the layer's exact length from Ardour's bundled ``SinGen``, and
:func:`swap_sources` then overwrites the placeholder audio files with the real sound.

A second program, run in a fresh process (reloading in the same process hangs), loads
the session, asserts its routing and automation match the recipe, and exports the
master and each stem (the other two buses muted) with ``Session:simple_export()`` and a
private export preset (``ardour8-export`` is not deterministic).
"""

from __future__ import annotations

from array import array
from collections.abc import Mapping, Sequence
from pathlib import Path

from spectracut.audio.recipe import BUS_NAMES, AudioRecipe, BusName, Cue, Layer, db_to_gain

SESSION_NAME = "mix"
BUILD_OK = "SPECTRACUT_BUILD_OK"
ROUTING_OK = "SPECTRACUT_ROUTING_OK"
AUTOMATION_OK = "SPECTRACUT_AUTOMATION_OK"
EXPORT_OK = "SPECTRACUT_EXPORT_OK"
SOURCE_LINE = "SPECTRACUT_SOURCE"

EXPORT_FORMAT_ID = "5c1e0d7a-48a0-4b0e-9c3e-2a7d0c0f4801"
EXPORT_PRESET_ID = "5c1e0d7a-48a0-4b0e-9c3e-2a7d0c0f4802"

# The settle time before export, as in the audio tool check.
_SETTLE_US = 500_000


class SessionError(RuntimeError):
    """The written session does not match the recipe, or a placeholder cannot be swapped."""


def bus_route(bus: BusName) -> str:
    """Ardour route name of a bus."""
    return bus.upper()


def track_name(cue: Cue, layer: Layer) -> str:
    """Ardour route name of a cue layer (ids are slugs, so ``.`` keeps names unique)."""
    return f"{cue.id}.{layer.id}"


def layer_starts(recipe: AudioRecipe, placements: Mapping[str, int]) -> dict[str, int]:
    """Start sample of every layer track, from the cue placements."""
    return {
        track_name(cue, layer): placements[cue.id] + layer.offset_samples
        for cue in recipe.cues
        for layer in cue.layers
    }


def lua_str(value: str | Path) -> str:
    """A Lua string literal for ``value``."""
    text = str(value)
    if any(ord(ch) < 32 for ch in text):
        raise ValueError(f"control character in Lua string {text!r}")
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _num(value: float) -> str:
    return repr(float(value))


def _automation(recipe: AudioRecipe, cue: Cue) -> list[tuple[int, float]]:
    if cue.automation is None:
        return []
    return [(recipe.frame_sample(p.frame), p.gain) for p in cue.automation.points]


def build_lua(recipe: AudioRecipe, placements: Mapping[str, int], session_dir: Path) -> str:
    """Lua program that creates, saves and closes the session for ``recipe``."""
    lines = [
        'AudioEngine:set_backend("None (Dummy)", "", "")',
        f"local s = create_session({lua_str(session_dir)}, {lua_str(SESSION_NAME)}, "
        f"{recipe.sample_rate})",
        'assert(s, "create_session failed")',
        "local ND = PBD.GroupControlDisposition.NoGroup",
        "local buses = {}",
        "local function new_bus(name, gain)",
        "  local rl = s:new_audio_route(2, 2, nil, 1, name,",
        "    ARDOUR.PresentationInfo.Flag.AudioBus, ARDOUR.PresentationInfo.max_order)",
        '  assert(rl:size() == 1, "bus " .. name .. " not created")',
        "  local b = rl:front()",
        "  b:amp():gain_control():set_value(gain, ND)",
        "  buses[name] = b",
        "end",
        "local function new_layer(name, bus, start, len, region_gain, fader, auto)",
        "  local tl = s:new_audio_track(2, 2, nil, 1, name, ARDOUR.PresentationInfo.max_order,",
        "    ARDOUR.TrackMode.Normal, true, false)",
        '  assert(tl:size() == 1, "track " .. name .. " not created")',
        "  local tr = tl:front():to_track()",
        "  assert(tr:name() == name, 'track renamed to ' .. tr:name())",
        "  tr:output():disconnect_all(nil)",
        "  for c = 0, 1 do",
        "    tr:output():audio(c):connect(buses[bus]:input():audio(c):name())",
        "  end",
        "  -- placeholder source of the exact length; swapped for the real sound afterwards",
        '  local gen = ARDOUR.LuaAPI.new_luaproc(s, "SinGen")',
        '  assert(not gen:isnil(), "SinGen Lua DSP not found")',
        '  assert(tr:add_processor_by_index(gen, 0, nil, true), "add SinGen failed")',
        "  local r = tr:bounce_range(0, len, ARDOUR.InterThreadInfo(), gen, true,",
        '    "ph_" .. name, false)',
        '  assert(not r:isnil(), "placeholder bounce failed for " .. name)',
        "  tr:remove_processor(gen, nil, true)",
        "  local ar = r:to_audioregion()",
        "  for c = 0, ar:n_channels() - 1 do",
        f'    print("{SOURCE_LINE}\\t" .. name .. "\\t" .. c .. "\\t" .. r:source(c):name())',
        "  end",
        "  tr:playlist():add_region(r, Temporal.timepos_t(start), 1, false, 0, 0, false)",
        "  for reg in tr:playlist():region_list():iter() do",
        "    local pr = reg:to_audioregion()",
        "    pr:set_scale_amplitude(region_gain)",
        "    pr:set_fade_in_active(false)",
        "    pr:set_fade_out_active(false)",
        "  end",
        "  local ac = tr:amp():gain_control()",
        "  ac:set_value(fader, ND)",
        "  if #auto > 0 then",
        "    local al = ac:alist()",
        "    al:clear_list()",
        "    for _, p in ipairs(auto) do",
        "      al:add(Temporal.timepos_t(p[1]), p[2], false, true)",
        "    end",
        "    ac:set_automation_state(ARDOUR.AutoState.Play)",
        "  end",
        "end",
    ]
    for bus in BUS_NAMES:
        gain = db_to_gain(recipe.bus(bus).gain_db)
        lines.append(f"new_bus({lua_str(bus_route(bus))}, {_num(gain)})")
    starts = layer_starts(recipe, placements)
    for cue in recipe.cues:
        auto = ", ".join(f"{{{t}, {_num(g)}}}" for t, g in _automation(recipe, cue))
        for layer in cue.layers:
            name = track_name(cue, layer)
            gain = db_to_gain(cue.gain_db + layer.gain_db)
            lines.append(
                f"new_layer({lua_str(name)}, {lua_str(bus_route(cue.bus))}, {starts[name]}, "
                f"{layer.sound.samples(recipe.sample_rate)}, {_num(gain)}, "
                f"{_num(cue.fader)}, {{{auto}}})"
            )
    lines += [
        "s:set_session_extents(Temporal.timepos_t(0), "
        f"Temporal.timepos_t({recipe.duration_samples}))",
        "s:save_state('', false, false, false, false, false)",
        "close_session()",
        f'print("{BUILD_OK}")',
    ]
    return "\n".join(lines) + "\n"


def reload_export_lua(recipe: AudioRecipe, session_dir: Path, out_dir: Path) -> str:
    """Lua program for a fresh process: reload, check routing and automation, export.

    Exports ``<out_dir>/master/`` and ``<out_dir>/<bus>/`` (one WAV each).
    """
    expected = []
    for cue in recipe.cues:
        auto = ", ".join(f"{{{t}, {_num(g)}}}" for t, g in _automation(recipe, cue))
        for layer in cue.layers:
            expected.append(
                f"  {{{lua_str(track_name(cue, layer))}, {lua_str(bus_route(cue.bus))}, "
                f"{_num(cue.fader)}, {{{auto}}}}},"
            )
    bus_list = ", ".join(lua_str(bus_route(b)) for b in BUS_NAMES)
    lines = [
        'AudioEngine:set_backend("None (Dummy)", "", "")',
        f"local s = load_session({lua_str(session_dir)}, {lua_str(SESSION_NAME)})",
        'assert(s, "load_session failed")',
        "local ND = PBD.GroupControlDisposition.NoGroup",
        f"local bus_names = {{{bus_list}}}",
        "local tracks = {",
        *expected,
        "}",
        "local function near(a, b) return math.abs(a - b) <= 1e-6 * math.max(1, math.abs(b)) end",
        "local n = 0",
        "for _ in s:get_routes():iter() do n = n + 1 end",
        "assert(n == 1 + #bus_names + #tracks,",
        "  'route count ' .. n .. ' after reload, expected ' .. (1 + #bus_names + #tracks))",
        "local master = s:master_out()",
        "for _, b in ipairs(bus_names) do",
        "  local r = s:route_by_name(b)",
        "  assert(not r:isnil(), 'bus ' .. b .. ' missing after reload')",
        "  for c = 0, 1 do",
        "    assert(r:output():audio(c):connected_to(master:input():audio(c):name()),",
        "      'bus ' .. b .. ' not routed to the master')",
        "  end",
        "end",
        "for _, t in ipairs(tracks) do",
        "  local r = s:route_by_name(t[1])",
        "  assert(not r:isnil(), 'track ' .. t[1] .. ' missing after reload')",
        "  local bus = s:route_by_name(t[2])",
        "  for c = 0, 1 do",
        "    local p = r:output():audio(c)",
        "    assert(p:connected_to(bus:input():audio(c):name()),",
        "      'track ' .. t[1] .. ' not routed to ' .. t[2])",
        "    assert(not p:connected_to(master:input():audio(c):name()),",
        "      'track ' .. t[1] .. ' bypasses its bus')",
        "  end",
        "end",
        f'print("{ROUTING_OK}")',
        "for _, t in ipairs(tracks) do",
        "  local ac = s:route_by_name(t[1]):amp():gain_control()",
        "  local want = t[4]",
        "  if #want == 0 then",
        "    assert(near(ac:get_value(), t[3]), 'fader of ' .. t[1] .. ' changed')",
        "  else",
        "    assert(ac:automation_state() == ARDOUR.AutoState.Play,",
        "      'automation of ' .. t[1] .. ' not in Play mode')",
        "    local i = 0",
        "    for ev in ac:alist():events():iter() do",
        "      i = i + 1",
        "      local w = want[i]",
        "      assert(w and ev.when:samples() == w[1] and near(ev.value, w[2]),",
        "        'automation point ' .. i .. ' of ' .. t[1] .. ' differs')",
        "    end",
        "    assert(i == #want, 'automation of ' .. t[1] .. ' has ' .. i .. ' points')",
        "  end",
        "end",
        f'print("{AUTOMATION_OK}")',
        f"ARDOUR.LuaAPI.usleep({_SETTLE_US})",
        "local function export(dir)",
        "  local e = s:simple_export()",
        "  e:set_folder(dir)",
        f'  e:set_name("{SESSION_NAME}")',
        f"  e:set_range(0, {recipe.duration_samples})",
        f'  assert(e:set_preset("{EXPORT_PRESET_ID}"), "export preset not found")',
        '  assert(e:check_outputs(), "master bus has no outputs to export")',
        '  assert(e:run_export(), "export failed: " .. dir)',
        "end",
        f"export({lua_str(out_dir / 'master')})",
        "for _, b in ipairs(bus_names) do",
        "  for _, o in ipairs(bus_names) do",
        "    s:route_by_name(o):mute_control():set_value(o == b and 0 or 1, ND)",
        "  end",
        f"  export({lua_str(out_dir)} .. '/' .. string.lower(b))",
        "end",
        "for _, o in ipairs(bus_names) do s:route_by_name(o):mute_control():set_value(0, ND) end",
        "close_session()",
        f'print("{EXPORT_OK}")',
    ]
    return "\n".join(lines) + "\n"


def parse_sources(log: str) -> dict[str, list[str]]:
    """Placeholder source file names per track, from the build program's output."""
    sources: dict[str, dict[int, str]] = {}
    for line in log.splitlines():
        parts = line.split("\t")
        if len(parts) == 4 and parts[0] == SOURCE_LINE:
            sources.setdefault(parts[1], {})[int(parts[2])] = parts[3]
    return {name: [chans[c] for c in sorted(chans)] for name, chans in sources.items()}


def swap_sources(
    session_dir: Path,
    sources: Mapping[str, Sequence[str]],
    audio: Mapping[str, Sequence[array[float]]],
    sample_rate: int,
) -> None:
    """Overwrite each track's placeholder audio files with its real sound.

    ``audio`` maps a track name to its channels (mono is spread to both channels). The
    replacement must have the placeholder's exact length; peak files are removed so
    Ardour rebuilds them.
    """
    from spectracut.audio.render import read_wav, write_wav

    audiofiles = sorted(session_dir.glob("interchange/*/audiofiles"))
    if len(audiofiles) != 1:
        raise SessionError(f"expected one interchange audiofiles dir in {session_dir}")
    for name, files in sources.items():
        if name not in audio:
            raise SessionError(f"no audio for track {name!r}")
        channels = list(audio[name])
        if len(channels) == 1:
            channels = channels * len(files)
        if len(channels) != len(files):
            raise SessionError(f"track {name!r}: {len(channels)} channels for {len(files)} sources")
        for file, samples in zip(files, channels, strict=True):
            path = audiofiles[0] / file
            if not path.is_file():
                raise SessionError(f"placeholder {path} missing")
            rate, placeholder = read_wav(path)
            if rate != sample_rate or len(placeholder) != 1:
                raise SessionError(f"placeholder {path} is not mono at {sample_rate} Hz")
            if len(placeholder[0]) != len(samples):
                raise SessionError(
                    f"track {name!r}: sound has {len(samples)} samples, "
                    f"placeholder {len(placeholder[0])}"
                )
            write_wav(path, [samples], sample_rate, float_format=True)
    missing = set(audio) - set(sources)
    if missing:
        raise SessionError(f"no placeholder for tracks {sorted(missing)}")
    for peak in (session_dir / "peaks").glob("*"):
        if peak.is_file():
            peak.unlink()


_FORMAT = f"""<?xml version="1.0" encoding="UTF-8"?>
<ExportFormatSpecification name="SpectraCut WAV 24" id="{EXPORT_FORMAT_ID}" analyse="0">
  <Encoding id="F_WAV" type="T_Sndfile" extension="wav" name="WAV" has-sample-format="true" \
channel-limit="256"/>
  <SampleRate rate="SR_Session"/>
  <SRCQuality quality="SRC_SincBest"/>
  <EncodingOptions>
    <Option name="sample-format" value="SF_24"/>
    <Option name="dithering" value="D_None"/>
    <Option name="tag-metadata" value="false"/>
    <Option name="tag-support" value="false"/>
    <Option name="broadcast-info" value="false"/>
  </EncodingOptions>
  <Processing>
    <Normalize enabled="false" loudness="no" dbfs="0" lufs="-23" dbtp="-1"/>
    <Silence>
      <Start><Trim enabled="false"/><Add enabled="false"><Duration format="Timecode" hours="0" \
minutes="0" seconds="0" frames="0"/></Add></Start>
      <End><Trim enabled="false"/><Add enabled="false"><Duration format="Timecode" hours="0" \
minutes="0" seconds="0" frames="0"/></Add></End>
    </Silence>
  </Processing>
</ExportFormatSpecification>
"""

_PRESET = f"""<?xml version="1.0" encoding="UTF-8"?>
<ExportPreset id="{EXPORT_PRESET_ID}" name="SpectraCut WAV 24">
  <ExportFormat id="{EXPORT_FORMAT_ID}"/>
  <ExportFilename>
    <Folder relative="1" path="export"/>
    <Field name="label" enabled="1"/>
    <Field name="session" enabled="0"/>
    <Field name="snapshot" enabled="0"/>
    <Field name="timespan" enabled="0"/>
    <Field name="revision" enabled="0"/>
    <Field name="time" enabled="0" value="T_None"/>
    <Field name="date" enabled="0" value="D_None"/>
  </ExportFilename>
</ExportPreset>
"""


def write_export_preset(xdg_config_home: Path) -> None:
    """Write the deterministic export preset (WAV int24, session rate, no dither, no
    normalise, no BWF, no tags) where ``ardour8-lua`` and ``ardour9-lua`` read it."""
    for version in ("ardour8", "ardour9"):
        folder = xdg_config_home / version / "export"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "spectracut-wav24.format").write_text(_FORMAT, encoding="utf-8")
        (folder / "spectracut-wav24.preset").write_text(_PRESET, encoding="utf-8")
