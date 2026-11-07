# SPDX-License-Identifier: AGPL-3.0-or-later
"""Opt-in smoke: HunyuanVideo-Foley XL on the real model host (legion) over ssh.

After ``spectracut setup`` installed the hunyuan-foley family on the model host::

    SPECTRACUT_E2E=1 uv run pytest -q -m e2e tests/host/test_hunyuan_foley_e2e.py

Makes a 3 s clip with ffmpeg, copies it to the host with scp (models-serve takes a
host-local ``video_path``), loads ``hunyuan-foley-xl`` through ``RemoteLoader`` and
checks the returned wav. The host session raises when peak VRAM exceeds 8 GB
(``BaseSession.check_peak_vram``), so a reply at all means it stayed within the card.
``SPECTRACUT_MODEL_HOST`` (default ``legion``), ``SPECTRACUT_MODEL_HOST_OS`` (default
``windows``) and ``SPECTRACUT_MODEL_HOST_CLIP`` (the clip path on the host) override.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import wave
from pathlib import Path

import pytest

from spectracut.providers.local.registry import MODELS
from spectracut.providers.local.remote import RemoteLoader

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(os.environ.get("SPECTRACUT_E2E") != "1", reason="set SPECTRACUT_E2E=1"),
]

HOST = os.environ.get("SPECTRACUT_MODEL_HOST", "legion")
HOST_OS = os.environ.get("SPECTRACUT_MODEL_HOST_OS", "windows")
REMOTE_CLIP = os.environ.get(
    "SPECTRACUT_MODEL_HOST_CLIP", "C:/spectracut/models/hunyuan-foley/smoke.mp4"
)
FFMPEG = shutil.which("ffmpeg")


def _ssh(remote: str) -> None:
    subprocess.run(["ssh", "-o", "BatchMode=yes", HOST, remote], check=True, timeout=120)


def _remote_dir_cmd(path: str) -> str:
    parent = path.rsplit("/", 1)[0]
    if HOST_OS.lower().startswith("windows"):
        return f"powershell -NoProfile -Command \"New-Item -ItemType Directory -Force '{parent}'\""
    return f"mkdir -p '{parent}'"


def _remote_rm_cmd(path: str) -> str:
    if HOST_OS.lower().startswith("windows"):
        return f"powershell -NoProfile -Command \"Remove-Item -Force -ErrorAction Ignore '{path}'\""
    return f"rm -f '{path}'"


@pytest.mark.skipif(FFMPEG is None, reason="needs ffmpeg on PATH to make the test clip")
def test_hunyuan_foley_writes_a_wav_on_the_model_host(tmp_path: Path) -> None:
    assert FFMPEG is not None
    clip = tmp_path / "smoke.mp4"
    subprocess.run(
        [
            FFMPEG, "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=duration=3:size=320x240:rate=25",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
            "-shortest", "-pix_fmt", "yuv420p", str(clip),
        ],
        check=True,
        timeout=120,
    )  # fmt: skip
    _ssh(_remote_dir_cmd(REMOTE_CLIP))
    subprocess.run(["scp", "-q", str(clip), f"{HOST}:{REMOTE_CLIP}"], check=True, timeout=300)
    session = RemoteLoader(HOST, HOST_OS)(MODELS["hunyuan-foley-xl"])
    try:
        out = session.generate(
            "a bouncing ball hits a wooden floor",
            out_path=tmp_path / "foley.wav",
            video_path=REMOTE_CLIP,
            seed=1,
        )
    finally:
        session.close()
        _ssh(_remote_rm_cmd(REMOTE_CLIP))
    with wave.open(str(out), "rb") as w:
        assert w.getnframes() > 0
        assert w.getframerate() > 0
