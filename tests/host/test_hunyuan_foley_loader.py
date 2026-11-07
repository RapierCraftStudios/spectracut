# SPDX-License-Identifier: AGPL-3.0-or-later
"""HunyuanVideo-Foley loader with a fake pinned source, torch, numpy and soundfile."""

from __future__ import annotations

import os
import subprocess
import sys
import types
import wave
from pathlib import Path
from typing import Any

import pytest
from spectracut_host import models as host_models
from spectracut_host.loaders import hunyuan_foley
from spectracut_host.loaders._base import model_dir

from spectracut.providers.base import ProviderError
from spectracut.providers.local.registry import MODELS
from spectracut.setup.model_assets import MODEL_ROOT_ENV, SRC_DIR

SPEC = MODELS["hunyuan-foley-xl"]
RATE = 48000


class FakeAudio:
    """``[channels, samples]`` like the tensor denoise_process returns per batch item."""

    def __init__(self, rows: list[list[float]]) -> None:
        self.rows = rows
        self.ndim = 2

    @property
    def T(self) -> list[list[float]]:  # noqa: N802 - numpy's name
        return [list(col) for col in zip(*self.rows, strict=True)]


class Hub:
    """A transformers class that records the ids it is asked to load."""

    def __init__(self, name: str, calls: list[tuple[str, str, bool]]) -> None:
        self.name = name
        self.calls = calls

    def from_pretrained(self, ref: str, *args: Any, **kwargs: Any) -> str:
        self.calls.append((self.name, ref, os.environ.get("HF_HUB_OFFLINE") == "1"))
        return f"{self.name}:{ref}"


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setenv(MODEL_ROOT_ENV, str(tmp_path / "models"))
    for var in hunyuan_foley.OFFLINE_ENV:
        monkeypatch.delenv(var, raising=False)
    root = model_dir(SPEC, tmp_path / "models")
    for sub in ("HunyuanVideo-Foley", "siglip2-base-patch16-512", "larger_clap_general"):
        (root / sub).mkdir(parents=True)
    (root / SRC_DIR / "configs").mkdir(parents=True)
    (root / SRC_DIR / hunyuan_foley.CONFIG).write_text("model: xl\n", encoding="utf-8")

    rec: dict[str, Any] = {"root": root, "hub": [], "seeds": []}

    torch = types.ModuleType("torch")
    torch.cuda = types.SimpleNamespace(is_available=lambda: False)  # type: ignore[attr-defined]
    torch.device = lambda name: f"device:{name}"  # type: ignore[attr-defined]
    torch.manual_seed = lambda s: rec["seeds"].append(("torch", s))  # type: ignore[attr-defined]
    numpy = types.ModuleType("numpy")
    numpy.random = types.SimpleNamespace(  # type: ignore[attr-defined]
        seed=lambda s: rec["seeds"].append(("numpy", s))
    )

    def write(path: str, data: list[list[float]], rate: int, **kwargs: Any) -> None:
        rec["write"] = {"data": data, "rate": rate, **kwargs}
        with wave.open(path, "wb") as w:
            w.setnchannels(len(data[0]))
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(b"\x00\x00" * len(data) * len(data[0]))

    soundfile = types.ModuleType("soundfile")
    soundfile.write = write  # type: ignore[attr-defined]

    model_utils = types.ModuleType("hunyuanvideo_foley.utils.model_utils")
    for name in hunyuan_foley.HUB_LOADERS:
        setattr(model_utils, name, Hub(name, rec["hub"]))

    def load_model(*args: Any, **kwargs: Any) -> tuple[str, str]:
        rec["load"] = (args, kwargs, os.environ.get("HF_HUB_OFFLINE"))
        # The real model_utils loads its encoders lazily; one at load, one at generate.
        model_utils.AutoModel.from_pretrained(hunyuan_foley.SIGLIP2)
        return "model_dict", "cfg"

    def denoise_process(*args: Any, **kwargs: Any) -> tuple[list[FakeAudio], int]:
        rec["denoise"] = (args, kwargs)
        return [FakeAudio([[0.0, 0.1, 0.2], [0.0, -0.1, -0.2]])], RATE

    model_utils.load_model = load_model  # type: ignore[attr-defined]
    model_utils.denoise_process = denoise_process  # type: ignore[attr-defined]

    feature_utils = types.ModuleType("hunyuanvideo_foley.utils.feature_utils")

    def feature_process(*args: Any, **kwargs: Any) -> tuple[str, str, float]:
        rec["features"] = (args, kwargs)
        model_utils.ClapTextModelWithProjection.from_pretrained(hunyuan_foley.CLAP)
        return "visual", "text", 3.0

    feature_utils.feature_process = feature_process  # type: ignore[attr-defined]

    for name, mod in {
        "torch": torch,
        "numpy": numpy,
        "soundfile": soundfile,
        "hunyuanvideo_foley": types.ModuleType("hunyuanvideo_foley"),
        "hunyuanvideo_foley.utils": types.ModuleType("hunyuanvideo_foley.utils"),
        "hunyuanvideo_foley.utils.model_utils": model_utils,
        "hunyuanvideo_foley.utils.feature_utils": feature_utils,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.setattr(sys, "path", list(sys.path))
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"not really a video")
    rec["clip"] = clip
    return rec


def test_load_uses_xl_checkpoint_with_offload_and_local_encoders(fake: dict[str, Any]) -> None:
    root: Path = fake["root"]
    session = hunyuan_foley.load(SPEC)
    args, kwargs, offline = fake["load"]
    assert args[:2] == (
        str(root / "HunyuanVideo-Foley"),
        str(root / SRC_DIR / hunyuan_foley.CONFIG),
    )
    assert args[2] == "device:cpu"
    assert kwargs == {"enable_offload": True, "model_size": "xl"}
    assert offline == "1"
    assert str(root / SRC_DIR) in sys.path
    assert fake["hub"] == [("AutoModel", str(root / "siglip2-base-patch16-512"), True)]
    assert os.environ.get("HF_HUB_OFFLINE") is None
    assert (session.model_dict, session.cfg) == ("model_dict", "cfg")


def test_generate_writes_a_wav_from_the_clip(fake: dict[str, Any], tmp_path: Path) -> None:
    session = hunyuan_foley.load(SPEC)
    out = session.generate(
        "footsteps on gravel",
        out_path=tmp_path / "o" / "foley.wav",
        video_path=str(fake["clip"]),
        seed=7,
        duration_s=3.0,
    )
    with wave.open(str(out), "rb") as w:
        assert (w.getnchannels(), w.getframerate(), w.getnframes()) == (2, RATE, 3)
    assert fake["write"]["subtype"] == "PCM_16"
    assert fake["write"]["data"] == [[0.0, 0.0], [0.1, -0.1], [0.2, -0.2]]
    args, kwargs = fake["features"]
    assert args[:4] == (str(fake["clip"]), "footsteps on gravel", "model_dict", "cfg")
    assert kwargs == {"neg_prompt": None}
    dargs, dkwargs = fake["denoise"]
    assert dargs == ("visual", "text", 3.0, "model_dict", "cfg")
    assert dkwargs == {"guidance_scale": 4.5, "num_inference_steps": 50}
    assert fake["seeds"] == [("numpy", 7), ("torch", 7)]
    assert fake["hub"][-1] == (
        "ClapTextModelWithProjection",
        str(fake["root"] / "larger_clap_general"),
        True,
    )


@pytest.mark.parametrize("video", [None, "", "missing.mp4"])
def test_generate_needs_a_video_on_this_host(
    fake: dict[str, Any], tmp_path: Path, video: str | None
) -> None:
    session = hunyuan_foley.load(SPEC)
    options = {} if video is None else {"video_path": str(tmp_path / video) if video else ""}
    with pytest.raises(ProviderError, match="video"):
        session.generate("x", out_path=tmp_path / "o.wav", **options)
    assert "features" not in fake


def test_close_drops_the_model(fake: dict[str, Any], tmp_path: Path) -> None:
    session = hunyuan_foley.load(SPEC)
    session.close()
    assert session.model_dict is None and session.cfg is None
    with pytest.raises(ProviderError, match="closed"):
        session.generate("x", out_path=tmp_path / "o.wav", video_path=str(fake["clip"]))


@pytest.mark.parametrize("missing", ["HunyuanVideo-Foley", "larger_clap_general", SRC_DIR])
def test_missing_install_says_run_setup(fake: dict[str, Any], missing: str) -> None:
    root: Path = fake["root"]
    target = root / missing
    for p in sorted(target.rglob("*"), reverse=True):
        p.unlink() if p.is_file() else p.rmdir()
    target.rmdir()
    with pytest.raises(ProviderError, match="spectracut setup"):
        hunyuan_foley.load(SPEC)
    assert "load" not in fake


def test_builtin_factory_resolves_this_loader(
    fake: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(host_models.entry_env("hunyuan-foley"), raising=False)
    session = host_models.load_model(SPEC)
    assert isinstance(session, hunyuan_foley.HunyuanFoleySession)


def test_module_imports_no_torch() -> None:
    code = (
        "import sys, spectracut_host.loaders.hunyuan_foley; "
        "assert 'torch' not in sys.modules and 'hunyuanvideo_foley' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
