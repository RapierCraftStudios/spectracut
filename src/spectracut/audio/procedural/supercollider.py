# SPDX-License-Identifier: AGPL-3.0-or-later
"""SuperCollider non-real-time rendering of the procedural sound kinds.

:class:`SuperColliderGenerator` writes an ``.scd`` file that builds a ``Score`` with one
SynthDef for the spec's kind and renders it with ``Score.recordNRT`` (scsynth NRT, no audio
device). Randomness is seeded with ``RandSeed`` from the spec's ``seed``, so the render is
deterministic (docs/engineering/audio-tool-check.md, SuperCollider R6).

Only numbers from the spec reach the script: every param is formatted as a numeric
literal and the SynthDef bodies are fixed templates, so no code from a spec or recipe is
ever executed (hosts run declarative recipes only).

``sclang`` needs ``QT_QPA_PLATFORM=offscreen`` and must not run as root (its QtWebEngine
aborts); run it as the render host's non-root user.
"""

from __future__ import annotations

import math
import os
import shutil
import subprocess
import tempfile
from array import array
from pathlib import Path

from spectracut.audio.procedural.synth import DEFAULT_TAIL_S, PROCEDURAL_KINDS, normalise_peak
from spectracut.audio.recipe import SoundSpec

Channels = list[array[float]]

# Fixed SynthDef bodies; ``{name}`` placeholders are filled with numeric literals only.
_BODIES: dict[str, str] = {
    "whoosh": (
        "var env = EnvGen.ar(Env.sine({dur})).squared;"
        " LPF.ar(WhiteNoise.ar, {cutoff_hz} * (0.35 + (0.65 * env))) * env"
    ),
    "riser": (
        "var env = EnvGen.ar(Env([0, 1, 0], [{peak_s}, {tail}], [3, -2]));"
        " var tone = SinOsc.ar(XLine.ar({f0}, {f1}, {peak_s}));"
        " var air = LPF.ar(WhiteNoise.ar, {f1} * 2);"
        " ((1 - {noise}) * tone + ({noise} * air)) * env"
    ),
    "impact": (
        "var body = EnvGen.ar(Env.perc(0.001, {decay_s}));"
        " var snap = EnvGen.ar(Env.perc(0.0005, 0.04));"
        " (SinOsc.ar(XLine.ar({freq} * 2, {freq}, {dur} / 8)) * body)"
        " + (0.6 * WhiteNoise.ar * snap)"
    ),
    "sub_drop": ("SinOsc.ar(Line.ar({f0}, {f1}, {dur})) * EnvGen.ar(Env.perc(0.001, {dur}))"),
    "ui_click": "SinOsc.ar({freq}) * EnvGen.ar(Env.perc(0.0005, 0.02))",
    "ui_key": (
        "((0.5 * SinOsc.ar({freq})) + (0.5 * WhiteNoise.ar)) * EnvGen.ar(Env.perc(0.0005, 0.015))"
    ),
    "ui_scroll": "LPF.ar(WhiteNoise.ar, {freq}) * EnvGen.ar(Env.sine({dur}))",
    "ui_nav": (
        "SinOsc.ar(Select.kr(Line.kr(0, 1.999, {dur}), [{freq}, {freq} * 1.5]))"
        " * EnvGen.ar(Env.perc(0.001, {dur} / 2))"
    ),
}
_BODIES["hit"] = _BODIES["impact"]

_DEFAULTS: dict[str, float] = {
    "cutoff_hz": 2000.0,
    "f0": 200.0,
    "f1": 2000.0,
    "noise": 0.3,
    "freq": 55.0,
}
_FREQ_DEFAULTS = {"ui_click": 2200.0, "ui_key": 3200.0, "ui_scroll": 3000.0, "ui_nav": 880.0}


def _lit(value: float) -> str:
    if not math.isfinite(value):
        raise ValueError("non-finite sound param")
    return repr(float(value))


def _number(spec: SoundSpec, name: str, default: float) -> float:
    value = spec.params.get(name, default)
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise ValueError(f"sound param {name!r} must be a number")
    return float(value)


def build_score(spec: SoundSpec, sample_rate: int, out_path: Path) -> str:
    """The sclang program that renders ``spec`` to ``out_path`` (32-bit float WAV)."""
    body = _BODIES.get(spec.kind)
    if body is None:
        raise ValueError(f"SuperColliderGenerator cannot make sound kind {spec.kind!r}")
    dur = spec.samples(sample_rate) / sample_rate
    values = {
        "dur": dur,
        "tail": DEFAULT_TAIL_S,
        "decay_s": _number(spec, "decay_s", dur * 0.8),
        "peak_s": _number(spec, "peak_s", max(dur - DEFAULT_TAIL_S, dur / 2)),
    }
    for name, default in _DEFAULTS.items():
        if name == "f0" and spec.kind == "sub_drop":
            default = 90.0
        if name == "f1" and spec.kind == "sub_drop":
            default = 30.0
        if name == "freq":
            default = _FREQ_DEFAULTS.get(spec.kind, default)
        values[name] = _number(spec, name, default)
    values["tail"] = max(1.0 / sample_rate, dur - values["peak_s"])
    filled = body.format(**{k: _lit(v) for k, v in values.items()})
    path = str(out_path).replace("\\", "\\\\").replace('"', '\\"')
    return f"""(
var def = SynthDef(\\spectracut_sound, {{ |out = 0|
    RandSeed.ir(1, {int(spec.seed)});
    Out.ar(out, ({{ {filled} }}.value) ! {spec.channels});
}});
var score = Score([
    [0.0, ['/d_recv', def.asBytes]],
    [0.0, Synth.basicNew(\\spectracut_sound, Server.default, 1000).newMsg],
    [{_lit(dur)}, [\\c_set, 0, 0]]
]);
var options = ServerOptions.new
    .numOutputBusChannels_({spec.channels})
    .numInputBusChannels_(0)
    .sampleRate_({int(sample_rate)});
score.recordNRT(
    outputFilePath: "{path}",
    headerFormat: "WAV",
    sampleFormat: "float",
    options: options,
    duration: {_lit(dur)},
    action: {{ 0.exit }}
);
)
"""


class SuperColliderGenerator:
    """:class:`~spectracut.audio.render.SoundGenerator` that renders through scsynth NRT."""

    kinds = PROCEDURAL_KINDS

    def __init__(self, sclang: str | None = None, timeout_s: float = 120.0) -> None:
        self.sclang = sclang or shutil.which("sclang")
        self.timeout_s = timeout_s

    def available(self) -> bool:
        return self.sclang is not None

    def generate(self, spec: SoundSpec, sample_rate: int) -> Channels:
        from spectracut.audio.render import read_wav

        if self.sclang is None:
            raise RuntimeError("sclang is not on PATH; install SuperCollider on the render host")
        n = spec.samples(sample_rate)
        with tempfile.TemporaryDirectory(prefix="spectracut-sc-") as tmp:
            out = Path(tmp) / "sound.wav"
            script = Path(tmp) / "score.scd"
            script.write_text(build_score(spec, sample_rate, out), encoding="utf-8")
            env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
            proc = subprocess.run(
                [self.sclang, str(script)],
                cwd=tmp,
                env=env,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                check=False,
            )
            if not out.is_file():
                raise RuntimeError(
                    f"sclang did not render {spec.kind!r} (exit {proc.returncode}): "
                    f"{proc.stderr[-2000:] or proc.stdout[-2000:]}"
                )
            rate, chans = read_wav(out)
        if rate != sample_rate:
            raise RuntimeError(f"scsynth rendered at {rate} Hz, expected {sample_rate}")
        level = _number(spec, "level", 0.5)
        fitted: Channels = []
        for chan in chans[: spec.channels]:
            samples = list(chan[:n]) + [0.0] * max(0, n - len(chan))
            fitted.append(array("f", samples))
        while len(fitted) < spec.channels:
            fitted.append(array("f", fitted[0]))
        peak = max(max((abs(v) for v in c), default=0.0) for c in fitted)
        scale = level / peak if peak > 0.0 else 0.0
        if len(fitted) == 1:
            return [normalise_peak(fitted[0], level)]
        return [array("f", (v * scale for v in c)) for c in fitted]
