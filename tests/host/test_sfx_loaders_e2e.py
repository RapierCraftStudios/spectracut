# SPDX-License-Identifier: AGPL-3.0-or-later
"""Smoke tests for the sound-effect loaders on the model host (legion).

Needs a CUDA GPU and the ezaudio and moss-sfx families installed by ``spectracut setup``.
Run them on the model host::

    SPECTRACUT_E2E=1 uv run pytest -q -m e2e tests/host/test_sfx_loaders_e2e.py
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest
from spectracut_host.models import load_model, model_lock

from spectracut.providers.local.registry import MODELS

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(os.environ.get("SPECTRACUT_E2E") != "1", reason="set SPECTRACUT_E2E=1"),
]

DURATION_S = 4
MAX_VRAM_BYTES = 8 * 1024**3


@pytest.mark.parametrize("model_id", ["ezaudio-xl", "moss-sfx-8b-nf4"])
def test_sfx_model_writes_a_valid_wav_within_8_gb(model_id: str, tmp_path: Path) -> None:
    torch = pytest.importorskip("torch")
    if not torch.cuda.is_available():
        pytest.skip("needs a CUDA GPU (the model host)")
    sf = importlib.import_module("soundfile")
    with model_lock():
        session = load_model(MODELS[model_id])
        try:
            out = session.generate(
                "Heavy wooden door creaking open slowly, then a soft thud.",
                out_path=tmp_path / "out",
                duration_s=DURATION_S,
                seed=1,
            )
            peak = session.peak_vram_bytes()  # type: ignore[attr-defined]
        finally:
            session.close()
    audio, sr = sf.read(str(out))
    assert sr > 0
    assert len(audio) == round(DURATION_S * sr)
    assert abs(audio).max() > 0
    assert peak is not None and peak <= MAX_VRAM_BYTES
