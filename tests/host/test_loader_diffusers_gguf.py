# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Z-Image-Turbo GGUF loader (family diffusers-gguf): fakes, plus a legion smoke."""

from __future__ import annotations

import os
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest
from spectracut_host.loaders import diffusers_gguf
from spectracut_host.models import load_model

from spectracut.providers.base import ProviderError
from spectracut.providers.local.registry import MODELS
from spectracut.setup.model_assets import MODEL_ROOT_ENV, asset_for

SPEC = MODELS["z-image-turbo-q4"]
PNG = b"\x89PNG\r\n\x1a\n"


class FakeImage:
    def save(self, path: str, format: str | None = None) -> None:
        assert format == "PNG"
        Path(path).write_bytes(PNG + b"fake")


class FakePipe:
    def __init__(self, calls: dict[str, Any]) -> None:
        self.calls = calls

    def enable_model_cpu_offload(self) -> None:
        self.calls["offload"] = True

    def __call__(self, prompt: str, **kw: Any) -> Any:
        self.calls["run"] = {"prompt": prompt, **kw}
        return types.SimpleNamespace(images=[FakeImage()])


@pytest.fixture
def fakes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    calls: dict[str, Any] = {}
    monkeypatch.setenv(MODEL_ROOT_ENV, str(tmp_path))
    root = tmp_path / "diffusers-gguf" / SPEC.id
    (root / "Z-Image-Turbo-GGUF").mkdir(parents=True)
    (root / "Z-Image-Turbo-GGUF" / diffusers_gguf.GGUF_FILE).write_bytes(b"gguf")
    (root / "Z-Image-Turbo").mkdir()
    calls["base"], calls["gguf"] = (
        str(root / "Z-Image-Turbo"),
        str(root / "Z-Image-Turbo-GGUF" / diffusers_gguf.GGUF_FILE),
    )

    class Gen:
        def __init__(self, device: str) -> None:
            calls["device"] = device

        def manual_seed(self, seed: int) -> Gen:
            calls["seed"] = seed
            return self

    class Transformer:
        @staticmethod
        def from_single_file(path: str, **kw: Any) -> str:
            calls["transformer"] = {"path": path, **kw}
            return "transformer"

    def from_pretrained(path: str, **kw: Any) -> FakePipe:
        calls["pipeline"] = {"path": path, **kw}
        return FakePipe(calls)

    torch = types.SimpleNamespace(
        bfloat16="bf16", Generator=Gen, cuda=types.SimpleNamespace(is_available=lambda: False)
    )
    diffusers = types.SimpleNamespace(
        ZImageTransformer2DModel=Transformer,
        ZImagePipeline=types.SimpleNamespace(from_pretrained=from_pretrained),
        GGUFQuantizationConfig=lambda compute_dtype: ("q", compute_dtype),
    )
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "diffusers", diffusers)
    return calls


def test_load_builds_the_gguf_pipeline_from_local_weights(fakes: dict[str, Any]) -> None:
    session = diffusers_gguf.load(SPEC)
    assert fakes["transformer"] == {
        "path": fakes["gguf"],
        "quantization_config": ("q", "bf16"),
        "torch_dtype": "bf16",
        "config": fakes["base"],
        "subfolder": "transformer",
    }
    assert fakes["pipeline"]["path"] == fakes["base"]
    assert fakes["pipeline"]["transformer"] == "transformer"
    assert fakes["offload"] is True
    session.close()


def test_generate_writes_a_png_with_defaults_and_options(
    fakes: dict[str, Any], tmp_path: Path
) -> None:
    session = diffusers_gguf.load(SPEC)
    out = session.generate("a watch", out_path=tmp_path / "o" / "out-1")  # no suffix
    assert out.read_bytes().startswith(PNG)
    run = fakes["run"]
    assert (run["num_inference_steps"], run["guidance_scale"]) == (9, 0.0)
    assert (run["width"], run["height"], fakes["seed"], fakes["device"]) == (1024, 1024, 42, "cpu")
    session.generate("x", out_path=tmp_path / "b", width=512, height=768, steps=4, seed=7)
    run = fakes["run"]
    assert (run["width"], run["height"], run["num_inference_steps"], fakes["seed"]) == (
        512,
        768,
        4,
        7,
    )
    with pytest.raises(ProviderError, match="multiple of 16"):
        session.generate("x", out_path=tmp_path / "c", width=1000)
    session.close()


def test_missing_weights_say_run_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MODEL_ROOT_ENV, str(tmp_path))
    with pytest.raises(ProviderError, match="spectracut setup"):
        diffusers_gguf.load(SPEC)
    root = tmp_path / "diffusers-gguf" / SPEC.id
    (root / "Z-Image-Turbo-GGUF").mkdir(parents=True)
    (root / "Z-Image-Turbo").mkdir()
    with pytest.raises(ProviderError, match="spectracut setup"):  # GGUF file absent
        diffusers_gguf.load(SPEC)


def test_close_unloads_and_blocks_generate(fakes: dict[str, Any], tmp_path: Path) -> None:
    session = diffusers_gguf.load(SPEC)
    session.close()
    assert session.pipe is None and session.closed
    with pytest.raises(ProviderError, match="closed"):
        session.generate("x", out_path=tmp_path / "z")


def test_import_pulls_no_torch() -> None:
    code = (
        "import sys, spectracut_host.loaders.diffusers_gguf;"
        "sys.exit('torch' in sys.modules or 'diffusers' in sys.modules)"
    )
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0


def test_setup_fetches_the_transformer_config() -> None:
    base = next(w for w in asset_for(SPEC.id).weights if w.repo == diffusers_gguf.BASE_REPO)
    assert "transformer/config.json" in base.include


def test_builtin_factory_resolves_to_this_loader(
    fakes: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SPECTRACUT_MODEL_ENTRY_DIFFUSERS_GGUF", raising=False)
    session = load_model(SPEC)
    assert isinstance(session, diffusers_gguf.ZImageSession)
    session.close()


@pytest.mark.e2e
@pytest.mark.skipif(os.environ.get("SPECTRACUT_E2E") != "1", reason="set SPECTRACUT_E2E=1")
def test_smoke_on_model_host(tmp_path: Path) -> None:
    session = diffusers_gguf.load(SPEC)
    try:
        out = session.generate("a smartwatch on slate, soft rim light", out_path=tmp_path / "i")
        data = out.read_bytes()
        assert data.startswith(PNG)
        peak = session.peak_vram_bytes()
        assert peak is None or peak <= 8 * 1024**3
    finally:
        session.close()
