# SPDX-License-Identifier: AGPL-3.0-or-later
"""Loudness and true peak of a PCM WAV master against a delivery spec.

ITU-R BS.1770-4 in pure Python: K-weighting (a high shelf and a high pass
biquad), 400 ms blocks with 75% overlap, a -70 LUFS absolute gate and a -10 LU
relative gate; true peak by 4x oversampling through a polyphase windowed-sinc
FIR. #61 extends this module with predictors and stricter tolerances.
"""

from __future__ import annotations

import math
import wave
from collections.abc import Sequence
from pathlib import Path

from pydantic import Field

from spectracut.qa.common import QACheck
from spectracut.spec._base import StrictModel

ABSOLUTE_GATE = -70.0
RELATIVE_GATE = -10.0
BLOCK_S = 0.4
STEP_S = 0.1
OVERSAMPLE = 4
TAPS_PER_PHASE = 12
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
        name="streaming", target_lufs=-14.0, tolerance_lu=1.0, max_true_peak_dbtp=-1.0
    ),
    "social": DeliverySpec(
        name="social", target_lufs=-14.0, tolerance_lu=1.0, max_true_peak_dbtp=-1.0
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


def check_audio(path: Path, spec: DeliverySpec | str) -> list[QACheck]:
    """Loudness within the spec's tolerance and true peak at or under its ceiling."""
    delivery = DELIVERY_SPECS[spec] if isinstance(spec, str) else spec
    try:
        rate, channels = read_wav(path)
    except (AudioError, OSError) as exc:
        return [QACheck(name="audio.read", status="fail", detail=str(exc))]
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
