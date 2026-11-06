# SPDX-License-Identifier: AGPL-3.0-or-later
"""Surge XT rendering of tonal sound kinds, hosted by Ardour.

Surge XT is installed plugins-only (LV2/VST3, docs/engineering/audio-tool-check.md), so it
cannot render on its own. :class:`SurgeXTGenerator` writes a Lua program for
``ardour8-lua`` (the deterministic export path, ``ardour.py``) that opens a throwaway
session on the dummy backend, hosts the Surge XT LV2 instrument on one MIDI track, plays
one note and exports the master bus with the private export preset. Pedalboard is not
used: it is not a dependency and not bit-exact.

Only numbers from the spec reach the program: the note, velocity and length are formatted
as numeric literals and everything else is a fixed template, so no code from a spec or
recipe is ever executed (hosts run declarative recipes only). Surge's default patch is
deterministic, so the spec's ``seed`` does not change the sound.
"""

from __future__ import annotations

import math
import os
import shutil
import subprocess
import tempfile
from array import array
from pathlib import Path

from spectracut.audio.ardour import (
    EXPORT_PRESET_ID,
    SESSION_NAME,
    lua_str,
    write_export_preset,
)
from spectracut.audio.procedural.synth import normalise_peak
from spectracut.audio.recipe import SoundSpec

Channels = list[array[float]]

SURGE_LV2_URI = "urn:surge-synth-team.org:surge-xt"
RENDER_OK = "SPECTRACUT_SURGE_OK"

# kind -> (default MIDI note, default note length as a fraction of the sound's duration)
SURGE_KINDS: dict[str, tuple[float, float]] = {
    "surge_pluck": (60.0, 0.25),
    "surge_pad": (48.0, 0.8),
    "surge_bell": (72.0, 0.15),
}


def _number(spec: SoundSpec, name: str, default: float) -> float:
    value = spec.params.get(name, default)
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise ValueError(f"sound param {name!r} must be a number")
    if not math.isfinite(value):
        raise ValueError("non-finite sound param")
    return float(value)


def build_surge_lua(spec: SoundSpec, sample_rate: int, session_dir: Path, out_dir: Path) -> str:
    """The ``ardour8-lua`` program that renders ``spec`` to ``<out_dir>/<session>.wav``."""
    kind = SURGE_KINDS.get(spec.kind)
    if kind is None:
        raise ValueError(f"SurgeXTGenerator cannot make sound kind {spec.kind!r}")
    default_note, length_fraction = kind
    note = _number(spec, "note", default_note)
    velocity = _number(spec, "velocity", 100.0)
    if not (0 <= note <= 127 and note == int(note)):
        raise ValueError("sound param 'note' must be an integer from 0 to 127")
    if not 1 <= velocity <= 127:
        raise ValueError("sound param 'velocity' must be from 1 to 127")
    n = spec.samples(sample_rate)
    default_len = n * length_fraction
    hold = int(_number(spec, "note_samples", default_len))
    if not 1 <= hold <= n:
        raise ValueError("sound param 'note_samples' must be from 1 to the sound's length")
    lines = [
        "ARDOUR.config():set_output_auto_connect(0)",
        'AudioEngine:set_backend("None (Dummy)", "", "")',
        f"local s = create_session({lua_str(session_dir)}, {lua_str(SESSION_NAME)}, "
        f"{int(sample_rate)})",
        'assert(s, "create_session failed")',
        "local ND = PBD.GroupControlDisposition.NoGroup",
        f'local info = ARDOUR.LuaAPI.new_plugin_info({lua_str(SURGE_LV2_URI)}, "LV2")',
        'assert(info and not info:isnil(), "Surge XT LV2 instrument not found")',
        "local tl = s:new_midi_track(ARDOUR.ChanCount(ARDOUR.DataType('midi'), 1),",
        "  ARDOUR.ChanCount(ARDOUR.DataType('audio'), 2), false, info:plugin_info(), nil, nil, 1,",
        "  'surge', ARDOUR.PresentationInfo.max_order, ARDOUR.TrackMode.Normal, true, false)",
        'assert(tl:size() == 1, "MIDI track not created")',
        "local tr = tl:front():to_track()",
        "tr:output():disconnect_all(nil)",
        "local master = s:master_out()",
        "for c = 0, 1 do",
        "  tr:output():audio(c):connect(master:input():audio(c):name())",
        "end",
        f"local note, vel, hold = {int(note)}, {int(velocity)}, {hold}",
        "local src = ARDOUR.SourceFactory.createWritable(ARDOUR.DataType('midi'), s,",
        "  s:new_midi_source_path('surge'), false)",
        'assert(src and not src:isnil(), "MIDI source not created")',
        "local model = src:to_midisource():model()",
        'local cmd = model:new_note_diff_command("surge note")',
        "cmd:add(ARDOUR.LuaAPI.new_noteptr(1, Temporal.Beats(0),",
        "  Temporal.Beats(hold / (s:sample_rate() * 0.5)), note, vel))",
        "model:apply_command(s, cmd)",
        "local pl = ARDOUR.PropertyList()",
        "pl:add(ARDOUR.Properties.start, 0)",
        f"pl:add(ARDOUR.Properties.length, Temporal.timecnt_t({n}))",
        "pl:add(ARDOUR.Properties.name, 'surge')",
        "local mr = ARDOUR.RegionFactory.create(src, pl, true, nil)",
        'assert(mr and not mr:isnil(), "MIDI region not created")',
        "tr:playlist():add_region(mr, Temporal.timepos_t(0), 1, false, 0, 0, false)",
        "ARDOUR.LuaAPI.usleep(500000)",
        "local e = s:simple_export()",
        f"e:set_folder({lua_str(out_dir)})",
        f'e:set_name("{SESSION_NAME}")',
        f"e:set_range(0, {n})",
        f'assert(e:set_preset("{EXPORT_PRESET_ID}"), "export preset not found")',
        'assert(e:check_outputs(), "master bus has no outputs to export")',
        'assert(e:run_export(), "export failed")',
        "close_session()",
        f'print("{RENDER_OK}")',
    ]
    return "\n".join(lines) + "\n"


class SurgeXTGenerator:
    """:class:`~spectracut.audio.render.SoundGenerator` that renders Surge XT through Ardour."""

    kinds = frozenset(SURGE_KINDS)

    def __init__(self, ardour_lua: str | None = None, timeout_s: float = 180.0) -> None:
        self.ardour_lua = ardour_lua or shutil.which("ardour8-lua")
        self.timeout_s = timeout_s

    def available(self) -> bool:
        return self.ardour_lua is not None

    def generate(self, spec: SoundSpec, sample_rate: int) -> Channels:
        from spectracut.audio.render import read_wav

        if self.ardour_lua is None:
            raise RuntimeError("ardour8-lua is not on PATH; install Ardour on the render host")
        n = spec.samples(sample_rate)
        with tempfile.TemporaryDirectory(prefix="spectracut-surge-") as tmp:
            root = Path(tmp)
            out_dir = root / "out"
            out_dir.mkdir()
            xdg = root / "xdg"
            write_export_preset(xdg)
            script = root / "render.lua"
            script.write_text(
                build_surge_lua(spec, sample_rate, root / "session", out_dir), encoding="utf-8"
            )
            env = {**os.environ, "XDG_CONFIG_HOME": str(xdg), "QT_QPA_PLATFORM": "offscreen"}
            proc = subprocess.run(
                [self.ardour_lua, str(script)],
                cwd=tmp,
                env=env,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                check=False,
            )
            wav = out_dir / f"{SESSION_NAME}.wav"
            if RENDER_OK not in proc.stdout or not wav.is_file():
                raise RuntimeError(
                    f"ardour8-lua did not render {spec.kind!r} (exit {proc.returncode}): "
                    f"{proc.stderr[-2000:] or proc.stdout[-2000:]}"
                )
            rate, chans = read_wav(wav)
        if rate != sample_rate:
            raise RuntimeError(f"Ardour rendered at {rate} Hz, expected {sample_rate}")
        level = _number(spec, "level", 0.5)
        fitted: Channels = []
        for chan in chans[: spec.channels]:
            samples = list(chan[:n]) + [0.0] * max(0, n - len(chan))
            fitted.append(array("f", samples))
        while len(fitted) < spec.channels:
            fitted.append(array("f", fitted[0]))
        peak = max(max((abs(v) for v in c), default=0.0) for c in fitted)
        if peak <= 0.0:
            raise RuntimeError(f"Surge XT rendered silence for {spec.kind!r}")
        if len(fitted) == 1:
            return [normalise_peak(fitted[0], level)]
        scale = level / peak
        return [array("f", (v * scale for v in c)) for c in fitted]
