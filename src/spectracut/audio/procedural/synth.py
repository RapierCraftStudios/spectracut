# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deterministic procedural sound design in pure Python, plus the measurements QA uses.

:class:`ProceduralGenerator` makes the precisely shaped elements of the spike's
procedural source (whooshes, risers, impacts, UI tones, sub drops) from a
:class:`~spectracut.audio.recipe.SoundSpec` alone: the same spec always gives the same
samples, so sounds cache by :func:`~spectracut.audio.recipe.sound_key`. It is the
reference renderer; :class:`~spectracut.audio.procedural.supercollider.SuperColliderGenerator`
renders the same kinds through SuperCollider NRT on render hosts.

Every sound is normalised so its peak equals the ``level`` param (default 0.5).

Kinds and params (all optional):

- ``whoosh``: ``cutoff_hz`` (brightness, default 2000); a sine-squared swell over the
  whole duration of seeded noise through a low-pass that opens with the swell.
- ``riser``: ``peak_s`` (default: duration minus 0.08 s), ``f0``/``f1`` (sweep, default
  200/2000 Hz), ``noise`` (mix, default 0.3); rises to its peak at exactly ``peak_s``,
  then releases to silence by the end.
- ``impact``/``hit``: ``freq`` (sub, default 55 Hz), ``decay_s``.
- ``sub_drop``: ``f0``/``f1`` (default 90/30 Hz).
- ``ui_click``, ``ui_key``, ``ui_scroll``, ``ui_nav``: ``freq``.
"""

from __future__ import annotations

import cmath
import math
import random
from array import array
from collections.abc import Callable, Sequence

from spectracut.audio.recipe import SoundSpec

Channels = list[array[float]]
Signal = list[float]

DEFAULT_TAIL_S = 0.08


def _num(spec: SoundSpec, name: str, default: float) -> float:
    value = spec.params.get(name, default)
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise ValueError(f"sound param {name!r} must be a number")
    return float(value)


def _lowpass(x: Signal, cutoffs: Sequence[float], sr: int, q: float = 0.707) -> Signal:
    """RBJ biquad low-pass with a per-sample cutoff (coefficients updated every 32 samples)."""
    y: Signal = [0.0] * len(x)
    x1 = x2 = y1 = y2 = 0.0
    b0 = b1 = b2 = a1 = a2 = 0.0
    nyq = sr * 0.45
    for i, v in enumerate(x):
        if i % 32 == 0:
            fc = min(max(cutoffs[i], 20.0), nyq)
            w = 2.0 * math.pi * fc / sr
            alpha = math.sin(w) / (2.0 * q)
            cw = math.cos(w)
            a0 = 1.0 + alpha
            b0 = (1.0 - cw) / 2.0 / a0
            b1 = (1.0 - cw) / a0
            b2 = b0
            a1 = -2.0 * cw / a0
            a2 = (1.0 - alpha) / a0
        out = b0 * v + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
        x2, x1, y2, y1 = x1, v, y1, out
        y[i] = out
    return y


def _sweep(n: int, sr: int, f0: float, f1: float, shape: Callable[[float], float]) -> Signal:
    """Sine whose frequency moves exponentially from ``f0`` to ``f1`` along ``shape(t)``."""
    phase = 0.0
    out: Signal = [0.0] * n
    ratio = f1 / f0
    for i in range(n):
        out[i] = math.sin(phase)
        f = f0 * ratio ** shape(i / max(1, n - 1))
        phase += 2.0 * math.pi * f / sr
    return out


def _noise(n: int, rng: random.Random) -> Signal:
    return [rng.uniform(-1.0, 1.0) for _ in range(n)]


def _whoosh(spec: SoundSpec, n: int, sr: int, rng: random.Random) -> Signal:
    cutoff = _num(spec, "cutoff_hz", 2000.0)
    env = [math.sin(math.pi * i / max(1, n - 1)) ** 2 for i in range(n)]
    filtered = _lowpass(_noise(n, rng), [cutoff * (0.35 + 0.65 * e) for e in env], sr)
    return [e * v for e, v in zip(env, filtered, strict=True)]


def _riser(spec: SoundSpec, n: int, sr: int, rng: random.Random) -> Signal:
    duration = n / sr
    peak_s = _num(spec, "peak_s", max(duration - DEFAULT_TAIL_S, duration / 2))
    peak = min(max(1, round(peak_s * sr)), n - 1)
    f0, f1 = _num(spec, "f0", 200.0), _num(spec, "f1", 2000.0)
    mix = _num(spec, "noise", 0.3)
    rise = peak
    tone = _sweep(n, sr, f0, f1, lambda t: min(1.0, t * n / rise))
    noise = _lowpass(_noise(n, rng), [f1 * 2.0] * n, sr)
    release = max(1, n - 1 - peak)
    out: Signal = [0.0] * n
    for i in range(n):
        env = (i / peak) ** 3 if i <= peak else max(0.0, 1.0 - (i - peak) / release) ** 2
        out[i] = env * ((1.0 - mix) * tone[i] + mix * noise[i])
    return out


def _decay(n: int, sr: int, seconds: float) -> Signal:
    k = 1.0 / max(seconds * sr, 1.0)
    return [math.exp(-i * k * 5.0) for i in range(n)]


def _impact(spec: SoundSpec, n: int, sr: int, rng: random.Random) -> Signal:
    freq = _num(spec, "freq", 55.0)
    body = _decay(n, sr, _num(spec, "decay_s", n / sr * 0.8))
    snap = _decay(n, sr, 0.04)
    sub = _sweep(n, sr, freq * 2.0, freq, lambda t: min(1.0, t * 8.0))
    noise = _noise(n, rng)
    return [b * s + 0.6 * c * w for b, s, c, w in zip(body, sub, snap, noise, strict=True)]


def _sub_drop(spec: SoundSpec, n: int, sr: int, rng: random.Random) -> Signal:
    tone = _sweep(n, sr, _num(spec, "f0", 90.0), _num(spec, "f1", 30.0), lambda t: t)
    return [t * e for t, e in zip(tone, _decay(n, sr, n / sr), strict=True)]


def _ui(default_freq: float, decay: float, noise_mix: float) -> Callable[..., Signal]:
    def make(spec: SoundSpec, n: int, sr: int, rng: random.Random) -> Signal:
        freq = _num(spec, "freq", default_freq)
        w = 2.0 * math.pi * freq / sr
        env = _decay(n, sr, min(decay, n / sr))
        return [
            e * ((1.0 - noise_mix) * math.sin(w * i) + noise_mix * rng.uniform(-1.0, 1.0))
            for i, e in enumerate(env)
        ]

    return make


def _ui_scroll(spec: SoundSpec, n: int, sr: int, rng: random.Random) -> Signal:
    env = [math.sin(math.pi * i / max(1, n - 1)) for i in range(n)]
    filtered = _lowpass(_noise(n, rng), [_num(spec, "freq", 3000.0)] * n, sr)
    return [e * v for e, v in zip(env, filtered, strict=True)]


def _ui_nav(spec: SoundSpec, n: int, sr: int, rng: random.Random) -> Signal:
    freq = _num(spec, "freq", 880.0)
    half = n // 2
    env = _decay(n, sr, n / sr / 2)
    out: Signal = []
    for i in range(n):
        f = freq if i < half else freq * 1.5
        out.append(env[i if i < half else i - half] * math.sin(2.0 * math.pi * f * i / sr))
    return out


_KINDS: dict[str, Callable[[SoundSpec, int, int, random.Random], Signal]] = {
    "whoosh": _whoosh,
    "riser": _riser,
    "impact": _impact,
    "hit": _impact,
    "sub_drop": _sub_drop,
    "ui_click": _ui(2200.0, 0.02, 0.2),
    "ui_key": _ui(3200.0, 0.015, 0.5),
    "ui_scroll": _ui_scroll,
    "ui_nav": _ui_nav,
}
PROCEDURAL_KINDS: frozenset[str] = frozenset(_KINDS)


def normalise(signal: Sequence[float], level: float) -> array[float]:
    """``signal`` scaled so its peak magnitude is ``level`` (silence stays silent)."""
    peak = max((abs(v) for v in signal), default=0.0)
    scale = level / peak if peak > 0.0 else 0.0
    return array("f", (v * scale for v in signal))


class ProceduralGenerator:
    """Deterministic :class:`~spectracut.audio.render.SoundGenerator` for procedural kinds."""

    kinds = PROCEDURAL_KINDS

    def generate(self, spec: SoundSpec, sample_rate: int) -> Channels:
        make = _KINDS.get(spec.kind)
        if make is None:
            raise ValueError(f"ProceduralGenerator cannot make sound kind {spec.kind!r}")
        n = spec.samples(sample_rate)
        rng = random.Random(f"{spec.kind}:{spec.seed}")
        mono = normalise(make(spec, n, sample_rate, rng), _num(spec, "level", 0.5))
        return [mono] if spec.channels == 1 else [mono, array("f", mono)]


# ----------------------------------------------------------------------- measurement


def rms_envelope(chan: Sequence[float], window: int) -> list[float]:
    """RMS of consecutive windows of ``window`` samples."""
    out: list[float] = []
    for s in range(0, len(chan), window):
        block = chan[s : s + window]
        out.append(math.sqrt(sum(v * v for v in block) / len(block)))
    return out


def active_span_seconds(
    chan: Sequence[float], sample_rate: int, threshold_db: float = -30.0
) -> float:
    """Seconds from the first to the last 5 ms window within ``threshold_db`` of the loudest."""
    window = max(1, sample_rate // 200)
    env = rms_envelope(chan, window)
    top = max(env, default=0.0)
    if top <= 0.0:
        return 0.0
    floor = top * 10.0 ** (threshold_db / 20.0)
    loud = [i for i, v in enumerate(env) if v >= floor]
    return (loud[-1] - loud[0] + 1) * window / sample_rate


def _fft(x: list[complex]) -> list[complex]:
    n = len(x)
    if n == 1:
        return x
    even, odd = _fft(x[0::2]), _fft(x[1::2])
    tw = [cmath.exp(-2j * math.pi * k / n) * odd[k] for k in range(n // 2)]
    return [even[k] + tw[k] for k in range(n // 2)] + [even[k] - tw[k] for k in range(n // 2)]


def spectral_centroid(chan: Sequence[float], sample_rate: int, size: int = 2048) -> float:
    """Power-weighted mean frequency (Hz) over Hann windows hopping by ``size``."""
    hann = [0.5 - 0.5 * math.cos(2.0 * math.pi * i / (size - 1)) for i in range(size)]
    num = den = 0.0
    for s in range(0, len(chan) - size + 1, size):
        spectrum = _fft([complex(chan[s + i] * hann[i]) for i in range(size)])
        for k in range(1, size // 2):
            power = abs(spectrum[k]) ** 2
            num += power * k * sample_rate / size
            den += power
    return num / den if den > 0.0 else 0.0


def peak_time_seconds(chan: Sequence[float], sample_rate: int, window: int = 64) -> float:
    """Centre time of the loudest ``window``-sample RMS window."""
    env = rms_envelope(chan, window)
    best = max(range(len(env)), key=lambda i: env[i])
    return (best * window + window / 2) / sample_rate
