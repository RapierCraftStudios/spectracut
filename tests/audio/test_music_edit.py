# SPDX-License-Identifier: AGPL-3.0-or-later
"""Music cut to picture: bar-boundary edits, the end-card hit, the stretch limit, stems."""

from __future__ import annotations

import math
import shutil
import subprocess
from array import array
from pathlib import Path

import pytest

from spectracut.audio.music_edit import (
    BeatGrid,
    DemucsSeparator,
    MusicEditError,
    RubberBandStretcher,
    analyze,
    cut_beats,
    edited_sound,
    plan_edit,
    render_edit,
    render_stems,
    separate_stems,
)
from spectracut.audio.recipe import Provenance
from spectracut.audio.render import Channels, read_wav, write_wav

RATE = 8000
FPS = 24.0
BAR = 2.0  # 120 BPM, 4/4
FIRST = 0.5
LAST = FIRST + 22 * BAR  # 44.5 s: the final hit on the last downbeat


def _click_track(path: Path, seconds: float = 45.0) -> Path:
    """Kick on every beat, accented on downbeats, then a big final hit."""
    n = int(seconds * RATE)
    data = array("f", bytes(4 * n))
    beat = BAR / 4
    t = FIRST
    index = 0
    while t < LAST - 1e-9:
        amp = 0.9 if index % 4 == 0 else 0.4
        _kick(data, round(t * RATE), amp, 0.06)
        t += beat
        index += 1
    _kick(data, round(LAST * RATE), 1.0, 0.45)
    write_wav(path, [data], RATE, float_format=True)
    return path


def _kick(data: array[float], start: int, amp: float, decay_s: float) -> None:
    for i in range(min(len(data) - start, round(decay_s * 3 * RATE))):
        data[start + i] += (
            amp * math.exp(-i / (decay_s * RATE)) * math.sin(2 * math.pi * 90 * i / RATE)
        )


class _Resample:
    """Test stretcher: linear resampling (changes pitch, keeps the timing a plan asks for)."""

    def stretch(self, channels: Channels, rate: int, ratio: float) -> Channels:
        out: Channels = []
        for chan in channels:
            n = round(len(chan) * ratio)
            row = array("f", bytes(4 * n))
            for i in range(n):
                x = i / ratio
                j = int(x)
                nxt = chan[min(j + 1, len(chan) - 1)]
                row[i] = chan[min(j, len(chan) - 1)] * (1 - (x - j)) + nxt * (x - j)
            out.append(row)
        return out


@pytest.fixture(scope="module")
def track(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _click_track(tmp_path_factory.mktemp("music") / "track.wav")


@pytest.fixture(scope="module")
def grid(track: Path) -> BeatGrid:
    return analyze(track)


def test_analyze_finds_tempo_bars_and_final_hit(grid: BeatGrid) -> None:
    assert grid.tempo_bpm == pytest.approx(120.0, abs=1.0)
    assert grid.downbeats[0] == pytest.approx(FIRST, abs=0.02)
    gaps = [b - a for a, b in zip(grid.downbeats, grid.downbeats[1:], strict=False)]
    assert all(g == pytest.approx(BAR, abs=0.03) for g in gaps)
    assert grid.hit_s == pytest.approx(LAST, abs=0.02)
    assert grid.sections[0] == 0


def test_45s_track_fits_a_30s_edit_with_every_edit_point_on_a_bar(grid: BeatGrid) -> None:
    plan = plan_edit(grid, target_s=30.0, fps=FPS)
    assert plan.out_duration_s == pytest.approx(30.0, abs=0.05)
    assert len(plan.segments) > 1  # bars were dropped, not just shortened
    for end, start in plan.edit_points:
        assert any(abs(end - d) < 1e-6 for d in grid.downbeats + (grid.duration_s,))
        assert any(abs(start - d) < 1e-6 for d in grid.downbeats)
    assert plan.segments[0][0] == grid.downbeats[0]


def test_final_hit_lands_within_one_frame_of_the_end_card(
    grid: BeatGrid, track: Path, tmp_path: Path
) -> None:
    plan = plan_edit(grid, target_s=30.0, end_card_s=29.0, fps=FPS)
    assert abs(plan.hit_out_s - 29.0) <= 1 / FPS
    out = render_edit(plan, track, tmp_path / "edit.wav", stretcher=_Resample())
    rate, channels = read_wav(out)
    assert len(channels[0]) / rate == pytest.approx(plan.out_duration_s, abs=1 / rate)
    assert plan.out_duration_s <= 30.0
    heard = analyze(out).hit_s
    assert abs(heard - 29.0) <= 1 / FPS


def test_stretch_stays_within_eight_percent_unless_the_cue_allows_more() -> None:
    bars = BeatGrid(8.0, 120.0, (), (0.0, 2.0, 4.0, 6.0), (0,), 6.0)
    assert plan_edit(bars, target_s=8.0, end_card_s=6.4, fps=FPS).stretch <= 0.08
    with pytest.raises(MusicEditError, match="allow more stretch"):
        plan_edit(bars, target_s=8.0, end_card_s=7.0, fps=FPS)
    loose = plan_edit(bars, target_s=8.0, end_card_s=7.0, fps=FPS, max_stretch=0.2)
    assert 0.08 < loose.stretch <= 0.2


def test_every_plan_respects_the_default_limit(grid: BeatGrid) -> None:
    for card in (20.0, 24.5, 27.0, 29.5):
        assert plan_edit(grid, target_s=30.0, end_card_s=card, fps=FPS).stretch <= 0.08


def test_plan_hash_is_stable_and_edit_sound_records_provenance(grid: BeatGrid) -> None:
    a = plan_edit(grid, target_s=30.0, fps=FPS)
    assert a.content_hash() == plan_edit(grid, target_s=30.0, fps=FPS).content_hash()
    assert (
        a.content_hash() != plan_edit(grid, target_s=30.0, end_card_s=27.0, fps=FPS).content_hash()
    )
    source = Provenance(source="library", origin="Track A", licence="CC-BY", commercial=True)
    spec = edited_sound(a, source)
    assert spec.provenance.origin == "music_edit(Track A)"
    assert spec.provenance.commercial and spec.params["plan"] == a.content_hash()


def test_supplied_stems_are_used_and_demucs_only_runs_without_them(tmp_path: Path) -> None:
    drums = tmp_path / "drums.wav"
    drums.write_bytes(b"x")

    class Boom:
        def separate(self, track: Path, outdir: Path) -> dict[str, Path]:
            raise AssertionError("must not run when the track has stems")

    assert separate_stems(
        tmp_path / "t.wav", tmp_path, stems={"drums": drums}, separator=Boom()
    ) == {"drums": drums}

    class Fake:
        def separate(self, track: Path, outdir: Path) -> dict[str, Path]:
            return {"bass": outdir / "bass.wav"}

    assert separate_stems(tmp_path / "t.wav", tmp_path, separator=Fake()) == {
        "bass": tmp_path / "bass.wav"
    }
    with pytest.raises(MusicEditError, match="missing"):
        separate_stems(tmp_path / "t.wav", tmp_path, stems={"gone": tmp_path / "gone.wav"})


def test_stems_are_cut_with_the_same_plan(grid: BeatGrid, track: Path, tmp_path: Path) -> None:
    plan = plan_edit(grid, target_s=30.0, fps=FPS)
    cut = render_stems(plan, {"mix": track, "again": track}, tmp_path, stretcher=_Resample())
    a, b = (read_wav(p)[1][0] for p in cut.values())
    assert list(a) == list(b) and len(a) == round(plan.out_duration_s * RATE)


def test_missing_binaries_raise_clear_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(MusicEditError, match="rubberband"):
        RubberBandStretcher().stretch([array("f", [0.0] * 10)], RATE, 1.0)
    with pytest.raises(MusicEditError, match="demucs"):
        DemucsSeparator().separate(tmp_path / "t.wav", tmp_path)


def test_rubberband_and_demucs_are_run_with_the_expected_arguments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[0] == "rb":
            _rate, chans = read_wav(Path(argv[-2]))
            write_wav(Path(argv[-1]), chans, RATE, float_format=True)
        else:
            stem = tmp_path / "htdemucs" / "t"
            stem.mkdir(parents=True)
            (stem / "drums.wav").write_bytes(b"x")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    RubberBandStretcher(binary="rb").stretch([array("f", [0.1] * 16)], RATE, 1.05)
    assert calls[0][1:4] == ["--quiet", "--time", "1.05000000"]
    stems = DemucsSeparator(binary="dm").separate(tmp_path / "t.wav", tmp_path)
    assert list(stems) == ["drums"] and calls[1][1:3] == ["-n", "htdemucs"]


def test_cut_beats_map_source_beats_through_the_plan(grid: BeatGrid) -> None:
    plan = plan_edit(grid, target_s=30.0, fps=FPS)
    beats = cut_beats(plan, grid)
    assert list(beats) == sorted(set(beats))
    assert all(0.0 <= b <= plan.out_duration_s for b in beats)
    # Every beat inside a kept bar is on the cut, offset by the bars before it.
    offset = 0.0
    for start, end in plan.segments:
        for b in grid.beats:
            if start <= b < end and (offset + b - start) * plan.ratio <= plan.out_duration_s:
                at = (offset + b - start) * plan.ratio
                assert any(abs(at - c) < 1e-5 for c in beats)
        offset += end - start
    # At 120 BPM the cut's beats are half a second apart (stretched by the ratio).
    gaps = [b - a for a, b in zip(beats, beats[1:], strict=False)]
    assert sorted(gaps)[len(gaps) // 2] == pytest.approx(0.5 * plan.ratio, abs=0.02)
