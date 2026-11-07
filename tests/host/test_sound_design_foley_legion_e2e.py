# SPDX-License-Identifier: AGPL-3.0-or-later
"""Opt-in e2e (#316): the studio sound_design stage requests HunyuanVideo-Foley on legion.

Needs the updated ``spectracut-host`` on the model host (``models-serve`` receiving the
picture inline as ``files``), ffmpeg on the control plane and the hunyuan-foley family
installed by ``spectracut setup``::

    SPECTRACUT_E2E=1 uv run pytest -q -m e2e tests/host/test_sound_design_foley_legion_e2e.py

Post-deploy check: ``studio-sound-design-foley-legion-e2e``. The edit has one board with a
real image, so sound design renders that shot with ffmpeg, ships the movie to the host
and places one foley cue on it; nothing is mocked. ``SPECTRACUT_MODEL_HOST`` (default
``legion``) and ``SPECTRACUT_MODEL_HOST_OS`` (default ``windows``) override the host.
"""

from __future__ import annotations

import os
import wave
from pathlib import Path

import pytest

from spectracut.cli.studio import DEPARTMENTS
from spectracut.edit.preview import FfmpegPreview
from spectracut.edit.timeline import Clip, Edit, Track
from spectracut.providers.local.registry import mock_mode, resolve_model
from spectracut.providers.local.remote import RemoteLoader
from spectracut.providers.local.runtime import ModelRuntime
from spectracut.providers.mock_image import solid_png
from spectracut.queue.executors.base import GpuCard, LeasePool
from spectracut.studio.departments import sound as sound_depts
from spectracut.studio.graph import RunContext, RunResult
from spectracut.studio.node import Tier

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(os.environ.get("SPECTRACUT_E2E") != "1", reason="set SPECTRACUT_E2E=1"),
]

HOST = os.environ.get("SPECTRACUT_MODEL_HOST", "legion")
HOST_OS = os.environ.get("SPECTRACUT_MODEL_HOST_OS", "windows")


def test_sound_design_generates_real_foley_from_the_picture_on_the_model_host(
    tmp_path: Path,
) -> None:
    assert not mock_mode(), "unset SPECTRACUT_PROVIDERS=mock: this test needs the real models"
    board = tmp_path / "board-a.png"
    board.write_bytes(solid_png(320, 180, (40, 90, 160)))
    picture = [
        Clip(id="pic-a", shot_id="a", media_ref=str(board), source_out=72),
        Clip(id="pic-b", shot_id="b", media_ref="board:b", source_out=48),
    ]
    edit = Edit(id="animatic", tracks=[Track(id="picture", kind="video", items=picture)])
    runtime = ModelRuntime(
        RemoteLoader(HOST, HOST_OS),
        LeasePool(8, 28, (GpuCard(name="RTX 3070 Laptop", vram_gb=8),)),
        host=HOST,
    )

    def unused(ctx: RunContext) -> RunResult:
        raise AssertionError(f"{ctx.spec.kind} must be built by SoundRunner")

    runner = sound_depts.SoundRunner(
        unused, DEPARTMENTS, workdir=tmp_path / "work", runtime=runtime, picture=FfmpegPreview()
    )
    spec = next(n for n in sound_depts.sound_nodes() if n.kind == sound_depts.SOUND_DESIGN)
    inputs = {spec.inputs[0]: edit.model_dump(mode="json")}
    result = runner(RunContext(spec=spec, tier=Tier.PREVIEW, key="k", inputs=inputs, attempt=1))
    design = sound_depts.SoundDesign.model_validate(result.output)

    foley = [c for c in design.cues if c.cue_id.startswith("foley-")]
    assert [c.cue_id for c in foley] == ["foley-pic-a"]
    assert foley[0].provenance.origin.startswith(resolve_model("foley").id)
    with wave.open(foley[0].media_ref, "rb") as w:
        assert w.getnframes() > 0 and any(w.readframes(w.getnframes()))
    assert result.usage.gpu_minutes > 0
