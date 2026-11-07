# SPDX-License-Identifier: AGPL-3.0-or-later
"""Audio input for the loaders that read audio (ASR, CLAP).

The input is either the prompt, a path to an audio file on the model host (what
:func:`spectracut.audio.captions.transcribe` sends), or the ``audio_b64`` option: the
file's bytes, base64-encoded, so a remote client can send audio over the JSON protocol.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from spectracut.providers.base import ProviderError

AUDIO_B64 = "audio_b64"


def input_audio(prompt: str, options: Mapping[str, Any], tmp_dir: Path, *, model: str) -> Path:
    """The audio file to read: ``audio_b64`` decoded under ``tmp_dir``, else the prompt path."""
    encoded = options.get(AUDIO_B64)
    if encoded is not None:
        if not isinstance(encoded, str):
            raise ProviderError("local", f"{model}: {AUDIO_B64} must be a base64 string")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ProviderError("local", f"{model}: {AUDIO_B64} is not valid base64") from exc
        if not data:
            raise ProviderError("local", f"{model}: {AUDIO_B64} is empty")
        path = Path(tmp_dir) / "input-audio"
        path.write_bytes(data)
        return path
    text = prompt.strip()
    path = Path(text)
    if not text or not path.is_file():
        raise ProviderError(
            "local", f"{model}: no input audio: pass a file path as the prompt or {AUDIO_B64}"
        )
    return path
