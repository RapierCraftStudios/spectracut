# SPDX-License-Identifier: AGPL-3.0-or-later
"""ACE-Step loader with fakes (no GPU, no weights); real-model smoke under SPECTRACUT_E2E=1."""

from __future__ import annotations

import os
import sys
import types
import wave
from pathlib import Path
from typing import Any

import pytest
from spectracut_host.loaders import ace_step
from spectracut_host.loaders._base import model_dir

from spectracut.providers.base import ProviderError
from spectracut.providers.local.registry import MODELS
from spectracut.setup.model_assets import MODEL_ROOT_ENV, SRC_DIR

SPEC = MODELS["ace-step-1.5"]
RATE = 100


class _Arr:
    """Tiny stand-in for a (frames, channels) array."""

    def __init__(self, n: int) -> None:
        self.n = n
        self.shape = (n, 2)
        self.dtype = "f4"

    def __len__(self) -> int:
        return self.n

    def __getitem__(self, s: slice) -> _Arr:
        return _Arr(len(range(*s.indices(self.n))))


@pytest.fixture
def fakes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    root = tmp_path / "root"
    monkeypatch.setenv(MODEL_ROOT_ENV, str(root))
    base = model_dir(SPEC, root)
    (base / SRC_DIR).mkdir(parents=True)
    dit = base / "Ace-Step1.5"
    (dit / "acestep-v15-turbo").mkdir(parents=True)
    (dit / "vae").mkdir()
    (base / "acestep-5Hz-lm-0.6B").mkdir()
    calls: dict[str, Any] = {"frames": 150, "ok": True, "init": []}

    class Handler:
        def initialize_service(self, **kw: Any) -> tuple[str, bool]:
            calls["init"].append(("dit", kw))
            return "ok", True

    class LLM:
        def initialize(self, **kw: Any) -> tuple[str, bool]:
            calls["init"].append(("lm", kw))
            return "ok", True

    class Params:
        def __init__(self, **kw: Any) -> None:
            calls["params"] = kw

    class Config:
        def __init__(self, **kw: Any) -> None:
            calls["config"] = kw

    def generate_music(dit: Any, llm: Any, *, params: Any, config: Any, save_dir: str) -> Any:
        p = Path(save_dir) / "a.wav"
        p.write_bytes(b"x")
        calls["save_dir"] = save_dir
        return types.SimpleNamespace(
            success=calls["ok"], status_message="boom", audios=[{"path": str(p)}]
        )

    pkg = types.ModuleType("acestep")
    mods = {
        "acestep": pkg,
        "acestep.handler": types.SimpleNamespace(AceStepHandler=Handler),
        "acestep.llm_inference": types.SimpleNamespace(LLMHandler=LLM),
        "acestep.inference": types.SimpleNamespace(
            GenerationParams=Params, GenerationConfig=Config, generate_music=generate_music
        ),
    }

    def sf_read(path: str, always_2d: bool = False) -> tuple[_Arr, int]:
        return _Arr(calls["frames"]), RATE

    def sf_write(path: str, data: _Arr, rate: int, format: str = "") -> None:
        calls["written"] = (path, len(data), rate, format)
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(b"\0\0" * len(data))

    mods["soundfile"] = types.SimpleNamespace(read=sf_read, write=sf_write)
    for k, v in mods.items():
        monkeypatch.setitem(sys.modules, k, v)  # type: ignore[arg-type]
    monkeypatch.setattr(ace_step, "fit_frames", lambda d, n: _Arr(n))
    calls["src"] = base / SRC_DIR
    return calls


def test_load_links_checkpoints_and_inits_offloaded(fakes: dict[str, Any]) -> None:
    session = ace_step.load(SPEC)
    ck = fakes["src"] / "checkpoints"
    assert (ck / "acestep-v15-turbo").is_symlink() and (ck / "vae").is_symlink()
    assert (ck / "acestep-5Hz-lm-0.6B").is_symlink()
    (_, dit_kw), (_, lm_kw) = fakes["init"]
    assert dit_kw["config_path"] == "acestep-v15-turbo" and dit_kw["offload_to_cpu"] is True
    assert lm_kw["lm_model_path"] == "acestep-5Hz-lm-0.6B" and lm_kw["offload_to_cpu"] is True
    assert str(fakes["src"]) in sys.path
    session.close()
    assert str(fakes["src"]) not in sys.path and session.dit is None


def test_generate_writes_exact_length_wav(fakes: dict[str, Any], tmp_path: Path) -> None:
    session = ace_step.load(SPEC)
    out = tmp_path / "out-1"  # no extension, like models-serve
    session.generate("warm pads", out_path=out, duration_s=2.0, seed=3)
    assert fakes["written"] == (str(out), 200, RATE, "WAV")
    assert fakes["params"]["duration"] == 2.0 and fakes["params"]["seed"] == 3
    assert (
        fakes["params"]["lyrics"] == "[Instrumental]" and fakes["params"]["caption"] == "warm pads"
    )
    assert fakes["config"] == {"batch_size": 1, "audio_format": "wav"}
    with wave.open(str(out)) as w:
        assert w.getnframes() == 200
    session.close()


def test_generate_failure_and_bad_duration(fakes: dict[str, Any], tmp_path: Path) -> None:
    session = ace_step.load(SPEC)
    with pytest.raises(ProviderError, match="positive"):
        session.generate("x", out_path=tmp_path / "o", duration_s=0)
    fakes["ok"] = False
    with pytest.raises(ProviderError, match="boom"):
        session.generate("x", out_path=tmp_path / "o", duration_s=1)
    session.close()


def test_init_failure_raises_and_restores_path(
    fakes: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    class Bad:
        def initialize_service(self, **kw: Any) -> tuple[str, bool]:
            return "no gpu", False

    monkeypatch.setattr(sys.modules["acestep.handler"], "AceStepHandler", Bad)
    with pytest.raises(ProviderError, match="no gpu"):
        ace_step.load(SPEC)
    assert str(fakes["src"]) not in sys.path


def test_missing_weights_say_run_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MODEL_ROOT_ENV, str(tmp_path))
    with pytest.raises(ProviderError, match="spectracut setup"):
        ace_step.load(SPEC)


def test_fit_frames_trims_and_pads() -> None:
    np = pytest.importorskip("numpy")
    data = np.ones((5, 2), dtype="float32")
    assert ace_step.fit_frames(data, 3).shape == (3, 2)
    padded = ace_step.fit_frames(data, 8)
    assert padded.shape == (8, 2) and padded[7].sum() == 0


@pytest.mark.e2e
@pytest.mark.skipif(os.environ.get("SPECTRACUT_E2E") != "1", reason="set SPECTRACUT_E2E=1")
def test_real_model_smoke(tmp_path: Path) -> None:
    sf = pytest.importorskip("soundfile")
    torch = pytest.importorskip("torch")

    session = ace_step.load(SPEC)
    try:
        out = session.generate(
            "warm synth pads, instrumental", out_path=tmp_path / "m", duration_s=10
        )
        info = sf.info(str(out))
        assert info.format == "WAV" and info.frames == round(10 * info.samplerate)
        assert torch.cuda.max_memory_allocated() <= 8 * 1024**3
    finally:
        session.close()
