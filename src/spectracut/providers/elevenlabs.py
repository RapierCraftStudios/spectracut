# SPDX-License-Identifier: AGPL-3.0-or-later
"""ElevenLabs text-to-speech: the only external service used for final VO.

The key comes from ``ELEVENLABS_API_KEY`` and is never logged or stored. Callers (see
:mod:`spectracut.audio.vo`) cache by line so each line is billed once. In mock mode
(``SPECTRACUT_PROVIDERS=mock``) :class:`MockElevenLabs` writes a silent WAV with no network.
"""

from __future__ import annotations

import io
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import wave
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

from spectracut.providers.base import ProviderAuthError, ProviderError, redact
from spectracut.providers.local.registry import mock_mode

SERVICE = "elevenlabs"
ENV_VAR = "ELEVENLABS_API_KEY"
TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice}"
DEFAULT_MODEL = "eleven_multilingual_v2"


class _Response(Protocol):
    def read(self) -> bytes: ...

    def __enter__(self) -> _Response: ...

    def __exit__(self, *args: object) -> None: ...


Opener = Callable[..., _Response]


class VoiceProvider(Protocol):
    """Final-VO synthesis: one call per line."""

    extension: str

    def synthesize(
        self, text: str, voice: str, settings: Mapping[str, Any], out_path: Path
    ) -> Path: ...


class ElevenLabsProvider:
    extension = "mp3"

    def __init__(self, opener: Opener | None = None, timeout: float = 120.0) -> None:
        self.opener: Opener = opener or urllib.request.urlopen
        self.timeout = timeout

    def synthesize(
        self, text: str, voice: str, settings: Mapping[str, Any], out_path: Path
    ) -> Path:
        key = os.environ.get(ENV_VAR, "").strip()
        if not key:
            raise ProviderAuthError(SERVICE, f"{ENV_VAR} is not set")
        body = {"text": text, "model_id": DEFAULT_MODEL, "voice_settings": dict(settings)}
        request = urllib.request.Request(
            TTS_URL.format(voice=urllib.parse.quote(voice, safe="")),
            data=json.dumps(body).encode(),
            headers={"content-type": "application/json", "xi-api-key": key, "accept": "audio/mpeg"},
            method="POST",
        )
        try:
            with self.opener(request, timeout=self.timeout) as response:
                audio = response.read()
        except urllib.error.HTTPError as exc:
            detail = redact(exc.read().decode("utf-8", "replace")[:300])
            if exc.code in (401, 403):
                raise ProviderAuthError(
                    SERVICE, f"{ENV_VAR} was rejected (HTTP {exc.code})"
                ) from None
            raise ProviderError(SERVICE, f"HTTP {exc.code}: {detail}") from None
        except urllib.error.URLError as exc:
            raise ProviderError(SERVICE, f"request failed: {redact(str(exc.reason))}") from None
        if not audio:
            raise ProviderError(SERVICE, "empty audio response")
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(audio)
        return out_path


class MockElevenLabs:
    """Deterministic silent WAV, about 60 ms per character: no network, no key."""

    extension = "wav"
    rate = 16000

    def synthesize(
        self, text: str, voice: str, settings: Mapping[str, Any], out_path: Path
    ) -> Path:
        frames = int(self.rate * 0.06 * max(1, len(text)))
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.rate)
            w.writeframes(b"\x00\x00" * frames)
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(buf.getvalue())
        return out_path


def get_voice_provider() -> VoiceProvider:
    """The mock in mock mode, else ElevenLabs."""
    return MockElevenLabs() if mock_mode() else ElevenLabsProvider()
