# SPDX-License-Identifier: AGPL-3.0-or-later
"""Audio QA: delivery-spec metrics, intelligibility and flag-only predictors."""

from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

import pytest

from spectracut.qa.audio import (
    DELIVERY_SPECS,
    CueTiming,
    check_mix,
    integrated_loudness,
    read_wav,
    true_peak,
)
from spectracut.qa.audio_predictors import MockAudioPredictor, check_predictors
from spectracut.qa.common import QACheck
from spectracut.qa.report import QAReport

RATE = 16000


def write(path: Path, channels: list[list[float]], rate: int = RATE) -> Path:
    with wave.open(str(path), "wb") as out:
        out.setnchannels(len(channels))
        out.setsampwidth(2)
        out.setframerate(rate)
        frames = zip(*channels, strict=True)
        out.writeframes(b"".join(struct.pack("<h", round(v * 32767)) for f in frames for v in f))
    return path


def tone(
    freq: float, seconds: float, gain: float, rate: int = RATE, fade: float = 0.05
) -> list[float]:
    n = round(seconds * rate)
    f = round(fade * rate)
    return [
        gain * math.sin(2 * math.pi * freq * i / rate) * min(1.0, i / f, (n - i) / f)
        for i in range(n)
    ]


def by_name(rows: list[QACheck]) -> dict[str, QACheck]:
    return {r.name: r for r in rows}


def failed(rows: list[QACheck]) -> list[str]:
    return [r.name for r in rows if r.failed]


def lufs_tone(target: float, seconds: float, rate: int) -> list[float]:
    """A 997 Hz tone with a short fade, gained to measure ``target`` LUFS."""
    raw = tone(997, seconds, 1.0, rate)
    gain = 10 ** ((target - integrated_loudness(rate, [raw])) / 20)
    return [v * gain for v in raw]


def test_streaming_mix_30s_measures_spec(tmp_path: Path) -> None:
    rate = 48000
    mix = write(tmp_path / "mix.wav", [lufs_tone(-14.0, 30.0, rate)], rate)
    rows = by_name(check_mix(mix, "streaming"))
    assert rows["audio.loudness"].value == pytest.approx(-14.0, abs=0.5)
    assert rows["audio.true_peak"].value <= -1.0
    assert failed(list(rows.values())) == []


@pytest.mark.parametrize("preset", ["ebu_r128", "atsc_a85"])
def test_broadcast_presets_measure_to_spec(tmp_path: Path, preset: str) -> None:
    spec = DELIVERY_SPECS[preset]
    mix = write(tmp_path / "mix.wav", [lufs_tone(spec.target_lufs, 4.0, RATE)])
    rows = by_name(check_mix(mix, preset))
    assert rows["audio.loudness"].value == pytest.approx(spec.target_lufs, abs=spec.tolerance_lu)
    assert rows["audio.true_peak"].value <= spec.max_true_peak_dbtp
    assert failed(list(rows.values())) == []
    other = "atsc_a85" if preset == "ebu_r128" else "ebu_r128"
    assert not by_name(check_mix(mix, other))["audio.loudness"].failed or preset == "atsc_a85"


def test_streaming_tolerance_is_half_a_lu(tmp_path: Path) -> None:
    mix = write(tmp_path / "mix.wav", [lufs_tone(-14.7, 4.0, RATE)])
    assert by_name(check_mix(mix, "streaming"))["audio.loudness"].failed
    assert true_peak([[0.0]]) == -math.inf


def stems(tmp_path: Path, music_gain: float) -> dict[str, Path]:
    vo = tone(1000, 3.0, 0.1)
    mx = tone(1200, 3.0, music_gain)
    return {
        "dx": write(tmp_path / "dx.wav", [vo]),
        "mx": write(tmp_path / "mx.wav", [mx]),
        "fx": write(tmp_path / "fx.wav", [[0.0] * len(vo)]),
    }


def test_music_masking_vo_fails_intelligibility(tmp_path: Path) -> None:
    masked = stems(tmp_path, 0.2)
    mix = write(
        tmp_path / "mix.wav",
        [[a + b for a, b in zip(*(read_wav(masked[k])[1][0] for k in ("dx", "mx")), strict=True)]],
    )
    assert by_name(check_mix(mix, "streaming", stems=masked))["audio.vo_intelligibility"].failed


def test_clean_mix_passes_intelligibility(tmp_path: Path) -> None:
    clean = stems(tmp_path, 0.01)
    mix = write(tmp_path / "mix.wav", [tone(1000, 3.0, 0.1)])
    row = by_name(check_mix(mix, "streaming", stems=clean))["audio.vo_intelligibility"]
    assert row.status == "pass"


def test_clipping_fails(tmp_path: Path) -> None:
    samples = tone(500, 1.0, 0.5)
    samples[4000:4010] = [1.0] * 10
    rows = by_name(check_mix(write(tmp_path / "m.wav", [samples]), "streaming"))
    assert rows["audio.clipping"].failed
    assert not by_name(check_mix(write(tmp_path / "ok.wav", [tone(500, 1.0, 0.5)]), "streaming"))[
        "audio.clipping"
    ].failed


def test_phase_and_mono_compat(tmp_path: Path) -> None:
    left = tone(500, 1.0, 0.3)
    anti = by_name(check_mix(write(tmp_path / "a.wav", [left, [-v for v in left]]), "streaming"))
    assert anti["audio.phase_correlation"].failed and anti["audio.mono_compat"].failed
    same = by_name(check_mix(write(tmp_path / "s.wav", [left, left]), "streaming"))
    assert not same["audio.phase_correlation"].failed and not same["audio.mono_compat"].failed
    mono = by_name(check_mix(write(tmp_path / "m.wav", [left]), "streaming"))
    assert mono["audio.phase_correlation"].status == "skipped"


def test_cutoff_and_silence(tmp_path: Path) -> None:
    cut = [0.3 * math.sin(2 * math.pi * 500 * i / RATE) for i in range(RATE)]
    assert by_name(check_mix(write(tmp_path / "c.wav", [cut]), "streaming"))["audio.cutoff"].failed
    gap = tone(500, 1.0, 0.3) + [0.0] * (4 * RATE) + tone(500, 1.0, 0.3)
    assert by_name(check_mix(write(tmp_path / "g.wav", [gap]), "streaming"))["audio.silence"].failed
    ok = by_name(check_mix(write(tmp_path / "f.wav", [tone(500, 1.0, 0.3)]), "streaming"))
    assert not ok["audio.cutoff"].failed and not ok["audio.silence"].failed


def test_sync_within_one_frame(tmp_path: Path) -> None:
    pad = lambda t: [0.0] * round(t * RATE) + tone(800, 0.2, 0.5, fade=0.002) + [0.0] * RATE  # noqa: E731
    mix = write(tmp_path / "m.wav", [pad(1.0)])
    on = by_name(check_mix(mix, "streaming", cues=[CueTiming("hit", 1.0)]))
    assert not on["audio.sync"].failed
    late = by_name(check_mix(mix, "streaming", cues=[CueTiming("hit", 1.1)]))
    assert late["audio.sync"].failed


def test_repetition(tmp_path: Path) -> None:
    mix = write(tmp_path / "m.wav", [tone(500, 1.0, 0.3)])
    cues = [CueTiming("a", 0.1, "whoosh-1"), CueTiming("b", 0.5, "whoosh-1")]
    assert by_name(check_mix(mix, "streaming", cues=cues))["audio.repetition"].failed
    spread = [CueTiming("a", 0.1, "whoosh-1"), CueTiming("b", 9.0, "whoosh-1")]
    assert not by_name(check_mix(mix, "streaming", cues=spread))["audio.repetition"].failed


def test_predictors_flag_per_stem_and_never_fail(tmp_path: Path) -> None:
    files = stems(tmp_path, 0.01)
    mix = write(tmp_path / "mix.wav", [tone(997, 3.0, 0.1)])
    weak = MockAudioPredictor({"mx": 0.1, "dx": 0.9})
    predictors = {k: [weak] for k in ("dx", "mx", "fx", "mix")}
    rows = check_mix(mix, "ebu_r128", stems=files, predictors=predictors)
    scored = {r.name: r for r in rows if r.name.startswith("audio.predictor.")}
    assert set(scored) == {f"audio.predictor.{k}" for k in ("dx", "mx", "fx", "mix")}
    assert "FLAG" in scored["audio.predictor.mx"].detail
    assert "FLAG" not in scored["audio.predictor.dx"].detail
    assert all(r.status != "fail" for r in scored.values())
    assert [r for r in rows if r.failed and r.name.startswith("audio.predictor")] == []


def test_low_predictor_scores_do_not_fail_the_report(tmp_path: Path) -> None:
    mix = write(tmp_path / "mix.wav", [tone(997, 1.0, 0.1)])
    rows = check_predictors({"mix": mix}, {"mix": [MockAudioPredictor({"mix": 0.0})]})
    assert rows[0].value == 0.0 and "FLAG" in rows[0].detail
    assert not any(r.failed for r in rows)
    assert QAReport.model_fields["passed"] is not None
