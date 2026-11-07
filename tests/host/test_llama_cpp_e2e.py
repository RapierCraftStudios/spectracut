# SPDX-License-Identifier: AGPL-3.0-or-later
"""Opt-in smoke: the omni critic on the model host (legion). Run with SPECTRACUT_E2E=1."""

from __future__ import annotations

import base64
import io
import math
import os
import struct
import wave
from pathlib import Path

import pytest

from spectracut.providers.local.registry import MODELS
from spectracut.providers.local.remote import RemoteLoader

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(os.environ.get("SPECTRACUT_E2E") != "1", reason="set SPECTRACUT_E2E=1"),
]

PROMPT = (
    "You are a sound editor. Listen to this clip and describe in 2 sentences what you hear, "
    "then rate its technical quality 1-5 and list any defects."
)


def _tone() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(
            b"".join(
                struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / 16000)))
                for i in range(3 * 16000)
            )
        )
    return buf.getvalue()


def test_omni_critic_describes_a_clip_on_the_model_host(tmp_path: Path) -> None:
    host = os.environ.get("SPECTRACUT_MODEL_HOST", "legion")
    host_os = os.environ.get("SPECTRACUT_MODEL_HOST_OS", "windows")
    session = RemoteLoader(host, host_os)(MODELS["qwen2.5-omni-7b-q4"])
    try:
        out = session.generate(
            PROMPT,
            out_path=tmp_path / "critic.txt",
            audio_b64=base64.b64encode(_tone()).decode(),
        )
        assert out.read_text(encoding="utf-8").strip()
    finally:
        session.close()
