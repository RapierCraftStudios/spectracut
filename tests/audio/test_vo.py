# SPDX-License-Identifier: AGPL-3.0-or-later
"""VO: temp VO is local, final VO is cached per line after lock, captions land within a frame."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from spectracut.audio.captions import Word, align_captions, captions_from_words
from spectracut.audio.vo import ScriptNotLockedError, VoiceOver, VoLine
from spectracut.edit.timeline import FrameRate
from spectracut.providers.base import ProviderAuthError
from spectracut.providers.elevenlabs import ElevenLabsProvider, MockElevenLabs
from spectracut.providers.local.registry import MockLocalProvider
from spectracut.providers.local.tts import LocalTts
from spectracut.studio.ledger import Ledger, LedgerEntry


class CountingEleven(MockElevenLabs):
    def __init__(self) -> None:
        self.calls: list[str] = []

    def synthesize(self, text: str, voice: str, settings: Any, out_path: Path) -> Path:
        self.calls.append(text)
        return super().synthesize(text, voice, settings, out_path)


LINES = [VoLine(id="a", text="Hello there."), VoLine(id="b", text="Meet SpectraCut.")]


def _vo(tmp_path: Path, eleven: CountingEleven) -> VoiceOver:
    return VoiceOver(
        tmp_path / "vo",
        voice="v1",
        settings={"stability": 0.5},
        local=LocalTts(MockLocalProvider("tts")),
        final_provider=eleven,
    )


def test_no_elevenlabs_before_script_lock(tmp_path: Path) -> None:
    eleven = CountingEleven()
    vo = _vo(tmp_path, eleven)
    temp = vo.temp(LINES)
    assert all(p.is_file() for p in temp.values())
    with pytest.raises(ScriptNotLockedError):
        vo.final(LINES, script_locked=False)
    assert eleven.calls == []


def test_changing_one_line_regenerates_only_that_line(tmp_path: Path) -> None:
    eleven = CountingEleven()
    vo = _vo(tmp_path, eleven)
    first = vo.final(LINES, script_locked=True)
    assert first.regenerated == ["a", "b"]
    again = vo.final(LINES, script_locked=True)
    assert again.regenerated == [] and again.usage.elevenlabs_characters == 0
    edited = [LINES[0], VoLine(id="b", text="Meet SpectraCut now.")]
    third = vo.final(edited, script_locked=True)
    assert third.regenerated == ["b"]
    assert eleven.calls == ["Hello there.", "Meet SpectraCut.", "Meet SpectraCut now."]
    assert third.paths["a"] == first.paths["a"]


def test_voice_and_settings_are_part_of_the_cache_key(tmp_path: Path) -> None:
    eleven = CountingEleven()
    vo = _vo(tmp_path, eleven)
    vo.final(LINES[:1], script_locked=True)
    other = VoiceOver(vo.cache_dir, voice="v2", settings=vo.settings, final_provider=eleven)
    assert other.final(LINES[:1], script_locked=True).regenerated == ["a"]


def test_characters_are_recorded_in_the_ledger(tmp_path: Path) -> None:
    vo = _vo(tmp_path, CountingEleven())
    ledger = Ledger()
    for _ in range(2):  # second run is all cache hits
        usage = vo.final(LINES, script_locked=True).usage
        ledger.append(
            LedgerEntry(
                production_id="p1",
                node_id="vo",
                kind="vo",
                department="audio",
                node_key="sha256:" + "0" * 64,
                tier="final",
                attempt=1,
                started_at=datetime.now(UTC),
                duration_s=1.0,
                elevenlabs_characters=usage.elevenlabs_characters,
                outcome="accepted",
            )
        )
    assert ledger.totals().elevenlabs_characters == sum(len(line.text) for line in LINES)


class FakeAsr:
    kind, name = "asr", "fake-asr"

    def __init__(self, words: list[dict[str, Any]]) -> None:
        self.words = words

    def batch(self) -> Any:
        raise NotImplementedError

    def generate(self, prompt: str, out_path: Path, **options: Any) -> Path:
        out_path.write_text(json.dumps({"words": self.words}), encoding="utf-8")
        return out_path


@pytest.mark.parametrize("rate", [FrameRate.of(24), FrameRate(numerator=24000, denominator=1001)])
def test_captions_align_within_one_frame(tmp_path: Path, rate: FrameRate) -> None:
    raw = [
        {"word": " Hello", "start": 0.0, "end": 0.37},
        {"word": " there", "start": 0.41, "end": 0.9},
        {"word": " friend", "start": 1.234, "end": 1.99},
    ]
    caps = align_captions(
        tmp_path / "a.wav",
        rate,
        tmp_path,
        provider=FakeAsr(raw),
        max_words=1,
    )
    assert [c.text for c in caps] == ["Hello", "there", "friend"]
    for cap, w in zip(caps, raw, strict=True):
        assert abs(rate.seconds(cap.start_frame) - Fraction(str(w["start"]))) <= 1 / rate.fps
        assert abs(rate.seconds(cap.end_frame) - Fraction(str(w["end"]))) <= 1 / rate.fps
        assert cap.end_frame > cap.start_frame


def test_zero_length_word_still_gets_a_frame() -> None:
    caps = captions_from_words([Word("x", Fraction(1), Fraction(1))], FrameRate.of(24))
    assert caps[0].end_frame == caps[0].start_frame + 1


def test_elevenlabs_needs_a_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    with pytest.raises(ProviderAuthError):
        ElevenLabsProvider().synthesize("hi", "v", {}, tmp_path / "x.mp3")
