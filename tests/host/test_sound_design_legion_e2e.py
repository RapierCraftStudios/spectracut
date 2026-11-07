# SPDX-License-Identifier: AGPL-3.0-or-later
"""Opt-in e2e: the studio's sound_design and music stages on the real model host (legion).

After ``spectracut setup`` installed the model families on the host::

    SPECTRACUT_E2E=1 uv run pytest -q -m e2e tests/host/test_sound_design_legion_e2e.py

Post-deploy check ``studio-sound-design-legion-e2e``. The stages run through
:class:`~spectracut.studio.departments.sound.SoundRunner` with a ``ModelRuntime`` over
``RemoteLoader`` (ssh ``models-serve``), nothing mocked. ``SPECTRACUT_MODEL_HOST`` (default
``legion``) and ``SPECTRACUT_MODEL_HOST_OS`` (default ``windows``) override the host.
"""

from __future__ import annotations

import os
import wave
from pathlib import Path

import pytest

from spectracut.cli.studio import DEPARTMENTS
from spectracut.edit.timeline import Clip, Edit, Track
from spectracut.providers.local.registry import mock_mode, resolve_model
from spectracut.providers.local.remote import RemoteLoader
from spectracut.providers.local.runtime import ModelRuntime
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


def _edit() -> Edit:
    picture = [
        Clip(id="pic-a", shot_id="a", media_ref="board:a", source_out=48),
        Clip(id="pic-b", shot_id="b", media_ref="board:b", source_out=48),
    ]
    return Edit(id="animatic", tracks=[Track(id="picture", kind="video", items=picture)])


def _runner(tmp_path: Path) -> sound_depts.SoundRunner:
    assert not mock_mode(), "unset SPECTRACUT_PROVIDERS=mock: this test needs the real models"
    runtime = ModelRuntime(
        RemoteLoader(HOST, HOST_OS),
        LeasePool(8, 28, (GpuCard(name="RTX 3070 Laptop", vram_gb=8),)),
        host=HOST,
    )

    def unused(ctx: RunContext) -> RunResult:
        raise AssertionError(f"{ctx.spec.kind} must be built by SoundRunner")

    return sound_depts.SoundRunner(unused, DEPARTMENTS, workdir=tmp_path, runtime=runtime)


def _run(runner: sound_depts.SoundRunner, kind: str) -> RunResult:
    spec = next(n for n in sound_depts.sound_nodes() if n.kind == kind)
    inputs = {spec.inputs[0]: _edit().model_dump(mode="json")}
    return runner(RunContext(spec=spec, tier=Tier.PREVIEW, key="k", inputs=inputs, attempt=1))


def _assert_real_wav(path: str) -> wave.Wave_read:
    w = wave.open(path, "rb")  # noqa: SIM115
    with w:
        assert w.getnframes() > 0 and w.getframerate() > 0
        assert any(w.readframes(w.getnframes())), f"{path} is silent"
    return w


def test_sound_design_stage_generates_real_sfx_on_the_model_host(tmp_path: Path) -> None:
    result = _run(_runner(tmp_path), sound_depts.SOUND_DESIGN)
    design = sound_depts.SoundDesign.model_validate(result.output)
    assert design.cues
    model = resolve_model("sfx").id
    for cue in design.cues:
        _assert_real_wav(cue.media_ref)
        assert cue.provenance.origin.startswith(model), cue.provenance.origin
        assert "mock" not in cue.provenance.origin
    assert result.usage.gpu_minutes > 0


def test_music_stage_generates_a_real_bed_on_the_model_host(tmp_path: Path) -> None:
    result = _run(_runner(tmp_path), sound_depts.MUSIC)
    bed = sound_depts.MusicBed.model_validate(result.output)
    with wave.open(bed.media_ref, "rb") as w:
        seconds = w.getnframes() / w.getframerate()
    _assert_real_wav(bed.media_ref)
    assert abs(seconds - float(_edit().seconds)) <= 1 / 24 + 1e-3
    assert "mock" not in bed.provenance.origin
    assert result.usage.gpu_minutes > 0
