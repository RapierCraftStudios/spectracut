# SPDX-License-Identifier: AGPL-3.0-or-later
"""Loudness and true peak of a PCM WAV master against a delivery spec.

ITU-R BS.1770-4 in pure Python: K-weighting (a high shelf and a high pass
biquad), 400 ms blocks with 75% overlap, a -70 LUFS absolute gate and a -10 LU
relative gate; true peak by 4x oversampling through a polyphase windowed-sinc
FIR. :func:`check_mix` adds the metric checks of the spike's audio QA (clipping,
phase and mono, VO intelligibility, sync, cut-offs and silence, sample repetition)
and the learned predictors of :mod:`spectracut.qa.audio_predictors`, which flag
weak audio but never fail a mix. All of it is pure Python.
"""

from __future__ import annotations

import math
import wave
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import Field

from spectracut.qa.audio_predictors import AudioPredictor, check_predictors
from spectracut.qa.common import QACheck
from spectracut.spec._base import StrictModel

ABSOLUTE_GATE = -70.0
RELATIVE_GATE = -10.0
BLOCK_S = 0.4
STEP_S = 0.1
OVERSAMPLE = 4
TAPS_PER_PHASE = 12
CLIP_LEVEL = 0.9995
MAX_CLIP_RUN = 2
MIN_PHASE_CORRELATION = -0.2
MAX_MONO_DROP_DB = 4.5
SPEECH_BAND = (300.0, 3400.0)
VO_WINDOW_S = 0.1
VO_ACTIVE_POWER = 1e-5
MIN_VO_MARGIN_DB = 6.0
MAX_MASKED_FRACTION = 0.1
TAIL_S = 0.005
MAX_TAIL_RMS = 0.03
SILENCE_RMS = 0.001
MAX_SILENCE_S = 3.0
SYNC_SEARCH_S = 0.25
MIN_SAMPLE_GAP_S = 2.0
SURROUND_WEIGHTS = (1.0, 1.0, 1.0, 0.0, 1.41, 1.41)
"""BS.1770 channel weights for 5.1 (L, R, C, LFE, Ls, Rs); LFE is excluded."""


class AudioError(ValueError):
    """The file is not a 16- or 24-bit PCM WAV."""


class DeliverySpec(StrictModel):
    """A loudness target with its tolerance and true-peak ceiling."""

    name: str
    target_lufs: float
    tolerance_lu: float = Field(gt=0.0)
    max_true_peak_dbtp: float


DELIVERY_SPECS: dict[str, DeliverySpec] = {
    "streaming": DeliverySpec(
        name="streaming", target_lufs=-14.0, tolerance_lu=0.5, max_true_peak_dbtp=-1.0
    ),
    "social": DeliverySpec(
        name="social", target_lufs=-14.0, tolerance_lu=0.5, max_true_peak_dbtp=-1.0
    ),
    "ebu_r128": DeliverySpec(
        name="ebu_r128", target_lufs=-23.0, tolerance_lu=0.5, max_true_peak_dbtp=-1.0
    ),
    "atsc_a85": DeliverySpec(
        name="atsc_a85", target_lufs=-24.0, tolerance_lu=2.0, max_true_peak_dbtp=-2.0
    ),
}


def read_wav(path: Path) -> tuple[int, list[list[float]]]:
    """``(sample rate, channels)`` of a 16- or 24-bit PCM WAV, samples in -1..1."""
    try:
        with wave.open(str(path), "rb") as wav:
            rate, nch, width = wav.getframerate(), wav.getnchannels(), wav.getsampwidth()
            raw = wav.readframes(wav.getnframes())
    except (wave.Error, EOFError) as exc:
        raise AudioError(f"{path}: {exc}") from None
    if width not in (2, 3):
        raise AudioError(f"{path}: {8 * width}-bit audio is not supported")
    scale = float(1 << (8 * width - 1))
    count = len(raw) // width
    samples = [
        int.from_bytes(raw[i * width : (i + 1) * width], "little", signed=True) / scale
        for i in range(count)
    ]
    return rate, [samples[c::nch] for c in range(nch)]


def _biquad(
    x: Sequence[float], b: tuple[float, float, float], a: tuple[float, float]
) -> list[float]:
    b0, b1, b2 = b
    a1, a2 = a
    x1 = x2 = y1 = y2 = 0.0
    out: list[float] = []
    for v in x:
        y = b0 * v + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
        x2, x1, y2, y1 = x1, v, y1, y
        out.append(y)
    return out


def k_weighting(rate: int) -> list[tuple[tuple[float, float, float], tuple[float, float]]]:
    """The two K-weighting biquads ``(b, a)`` at ``rate`` (exact BS.1770 values at 48 kHz)."""
    f0, gain, q = 1681.974450955533, 3.999843853973347, 0.7071752369554196
    k = math.tan(math.pi * f0 / rate)
    vh = 10.0 ** (gain / 20.0)
    vb = vh**0.4996667741545416
    a0 = 1.0 + k / q + k * k
    shelf = (
        ((vh + vb * k / q + k * k) / a0, 2.0 * (k * k - vh) / a0, (vh - vb * k / q + k * k) / a0),
        (2.0 * (k * k - 1.0) / a0, (1.0 - k / q + k * k) / a0),
    )
    f0, q = 38.13547087602444, 0.5003270373238773
    k = math.tan(math.pi * f0 / rate)
    a0 = 1.0 + k / q + k * k
    highpass = ((1.0, -2.0, 1.0), (2.0 * (k * k - 1.0) / a0, (1.0 - k / q + k * k) / a0))
    return [shelf, highpass]


def integrated_loudness(rate: int, channels: Sequence[Sequence[float]]) -> float:
    """BS.1770-4 gated integrated loudness in LUFS; ``-inf`` for silence or under 400 ms."""
    filters = k_weighting(rate)
    weighted = []
    for ch in channels:
        y: Sequence[float] = ch
        for b, a in filters:
            y = _biquad(y, b, a)
        weighted.append(y)
    weights = SURROUND_WEIGHTS if len(channels) >= 6 else (1.0,) * len(channels)
    block, step = round(BLOCK_S * rate), round(STEP_S * rate)
    n = min((len(c) for c in weighted), default=0)
    powers: list[float] = []
    for start in range(0, n - block + 1, step):
        z = 0.0
        for g, ch in zip(weights, weighted, strict=False):
            if g:
                z += g * sum(v * v for v in ch[start : start + block]) / block
        powers.append(z)

    def lufs(power: float) -> float:
        return -0.691 + 10.0 * math.log10(power) if power > 0 else -math.inf

    gated = [p for p in powers if lufs(p) > ABSOLUTE_GATE]
    if not gated:
        return -math.inf
    relative = lufs(sum(gated) / len(gated)) + RELATIVE_GATE
    gated = [p for p in gated if lufs(p) > relative]
    return lufs(sum(gated) / len(gated)) if gated else -math.inf


def _polyphase() -> list[list[float]]:
    taps = OVERSAMPLE * TAPS_PER_PHASE
    centre = (taps - 1) / 2.0
    h = []
    for i in range(taps):
        t = (i - centre) / OVERSAMPLE
        sinc = 1.0 if t == 0 else math.sin(math.pi * t) / (math.pi * t)
        window = 0.5 - 0.5 * math.cos(2.0 * math.pi * (i + 0.5) / taps)
        h.append(sinc * window)
    phases = [h[p::OVERSAMPLE] for p in range(OVERSAMPLE)]
    return [[c / sum(ph) for c in ph] for ph in phases]


def true_peak(channels: Sequence[Sequence[float]]) -> float:
    """Peak of the 4x oversampled signal in dBTP; ``-inf`` for silence."""
    phases = _polyphase()
    peak = 0.0
    for ch in channels:
        peak = max(peak, max((abs(v) for v in ch), default=0.0))
        for coeffs in phases:
            m = len(coeffs)
            for n in range(m - 1, len(ch)):
                acc = 0.0
                for k, c in enumerate(coeffs):
                    acc += c * ch[n - k]
                peak = max(peak, abs(acc))
    return 20.0 * math.log10(peak) if peak > 0 else -math.inf


def _loudness_rows(
    rate: int, channels: Sequence[Sequence[float]], delivery: DeliverySpec
) -> list[QACheck]:
    loudness = integrated_loudness(rate, channels)
    peak = true_peak(channels)
    loud_ok = (
        math.isfinite(loudness) and abs(loudness - delivery.target_lufs) <= delivery.tolerance_lu
    )
    peak_ok = peak <= delivery.max_true_peak_dbtp
    return [
        QACheck(
            name="audio.loudness",
            status="pass" if loud_ok else "fail",
            value=round(loudness, 2) if math.isfinite(loudness) else None,
            limit=delivery.target_lufs,
            detail=f"{delivery.name}: {delivery.target_lufs} LUFS ±{delivery.tolerance_lu} LU",
        ),
        QACheck(
            name="audio.true_peak",
            status="pass" if peak_ok else "fail",
            value=round(peak, 2) if math.isfinite(peak) else None,
            limit=delivery.max_true_peak_dbtp,
            detail=f"{delivery.name}: <= {delivery.max_true_peak_dbtp} dBTP",
        ),
    ]


def check_audio(path: Path, spec: DeliverySpec | str) -> list[QACheck]:
    """Loudness within the spec's tolerance and true peak at or under its ceiling."""
    delivery = DELIVERY_SPECS[spec] if isinstance(spec, str) else spec
    try:
        rate, channels = read_wav(path)
    except (AudioError, OSError) as exc:
        return [QACheck(name="audio.read", status="fail", detail=str(exc))]
    return _loudness_rows(rate, channels, delivery)


@dataclass(frozen=True)
class CueTiming:
    """A sound cue: where it must land (seconds) and the sample it plays, if known."""

    id: str
    time_s: float
    sample: str | None = None


def _row(name: str, ok: bool, value: float | None, limit: float, detail: str) -> QACheck:
    return QACheck(
        name=name, status="pass" if ok else "fail", value=value, limit=limit, detail=detail
    )


def _skip(name: str, why: str) -> QACheck:
    return QACheck(name=name, status="skipped", detail=why)


def _db(ratio: float) -> float:
    return 10.0 * math.log10(ratio) if ratio > 0 else -math.inf


def _finite(v: float) -> float | None:
    return round(v, 2) if math.isfinite(v) else None


def _mono(channels: Sequence[Sequence[float]]) -> list[float]:
    return [sum(f) / len(channels) for f in zip(*channels, strict=False)]


def check_clipping(channels: Sequence[Sequence[float]]) -> QACheck:
    """No run of more than ``MAX_CLIP_RUN`` consecutive full-scale samples on any channel."""
    longest = 0
    for ch in channels:
        run = 0
        for v in ch:
            run = run + 1 if abs(v) >= CLIP_LEVEL else 0
            longest = max(longest, run)
    return _row(
        "audio.clipping",
        longest <= MAX_CLIP_RUN,
        float(longest),
        MAX_CLIP_RUN,
        "longest run of full-scale samples",
    )


def check_phase(channels: Sequence[Sequence[float]]) -> list[QACheck]:
    """Left/right correlation and the loudness lost when the mix is folded to mono."""
    if len(channels) < 2:
        return [_skip("audio.phase_correlation", "mono"), _skip("audio.mono_compat", "mono")]
    left, right = channels[0], channels[1]
    n = min(len(left), len(right))
    pl = sum(v * v for v in left[:n])
    pr = sum(v * v for v in right[:n])
    cross = sum(a * b for a, b in zip(left, right, strict=False))
    if pl == 0.0 or pr == 0.0:
        corr = 1.0
    else:
        corr = cross / math.sqrt(pl * pr)
    fold = (pl + pr + 2.0 * cross) / 4.0
    drop = _db(((pl + pr) / 2.0) / fold) if fold > 0 else (math.inf if pl + pr > 0 else 0.0)
    return [
        _row(
            "audio.phase_correlation",
            corr >= MIN_PHASE_CORRELATION,
            round(corr, 3),
            MIN_PHASE_CORRELATION,
            "left/right correlation",
        ),
        _row(
            "audio.mono_compat",
            drop <= MAX_MONO_DROP_DB,
            _finite(drop),
            MAX_MONO_DROP_DB,
            "level lost folding to mono (dB)",
        ),
    ]


def _bandpass(x: Sequence[float], rate: int) -> list[float]:
    """The speech band (300-3400 Hz): a second-order high pass then low pass."""
    out: Sequence[float] = x
    for f0, high in ((SPEECH_BAND[0], True), (SPEECH_BAND[1], False)):
        w = 2.0 * math.pi * f0 / rate
        alpha = math.sin(w) / (2.0 * 0.7071)
        c = math.cos(w)
        a0 = 1.0 + alpha
        edge = (1.0 + c) / 2.0 if high else (1.0 - c) / 2.0
        b = (edge / a0, (-2.0 * edge if high else 2.0 * edge) / a0, edge / a0)
        out = _biquad(out, b, (-2.0 * c / a0, (1.0 - alpha) / a0))
    return list(out)


def _window_power(x: Sequence[float], size: int) -> list[float]:
    return [sum(v * v for v in x[i : i + size]) / size for i in range(0, len(x) - size + 1, size)]


def check_vo_intelligibility(stems: Mapping[str, Path]) -> QACheck:
    """In the speech band, music and effects stay ``MIN_VO_MARGIN_DB`` below active VO."""
    name = "audio.vo_intelligibility"
    if "dx" not in stems or not ({"mx", "fx"} & set(stems)):
        return _skip(name, "needs the dx stem and a music or effects stem")
    try:
        loaded = {k: read_wav(p) for k, p in stems.items() if k in ("dx", "mx", "fx")}
    except (AudioError, OSError) as exc:
        return QACheck(name=name, status="fail", detail=str(exc))
    rate = loaded["dx"][0]
    size = round(VO_WINDOW_S * rate)
    power = {k: _window_power(_bandpass(_mono(ch), r), size) for k, (r, ch) in loaded.items()}
    active = [i for i, p in enumerate(power["dx"]) if p > VO_ACTIVE_POWER]
    if not active:
        return _skip(name, "no active VO")
    masked = 0
    for i in active:
        bed = sum(p[i] for k, p in power.items() if k != "dx" and i < len(p))
        if bed > 0 and _db(power["dx"][i] / bed) < MIN_VO_MARGIN_DB:
            masked += 1
    frac = masked / len(active)
    return _row(
        name,
        frac <= MAX_MASKED_FRACTION,
        round(frac, 3),
        MAX_MASKED_FRACTION,
        f"fraction of VO windows with music/effects within {MIN_VO_MARGIN_DB} dB of VO",
    )


def check_cutoff(rate: int, channels: Sequence[Sequence[float]]) -> list[QACheck]:
    """The mix ends faded, not cut, and has no long silence inside it."""
    n = min((len(c) for c in channels), default=0)
    tail = max(1, round(TAIL_S * rate))
    rms = max(
        (math.sqrt(sum(v * v for v in c[n - tail : n]) / tail) for c in channels if n >= tail),
        default=0.0,
    )
    size = max(1, round(0.01 * rate))
    quiet = [p < SILENCE_RMS**2 for p in _window_power(_mono_abs(channels, n), size)]
    while quiet and quiet[-1]:
        quiet.pop()
    start = 0
    while start < len(quiet) and quiet[start]:
        start += 1
    longest = run = 0
    for q in quiet[start:]:
        run = run + 1 if q else 0
        longest = max(longest, run)
    seconds = longest * size / rate
    return [
        _row(
            "audio.cutoff",
            rms <= MAX_TAIL_RMS,
            round(rms, 4),
            MAX_TAIL_RMS,
            "level of the last 5 ms",
        ),
        _row(
            "audio.silence",
            seconds <= MAX_SILENCE_S,
            round(seconds, 2),
            MAX_SILENCE_S,
            "longest silence inside the mix (s)",
        ),
    ]


def _mono_abs(channels: Sequence[Sequence[float]], n: int) -> list[float]:
    """Per-sample loudest channel (sign dropped), so out-of-phase channels never cancel."""
    return [max(abs(c[i]) for c in channels) for i in range(n)]


def check_sync(
    rate: int, channels: Sequence[Sequence[float]], cues: Sequence[CueTiming], fps: float
) -> QACheck:
    """Every cue's onset lands within one frame of its time."""
    name = "audio.sync"
    if not cues:
        return _skip(name, "no cues")
    hop = max(1, round(0.001 * rate))
    env = [math.sqrt(p) for p in _window_power(_mono_abs(channels, min(map(len, channels))), hop)]
    worst, bad = 0.0, []
    for cue in cues:
        lo = max(0, round((cue.time_s - SYNC_SEARCH_S) * 1000))
        hi = min(len(env), round((cue.time_s + SYNC_SEARCH_S) * 1000))
        peak = max(env[lo:hi], default=0.0)
        if peak < SILENCE_RMS:
            bad.append(f"{cue.id}: no onset")
            worst = math.inf
            continue
        onset = next(i for i in range(lo, hi) if env[i] >= 0.1 * peak)
        err = abs(onset / 1000.0 - cue.time_s) * fps
        worst = max(worst, err)
        if err > 1.0:
            bad.append(f"{cue.id}: {err:.1f} frames off")
    return _row(name, not bad, _finite(worst), 1.0, "; ".join(bad) or "max onset error (frames)")


def check_sample_reuse(cues: Sequence[CueTiming]) -> QACheck:
    """The same sample is not reused within ``MIN_SAMPLE_GAP_S`` seconds."""
    name = "audio.repetition"
    if not any(c.sample for c in cues):
        return _skip(name, "no sample keys")
    last: dict[str, float] = {}
    bad: list[str] = []
    for cue in sorted(cues, key=lambda c: c.time_s):
        if cue.sample is None:
            continue
        if cue.sample in last and cue.time_s - last[cue.sample] < MIN_SAMPLE_GAP_S:
            bad.append(f"{cue.sample} at {cue.time_s:g}s")
        last[cue.sample] = cue.time_s
    return _row(name, not bad, float(len(bad)), 0.0, "; ".join(bad) or "no close reuse")


def check_mix(
    path: Path,
    spec: DeliverySpec | str,
    *,
    stems: Mapping[str, Path] | None = None,
    cues: Sequence[CueTiming] = (),
    fps: float = 24.0,
    predictors: Mapping[str, Sequence[AudioPredictor]] | None = None,
) -> list[QACheck]:
    """Every audio check of a mix: delivery spec, metrics, and (flag-only) predictors.

    ``stems`` maps ``dx``/``mx``/``fx`` to their WAVs; sync reads the fx stem when given.
    """
    delivery = DELIVERY_SPECS[spec] if isinstance(spec, str) else spec
    try:
        rate, channels = read_wav(path)
        sync_rate, sync_channels = (
            read_wav(stems["fx"]) if stems and "fx" in stems else (rate, channels)
        )
    except (AudioError, OSError) as exc:
        return [QACheck(name="audio.read", status="fail", detail=str(exc))]
    rows = _loudness_rows(rate, channels, delivery)
    rows.append(check_clipping(channels))
    rows += check_phase(channels)
    rows.append(check_vo_intelligibility(stems or {}))
    rows += check_cutoff(rate, channels)
    rows.append(check_sync(sync_rate, sync_channels, cues, fps))
    rows.append(check_sample_reuse(cues))
    if predictors:
        rows += check_predictors({**(stems or {}), "mix": path}, predictors)
    return rows
