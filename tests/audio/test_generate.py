# SPDX-License-Identifier: AGPL-3.0-or-later
"""Generative sound: candidates, scores, one load per model, provenance, no external API.

The e2e test runs real models on a render host::

    SPECTRACUT_E2E=1 SPECTRACUT_MODEL_ENTRY_EZAUDIO=module:factory \
    uv run pytest -m e2e tests/audio/test_generate.py
"""

from __future__ import annotations

import math
import os
import socket
import struct
import subprocess
import sys
import wave
from pathlib import Path
from typing import Any

import pytest

from spectracut.audio.generate import (
    CueRequest,
    GenerativeSoundGenerator,
    generate_scene,
    qa_score,
)
from spectracut.audio.render import SoundCache, read_wav
from spectracut.providers.local.registry import MODELS
from spectracut.providers.local.runtime import ModelRuntime, ModelSpec
from spectracut.queue.executors.base import GpuCard, LeasePool

RATE = 16000


def _write(path: Path, amp: float, seconds: float = 0.2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = int(RATE * seconds)
    frames = b"".join(
        struct.pack(
            "<h", max(-32768, min(32767, int(32000 * amp * math.sin(2 * math.pi * 220 * i / RATE))))
        )
        for i in range(n)
    )
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(frames)


class FakeSession:
    """Writes a tone whose loudness depends on the seed, so candidates differ."""

    def __init__(self, spec: ModelSpec, log: list[str]) -> None:
        self.spec, self.log = spec, log

    def generate(self, prompt: str, **options: Any) -> None:
        self.log.append(f"gen {self.spec.id}")
        _write(options["out_path"], 0.1 + 0.2 * (options["seed"] % 3), options["duration_s"])

    def close(self) -> None:
        self.log.append(f"close {self.spec.id}")


def runtime() -> tuple[ModelRuntime, list[str]]:
    log: list[str] = []

    def loader(spec: ModelSpec) -> FakeSession:
        log.append(f"load {spec.id}")
        return FakeSession(spec, log)

    pool = LeasePool(8, 32, [GpuCard(name="gpu0", vram_gb=8)])
    return ModelRuntime(loader, pool), log


SCENE = [
    CueRequest("hit", "sfx", "heavy door impact", 0.2, size="large", palette="warm"),
    CueRequest("whoosh", "sfx", "airy whoosh", 0.2),
    CueRequest("steps", "foley", "footsteps on gravel", 0.2, video_path=Path("shot.mp4")),
    CueRequest("score", "music", "slow cinematic pulse", 0.2, stems=("drums", "pads")),
]


def test_each_cue_gets_n_candidates_and_best_is_recorded(tmp_path: Path) -> None:
    rt, _ = runtime()
    out = generate_scene(SCENE[:2], tmp_path, runtime=rt, candidates=4)
    for sel in out:
        assert len(sel.candidates) == 4
        assert all(c.path.is_file() for c in sel.candidates)
        assert sel.score == max(c.score for c in sel.candidates)
        assert sel.spec.seed == sel.chosen  # request seed 0 + chosen candidate
    assert len({c.score for c in out[0].candidates}) > 1


def test_candidates_configurable_and_validated(tmp_path: Path) -> None:
    rt, _ = runtime()
    assert len(generate_scene(SCENE[:1], tmp_path, runtime=rt, candidates=1)[0].candidates) == 1
    with pytest.raises(ValueError):
        generate_scene(SCENE[:1], tmp_path, runtime=rt, candidates=0)


def test_custom_scorer_decides_and_ties_take_first(tmp_path: Path) -> None:
    rt, _ = runtime()
    sel = generate_scene(SCENE[:1], tmp_path, runtime=rt, candidates=3, scorer=lambda p: 1.0)[0]
    assert sel.chosen == 0
    sel = generate_scene(
        SCENE[:1],
        tmp_path,
        runtime=rt,
        candidates=3,
        scorer=lambda p: float(p.name.endswith("c2.wav")),
    )[0]
    assert sel.chosen == 2


def test_parallel_candidates_same_result(tmp_path: Path) -> None:
    rt, log = runtime()
    serial = generate_scene(SCENE[:2], tmp_path / "a", runtime=rt, candidates=3)
    par = generate_scene(SCENE[:2], tmp_path / "b", runtime=rt, candidates=3, parallel=3)
    assert [s.chosen for s in serial] == [s.chosen for s in par]
    assert log.count("load ezaudio-xl") == 2  # once per scene


def test_scene_loads_each_model_once(tmp_path: Path) -> None:
    rt, log = runtime()
    out = generate_scene(SCENE, tmp_path, runtime=rt, candidates=3)
    assert [s.cue_id for s in out] == ["hit", "whoosh", "steps", "score-drums", "score-pads"]
    assert rt.loads == {"ezaudio-xl": 1, "hunyuan-foley-xl": 1, "ace-step-1.5": 1}
    assert log.count("gen ezaudio-xl") == 6 and log.count("gen ace-step-1.5") == 6
    assert "close ezaudio-xl" in log  # lease released after the scene


def test_provenance_records_model_version_and_prompt(tmp_path: Path) -> None:
    rt, _ = runtime()
    sel = generate_scene(SCENE[:1], tmp_path, runtime=rt, candidates=2)[0]
    spec = MODELS["ezaudio-xl"]
    assert sel.spec.provenance.source == "generated"
    assert sel.spec.provenance.origin == f"{spec.id}@{spec.version}" and spec.version
    assert sel.spec.params == {
        "prompt": sel.prompt,
        "model": "ezaudio-xl",
        "model_version": spec.version,
    }
    assert "heavy door impact" in sel.prompt and "warm" in sel.prompt


def test_foley_gets_picture_and_stems_are_in_prompts(tmp_path: Path) -> None:
    seen: list[dict[str, Any]] = []

    class Spy(FakeSession):
        def generate(self, prompt: str, **options: Any) -> None:
            seen.append({"prompt": prompt, **options})
            super().generate(prompt, **options)

    log: list[str] = []
    pool = LeasePool(8, 32, [GpuCard(name="gpu0", vram_gb=8)])
    rt = ModelRuntime(lambda spec: Spy(spec, log), pool)
    generate_scene(SCENE[2:], tmp_path, runtime=rt, candidates=1)
    assert seen[0]["video_path"] == Path("shot.mp4")
    assert [("drums" in s["prompt"], "pads" in s["prompt"]) for s in seen[1:]] == [
        (True, False),
        (False, True),
    ]


def test_mock_mode_needs_no_runtime_and_generator_fits_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPECTRACUT_PROVIDERS", "mock")
    out = generate_scene(SCENE, tmp_path, candidates=2)
    assert len(out) == 5 and all(s.version == "mock" for s in out)
    gen = GenerativeSoundGenerator(out)
    cache = SoundCache(tmp_path / "cache")
    path = cache.ensure(out[0].spec, gen, 48000)
    rate, chans = read_wav(path)
    assert rate == 48000 and len(chans) == 2 and len(chans[0]) == out[0].spec.samples(48000)


def test_no_external_api_for_sfx_or_music(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    code = (
        "import sys, spectracut.audio.generate;"
        "bad=[m for m in sys.modules if m.startswith(('spectracut.providers.api_key',"
        "'spectracut.providers.claude_code','spectracut.providers.codex_cli','requests','httpx','openai','anthropic'))];"
        "print(bad)"
    )
    res = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
    assert res.stdout.strip() == "[]", res.stdout + res.stderr

    def refuse(*a: Any, **k: Any) -> None:
        raise AssertionError("network access")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    rt, _ = runtime()
    assert generate_scene(SCENE, tmp_path, runtime=rt, candidates=1)


def test_qa_score_rejects_silence_and_clipping(tmp_path: Path) -> None:
    _write(tmp_path / "ok.wav", 0.4)
    _write(tmp_path / "clip.wav", 1.5)
    _write(tmp_path / "quiet.wav", 0.0)
    (tmp_path / "bad.wav").write_bytes(b"nope")
    ok = qa_score(tmp_path / "ok.wav")
    assert ok > qa_score(tmp_path / "clip.wav")
    assert qa_score(tmp_path / "quiet.wav") == 0.0 == qa_score(tmp_path / "bad.wav")


def test_unknown_kind_and_duplicate_ids_rejected(tmp_path: Path) -> None:
    rt, _ = runtime()
    with pytest.raises(Exception, match="unknown generative kind"):
        generate_scene([CueRequest("a", "tts", "x", 1.0)], tmp_path, runtime=rt)
    with pytest.raises(ValueError, match="unique"):
        generate_scene([SCENE[0], SCENE[0]], tmp_path, runtime=rt)


@pytest.mark.e2e
@pytest.mark.skipif(os.environ.get("SPECTRACUT_E2E") != "1", reason="set SPECTRACUT_E2E=1")
def test_e2e_real_scene_loads_each_model_once(tmp_path: Path) -> None:
    from spectracut_host.models import load_model

    count: dict[str, int] = {}

    def counting(spec: ModelSpec) -> Any:
        count[spec.id] = count.get(spec.id, 0) + 1
        return load_model(spec)

    pool = LeasePool(4, 16, [GpuCard(name="gpu", vram_gb=8)])
    scene = [CueRequest(f"c{i}", "sfx", f"short impact {i}", 2.0) for i in range(4)]
    out = generate_scene(scene, tmp_path, runtime=ModelRuntime(counting, pool), candidates=2)
    assert all(s.path.stat().st_size > 0 for s in out)
    assert count == {"ezaudio-xl": 1}
