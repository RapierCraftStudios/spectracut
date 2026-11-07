# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sound-effect loaders (ezaudio, moss-sfx) with fake pipelines: no torch, no weights."""

from __future__ import annotations

import os
import struct
import subprocess
import sys
import types
import wave
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from spectracut_host.loaders import ezaudio, moss_sfx
from spectracut_host.models import entry_env, load_model

from spectracut.providers.base import ProviderError
from spectracut.providers.local.registry import MODELS
from spectracut.setup.model_assets import MODEL_ROOT_ENV

EZ = MODELS["ezaudio-xl"]
MOSS = MODELS["moss-sfx-8b-nf4"]
SR = 100


class Arr(list[float]):
    """Just enough of a 1-D numpy array for the loaders."""

    dtype = "float32"
    ndim = 1

    @property
    def shape(self) -> tuple[int]:
        return (len(self),)

    def __getitem__(self, key: Any) -> Any:
        got = super().__getitem__(key)
        return Arr(got) if isinstance(key, slice) else got


def _fake_numpy() -> types.ModuleType:
    np = types.ModuleType("numpy")
    np.zeros = lambda shape, dtype=None: Arr([0.0] * shape[0])  # type: ignore[attr-defined]
    np.concatenate = lambda parts: Arr(x for p in parts for x in p)  # type: ignore[attr-defined]
    return np


def _fake_soundfile(calls: list[dict[str, Any]]) -> types.ModuleType:
    sf = types.ModuleType("soundfile")

    def write(file: str, data: Arr, samplerate: int, format: str, subtype: str) -> None:
        calls.append({"file": file, "format": format, "subtype": subtype, "n": len(data)})
        with wave.open(file, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(samplerate)
            w.writeframes(b"".join(struct.pack("<h", int(x * 32767)) for x in data))

    sf.write = write  # type: ignore[attr-defined]
    return sf


class FakeTorch(types.ModuleType):
    def __init__(self) -> None:
        super().__init__("torch")
        self.seeds: list[int] = []
        self.cudnn_sdp: list[bool] = []
        self.bfloat16 = "bf16"
        self.cuda = types.SimpleNamespace(is_available=lambda: False, empty_cache=lambda: None)
        self.backends = types.SimpleNamespace(
            cuda=types.SimpleNamespace(enable_cudnn_sdp=self.cudnn_sdp.append)
        )

    def manual_seed(self, seed: int) -> None:
        self.seeds.append(seed)

    @contextmanager
    def no_grad(self) -> Iterator[None]:
        yield


@pytest.fixture
def fakes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    calls: list[dict[str, Any]] = []
    torch = FakeTorch()
    monkeypatch.setitem(sys.modules, "numpy", _fake_numpy())
    monkeypatch.setitem(sys.modules, "soundfile", _fake_soundfile(calls))
    monkeypatch.setitem(sys.modules, "torch", torch)
    root = tmp_path / "models"
    monkeypatch.setenv(MODEL_ROOT_ENV, str(root))
    for spec in (EZ, MOSS):
        monkeypatch.delenv(entry_env(spec.family), raising=False)
    return {"calls": calls, "torch": torch, "root": root}


def _read(path: Path) -> tuple[int, int]:
    with wave.open(str(path), "rb") as w:
        return w.getframerate(), w.getnframes()


# --- ezaudio ---------------------------------------------------------------------------


class FakeEzAudio:
    instances: list[FakeEzAudio] = []

    def __init__(self, model_name: str, ckpt_path: str, vae_path: str, device: str) -> None:
        self.model_name, self.ckpt_path, self.vae_path = model_name, ckpt_path, vae_path
        self.cwd = os.getcwd()
        self.params = sys.modules["api.ezaudio"].load_yaml_with_includes("ckpts/ezaudio-xl.yml")
        self.calls: list[dict[str, Any]] = []
        FakeEzAudio.instances.append(self)

    def generate_audio(self, text: str, **kw: Any) -> tuple[int, Arr]:
        self.calls.append({"text": text, **kw})
        return SR, Arr([0.5] * (kw["length"] * SR))


def _install_ezaudio(root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base = root / "ezaudio" / "ezaudio-xl"
    (base / "src").mkdir(parents=True)
    (base / "flan-t5-xl").mkdir()
    for rel in (ezaudio.CKPT, ezaudio.VAE):
        (base / "EzAudio" / rel).parent.mkdir(parents=True, exist_ok=True)
        (base / "EzAudio" / rel).write_bytes(b"x")
    api = types.ModuleType("api")
    mod = types.ModuleType("api.ezaudio")
    mod.EzAudio = FakeEzAudio  # type: ignore[attr-defined]
    mod.load_yaml_with_includes = lambda name: {  # type: ignore[attr-defined]
        "name": name,
        "text_encoder": {"model": "google/flan-t5-xl"},
    }
    monkeypatch.setitem(sys.modules, "api", api)
    monkeypatch.setitem(sys.modules, "api.ezaudio", mod)
    FakeEzAudio.instances.clear()
    return base


def test_ezaudio_loads_installed_weights_and_writes_a_fitted_wav(
    fakes: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = _install_ezaudio(fakes["root"], monkeypatch)
    cwd = os.getcwd()
    session = load_model(EZ)
    assert isinstance(session, ezaudio.EzAudioSession)
    ez = FakeEzAudio.instances[0]
    assert ez.model_name == "s3_xl" and ez.cwd == str(base / "src") and os.getcwd() == cwd
    assert ez.ckpt_path == str(base / "EzAudio" / "ckpts/s3/ezaudio_s3_xl.pt")
    assert ez.vae_path == str(base / "EzAudio" / "ckpts/vae/1m.pt")
    assert ez.params["text_encoder"]["model"] == str(base / "flan-t5-xl")
    assert sys.modules["api.ezaudio"].load_yaml_with_includes("x")["text_encoder"]["model"] == (
        "google/flan-t5-xl"
    )
    assert str(base / "src") in sys.path

    out = session.generate("door creak", out_path=tmp_path / "out-1", duration_s=2.5, seed=7)
    assert _read(out) == (SR, 250)  # 3 s generated, trimmed to 2.5 s
    assert ez.calls[-1]["length"] == 3 and ez.calls[-1]["random_seed"] == 7
    assert fakes["calls"][-1]["format"] == "WAV" and fakes["calls"][-1]["subtype"] == "PCM_16"

    out = session.generate("long hum", out_path=tmp_path / "out-2", duration_s=12, seed=1)
    assert _read(out) == (SR, 1200)  # capped at 10 s, padded with silence
    assert ez.calls[-1]["length"] == ezaudio.MAX_SECONDS

    session.close()
    assert session.model is None and str(base / "src") not in sys.path


def test_ezaudio_missing_install_says_run_setup(
    fakes: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ProviderError, match="source not installed.*spectracut setup"):
        load_model(EZ)
    base = _install_ezaudio(fakes["root"], monkeypatch)
    (base / "EzAudio" / ezaudio.VAE).unlink()
    with pytest.raises(ProviderError, match="weights not installed.*1m.pt.*spectracut setup"):
        load_model(EZ)


# --- moss-sfx --------------------------------------------------------------------------


class Codes:
    def __init__(self, n: int) -> None:
        self.n = n

    def float(self) -> Codes:
        return self

    def cpu(self) -> Codes:
        return self

    def numpy(self) -> Arr:
        return Arr([0.25] * self.n)


class Moved:
    def __init__(self, value: str) -> None:
        self.value = value

    def to(self, device: str) -> str:
        return device


class Tokenizer:
    def __init__(self, log: list[str]) -> None:
        self.device, self.log = "cuda", log

    def to(self, device: str) -> Tokenizer:
        self.device = device
        self.log.append(device)
        return self


class FakeProcessor:
    def __init__(self, path: str, **kw: Any) -> None:
        self.path, self.kw = path, kw
        self.moves: list[str] = []
        self.audio_tokenizer = Tokenizer(self.moves)
        self.model_config = types.SimpleNamespace(sampling_rate=SR)
        self.decoded_on: list[str] = []

    def build_user_message(self, ambient_sound: str) -> dict[str, str]:
        return {"ambient_sound": ambient_sound}

    def __call__(self, conversations: Any, mode: str) -> dict[str, Moved]:
        self.last = (conversations, mode)
        return {"input_ids": Moved("ids"), "attention_mask": Moved("mask")}

    def decode(self, out: Any) -> list[Any]:
        self.decoded_on.append(self.audio_tokenizer.device)
        return [types.SimpleNamespace(audio_codes_list=[Codes(8 * SR)])]


class FakeModel:
    def __init__(self, path: str, **kw: Any) -> None:
        self.path, self.kw = path, kw
        self.generated: list[dict[str, Any]] = []

    def eval(self) -> FakeModel:
        return self

    def generate(self, **kw: Any) -> str:
        self.generated.append(kw)
        return "tokens"


def _install_moss(root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    snap = root / "moss-sfx" / "moss-sfx-8b-nf4" / "MOSS-SoundEffect"
    snap.mkdir(parents=True)
    tf = types.ModuleType("transformers")
    tf.AutoProcessor = types.SimpleNamespace(from_pretrained=FakeProcessor)  # type: ignore[attr-defined]
    tf.AutoModel = types.SimpleNamespace(from_pretrained=FakeModel)  # type: ignore[attr-defined]
    tf.BitsAndBytesConfig = lambda **kw: kw  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "transformers", tf)
    return snap


def test_moss_loads_nf4_from_the_snapshot_and_offloads_the_tokenizer(
    fakes: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snap = _install_moss(fakes["root"], monkeypatch)
    session = load_model(MOSS)
    assert isinstance(session, moss_sfx.MossSfxSession)
    proc, model = session.processor, session.model
    assert proc.path == model.path == str(snap)
    assert proc.kw == {"trust_remote_code": True, "local_files_only": True}
    assert model.kw["quantization_config"] == {
        "load_in_4bit": True,
        "bnb_4bit_quant_type": "nf4",
        "bnb_4bit_compute_dtype": "bf16",
    }
    assert model.kw["device_map"] == {"": 0} and model.kw["local_files_only"] is True
    assert fakes["torch"].cudnn_sdp == [False]
    assert proc.audio_tokenizer.device == "cpu"

    out = session.generate("whoosh", out_path=tmp_path / "out-1", duration_s=3, seed=11)
    assert _read(out) == (SR, 300)  # 8 s decoded, trimmed to 3 s
    assert fakes["torch"].seeds == [11]
    assert model.generated[-1]["max_new_tokens"] == moss_sfx.MAX_NEW_TOKENS
    assert proc.last == ([[{"ambient_sound": "whoosh"}]], "generation")
    assert proc.decoded_on == ["cuda"] and proc.audio_tokenizer.device == "cpu"

    out = session.generate("rain", out_path=tmp_path / "out-2", duration_s=9.5)
    assert _read(out) == (SR, 950)  # padded with silence
    assert fakes["calls"][-1]["format"] == "WAV"

    session.close()
    assert session.model is None and session.processor is None


def test_moss_tokenizer_returns_to_cpu_when_decode_fails(
    fakes: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_moss(fakes["root"], monkeypatch)
    session = load_model(MOSS)
    assert isinstance(session, moss_sfx.MossSfxSession)

    def boom(out: Any) -> None:
        raise RuntimeError("decode failed")

    session.processor.decode = boom
    with pytest.raises(RuntimeError, match="decode failed"):
        session.generate("x", out_path=tmp_path / "o")
    assert session.processor.audio_tokenizer.device == "cpu"


def test_moss_missing_weights_says_run_setup(fakes: dict[str, Any]) -> None:
    with pytest.raises(ProviderError, match="weights not installed.*spectracut setup"):
        load_model(MOSS)


def test_sfx_loader_modules_import_no_torch() -> None:
    code = (
        "import sys, spectracut_host.loaders.ezaudio, spectracut_host.loaders.moss_sfx\n"
        "bad = {'torch', 'transformers', 'soundfile', 'numpy'} & set(sys.modules)\n"
        "assert not bad, bad\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
