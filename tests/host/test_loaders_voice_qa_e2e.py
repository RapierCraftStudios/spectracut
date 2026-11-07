# SPDX-License-Identifier: AGPL-3.0-or-later
"""Opt-in smokes of the Kokoro, faster-whisper and CLAP loaders on the model host (#300).

Run on legion after ``spectracut setup`` installed the families and weights::

    SPECTRACUT_E2E=1 uv run pytest -m e2e tests/host/test_loaders_voice_qa_e2e.py

Kokoro speaks a line to a wav, Whisper transcribes that wav and CLAP embeds it and must
rank "a person speaking" first (the sanity check local-model-selection.md:111 failed).
Each model loads and closes before the next, within its declared VRAM.
"""

from __future__ import annotations

import json
import math
import os
import wave
from pathlib import Path
from typing import Any

import pytest
from spectracut_host.loaders._base import GIB, MAX_VRAM_GB
from spectracut_host.models import load_model

from spectracut.providers.local.registry import MODELS

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(os.environ.get("SPECTRACUT_E2E") != "1", reason="set SPECTRACUT_E2E=1"),
]

TEXT = (
    "Welcome to SpectraCut. This temporary narration lets us check timing before the final "
    "voice over is recorded."
)
CAPTIONS = ["a person speaking", "an explosion", "piano music"]


def _within_vram(session: Any) -> None:
    peak = session.peak_vram_bytes()
    assert peak is None or peak <= min(session.spec.vram_gb, MAX_VRAM_GB) * GIB


@pytest.fixture(scope="module")
def speech(tmp_path_factory: pytest.TempPathFactory) -> Path:
    s = load_model(MODELS["kokoro-82m"])
    try:
        out = Path(s.generate(TEXT, out_path=tmp_path_factory.mktemp("vo") / "vo.wav"))
        _within_vram(s)
    finally:
        s.close()
    return out


def test_kokoro_writes_a_valid_wav(speech: Path) -> None:
    assert speech.read_bytes()[:4] == b"RIFF"
    with wave.open(str(speech), "rb") as w:
        assert w.getframerate() == 24000 and w.getnchannels() == 1
        assert w.getnframes() / w.getframerate() > 1.0


def test_whisper_transcribes_with_word_timestamps(speech: Path, tmp_path: Path) -> None:
    s = load_model(MODELS["faster-whisper-large-v3-turbo"])
    try:
        out = s.generate(str(speech), out_path=tmp_path / "t.json", word_timestamps=True)
        _within_vram(s)
    finally:
        s.close()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert len(data["words"]) >= 5
    assert all(0 <= w["start"] < w["end"] for w in data["words"])
    assert "spectracut" in data["text"].lower().replace(" ", "")


def test_clap_embeds_and_ranks_speech_first(speech: Path, tmp_path: Path) -> None:
    s = load_model(MODELS["larger-clap-general"])
    try:
        out = s.generate(str(speech), out_path=tmp_path / "c.json", captions=CAPTIONS)
        _within_vram(s)
    finally:
        s.close()
    data = json.loads(out.read_text(encoding="utf-8"))
    emb = data["embedding"]
    assert emb and all(math.isfinite(x) for x in emb)
    assert data["top"] == "a person speaking", data["scores"]
