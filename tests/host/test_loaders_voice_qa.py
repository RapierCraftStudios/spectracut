# SPDX-License-Identifier: AGPL-3.0-or-later
"""Kokoro, faster-whisper and CLAP loaders (#300) with fake model packages.

No torch, kokoro, faster-whisper or transformers is installed in the dev env (they live
in the model host's venv), so each test injects small fakes through ``sys.modules`` and
points the model root at a tmp tree laid out the way ``spectracut setup`` installs it.
"""

from __future__ import annotations

import base64
import builtins
import json
import subprocess
import sys
import types
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from spectracut_host.loaders import _audio, clap, faster_whisper, kokoro
from spectracut_host.models import load_model

from spectracut.audio.captions import transcribe
from spectracut.providers.base import ProviderError
from spectracut.providers.local.registry import MODELS
from spectracut.setup.model_assets import MODEL_ROOT_ENV

KOKORO = MODELS["kokoro-82m"]
WHISPER = MODELS["faster-whisper-large-v3-turbo"]
CLAP = MODELS["larger-clap-general"]


class FT:
    """A tiny tensor over (nested) lists: the subset the loaders use."""

    def __init__(self, data: Any) -> None:
        self.data = data

    @property
    def ndim(self) -> int:
        n, d = 1, self.data
        while d and isinstance(d[0], list):
            n, d = n + 1, d[0]
        return n

    def mean(self, dim: int) -> FT:
        assert dim == 1 and self.ndim == 2
        return FT([sum(row) / len(row) for row in self.data])

    def contiguous(self) -> FT:
        return self

    def float(self) -> FT:
        return self

    def cpu(self) -> FT:
        return self

    def numpy(self) -> list[builtins.float]:
        return [float(x) for x in self.data]

    def tolist(self) -> Any:
        return self.data

    def __getitem__(self, i: int) -> FT:
        return FT(self.data[i])


class Fakes:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[tuple[str, Any]] = []
        self.read_result: tuple[Any, int] = ([[0.1], [0.2]], 48000)
        self.words = [("Hello", 0.0, 0.4), ("world", 0.5, 0.9)]


@contextmanager
def _no_grad() -> Iterator[None]:
    yield


def _install(monkeypatch: pytest.MonkeyPatch, f: Fakes) -> None:
    torch = types.ModuleType("torch")
    torch.cuda = SimpleNamespace(is_available=lambda: False)  # type: ignore[attr-defined]
    torch.float32 = "float32"  # type: ignore[attr-defined]

    def as_tensor(data: Any, dtype: Any = None) -> FT:
        f.calls.append(("as_tensor", dtype))
        return data if isinstance(data, FT) else FT(data)

    torch.as_tensor = as_tensor  # type: ignore[attr-defined]
    torch.no_grad = _no_grad  # type: ignore[attr-defined]

    torchaudio = types.ModuleType("torchaudio")
    functional = types.ModuleType("torchaudio.functional")

    def resample(wave: FT, orig: int, new: int) -> FT:
        f.calls.append(("resample", (list(wave.data), orig, new)))
        n = round(len(wave.data) * new / orig)
        return FT([wave.data[min(int(i * orig / new), len(wave.data) - 1)] for i in range(n)])

    functional.resample = resample  # type: ignore[attr-defined]
    torchaudio.functional = functional  # type: ignore[attr-defined]

    numpy = types.ModuleType("numpy")
    numpy.asarray = lambda a: list(a)  # type: ignore[attr-defined]
    numpy.concatenate = lambda chunks: [x for c in chunks for x in c]  # type: ignore[attr-defined]

    soundfile = types.ModuleType("soundfile")

    def write(path: str, data: Any, sr: int, **kw: Any) -> None:
        f.calls.append(("sf.write", (list(data), sr, kw)))
        Path(path).write_bytes(b"RIFF fake wav")

    def read(path: str, **kw: Any) -> tuple[Any, int]:
        f.calls.append(("sf.read", (Path(path).read_bytes(), kw)))
        return f.read_result

    soundfile.write = write  # type: ignore[attr-defined]
    soundfile.read = read  # type: ignore[attr-defined]

    kok = types.ModuleType("kokoro")

    class KModel:
        def __init__(self, **kw: Any) -> None:
            f.calls.append(("KModel", kw))

        def to(self, device: str) -> KModel:
            f.calls.append(("KModel.to", device))
            return self

        def eval(self) -> KModel:
            return self

    class KPipeline:
        def __init__(self, **kw: Any) -> None:
            f.calls.append(("KPipeline", kw))

        def __call__(self, text: str, **kw: Any) -> Iterator[tuple[str, str, Any]]:
            f.calls.append(("speak", (text, kw)))
            yield "g", "p", FT([0.1, 0.2])
            yield "g", "p", None
            yield "g", "p", [0.3]

    kok.KModel = KModel  # type: ignore[attr-defined]
    kok.KPipeline = KPipeline  # type: ignore[attr-defined]

    fw = types.ModuleType("faster_whisper")

    class WhisperModel:
        def __init__(self, path: str, **kw: Any) -> None:
            f.calls.append(("WhisperModel", (path, kw)))

        def transcribe(self, path: str, **kw: Any) -> tuple[Iterator[Any], Any]:
            f.calls.append(("transcribe", (path, kw)))

            def segments() -> Iterator[Any]:
                assert Path(path).is_file()  # still there while the generator is consumed
                words = [
                    SimpleNamespace(word=f" {w}", start=s, end=e, probability=0.9)
                    for w, s, e in f.words
                ]
                yield SimpleNamespace(text=" Hello world", words=words)

            return segments(), SimpleNamespace(language="en", duration=1.0)

    fw.WhisperModel = WhisperModel  # type: ignore[attr-defined]

    tf = types.ModuleType("transformers")

    class Inputs(dict[str, Any]):
        def to(self, device: str) -> Inputs:
            f.calls.append(("inputs.to", device))
            return self

    class ClapProcessor:
        @classmethod
        def from_pretrained(cls, path: str) -> ClapProcessor:
            f.calls.append(("ClapProcessor", path))
            return cls()

        def __call__(self, **kw: Any) -> Inputs:
            f.calls.append(("processor", kw))
            return Inputs(x=1)

    class ClapModel:
        @classmethod
        def from_pretrained(cls, path: str) -> ClapModel:
            f.calls.append(("ClapModel", path))
            return cls()

        def to(self, device: str) -> ClapModel:
            return self

        def eval(self) -> ClapModel:
            return self

        def get_audio_features(self, **kw: Any) -> FT:
            return FT([[3.0, 4.0]])

        def __call__(self, **kw: Any) -> Any:
            return SimpleNamespace(logits_per_audio=FT([[5.0, 0.0, 0.0]]))

    tf.ClapProcessor = ClapProcessor  # type: ignore[attr-defined]
    tf.ClapModel = ClapModel  # type: ignore[attr-defined]

    for name, mod in {
        "torch": torch,
        "torchaudio": torchaudio,
        "torchaudio.functional": functional,
        "numpy": numpy,
        "soundfile": soundfile,
        "kokoro": kok,
        "faster_whisper": fw,
        "transformers": tf,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)


@pytest.fixture
def fakes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Fakes:
    root = tmp_path / "models"
    kdir = root / "kokoro" / "kokoro-82m" / "Kokoro-82M"
    (kdir / "voices").mkdir(parents=True)
    for v in ("af_heart", "am_adam"):
        (kdir / "voices" / f"{v}.pt").write_bytes(b"v")
    (root / "faster-whisper" / WHISPER.id / "faster-whisper-large-v3-turbo").mkdir(parents=True)
    (root / "clap" / CLAP.id / "larger_clap_general").mkdir(parents=True)
    monkeypatch.setenv(MODEL_ROOT_ENV, str(root))
    for fam in ("KOKORO", "FASTER_WHISPER", "CLAP"):
        monkeypatch.delenv(f"SPECTRACUT_MODEL_ENTRY_{fam}", raising=False)
    f = Fakes(root)
    _install(monkeypatch, f)
    return f


def _calls(f: Fakes, name: str) -> list[Any]:
    return [c[1] for c in f.calls if c[0] == name]


def _wav(tmp_path: Path) -> Path:
    p = tmp_path / "in.wav"
    p.write_bytes(b"RIFF input")
    return p


# kokoro


def test_kokoro_writes_a_24k_wav_with_af_heart_from_local_weights(
    fakes: Fakes, tmp_path: Path
) -> None:
    s = load_model(KOKORO)
    assert isinstance(s, kokoro.KokoroSession)
    kdir = fakes.root / "kokoro" / "kokoro-82m" / "Kokoro-82M"
    assert _calls(fakes, "KModel") == [
        {
            "repo_id": "hexgrad/Kokoro-82M",
            "config": str(kdir / "config.json"),
            "model": str(kdir / "kokoro-v1_0.pth"),
        }
    ]
    assert _calls(fakes, "KPipeline")[0]["lang_code"] == "a"
    out = s.generate(" Hello there. ", out_path=tmp_path / "vo")
    assert out.read_bytes().startswith(b"RIFF")
    assert _calls(fakes, "speak") == [
        ("Hello there.", {"voice": str(kdir / "voices" / "af_heart.pt"), "speed": 1.0})
    ]
    data, sr, kw = _calls(fakes, "sf.write")[0]
    assert data == [0.1, 0.2, 0.3] and sr == 24000
    assert kw == {"format": "WAV", "subtype": "PCM_16"}
    s.generate("Hi", out_path=tmp_path / "vo2", voice="am_adam", speed=1.2)
    assert _calls(fakes, "speak")[1][1] == {
        "voice": str(kdir / "voices" / "am_adam.pt"),
        "speed": 1.2,
    }
    s.close()
    assert s.pipeline is None and s.model is None and s.closed


@pytest.mark.parametrize(
    ("prompt", "voice", "match"),
    [
        ("  ", "af_heart", "empty text"),
        ("Hi", "../../etc/passwd", "invalid voice"),
        ("Hi", "af_heart/../x", "invalid voice"),
        ("Hi", "bf_missing", "not installed"),
    ],
)
def test_kokoro_refuses_bad_input(
    fakes: Fakes, tmp_path: Path, prompt: str, voice: str, match: str
) -> None:
    s = load_model(KOKORO)
    with pytest.raises(ProviderError, match=match):
        s.generate(prompt, out_path=tmp_path / "x", voice=voice)
    assert not (tmp_path / "x").exists()


@pytest.mark.parametrize("spec", [KOKORO, WHISPER, CLAP], ids=lambda s: s.id)
def test_missing_weights_say_run_setup(
    fakes: Fakes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spec: Any
) -> None:
    monkeypatch.setenv(MODEL_ROOT_ENV, str(tmp_path / "empty"))
    with pytest.raises(ProviderError, match="spectracut setup"):
        load_model(spec)


# faster-whisper


def test_whisper_writes_word_timestamps_captions_can_read(fakes: Fakes, tmp_path: Path) -> None:
    s = load_model(WHISPER)
    assert isinstance(s, faster_whisper.WhisperSession)
    wdir = fakes.root / "faster-whisper" / WHISPER.id / "faster-whisper-large-v3-turbo"
    assert _calls(fakes, "WhisperModel") == [
        (str(wdir), {"device": "cuda", "compute_type": "float16"})
    ]
    audio = _wav(tmp_path)

    class Provider:
        kind, name = "asr", "test"

        def generate(self, prompt: str, out_path: Path, **options: Any) -> Path:
            return Path(s.generate(prompt, out_path=out_path, **options))

    words = transcribe(audio, tmp_path / "t.json", provider=Provider())  # type: ignore[arg-type]
    assert [(w.text, float(w.start_s), float(w.end_s)) for w in words] == [
        ("Hello", 0.0, 0.4),
        ("world", 0.5, 0.9),
    ]
    assert _calls(fakes, "transcribe") == [
        (str(audio), {"word_timestamps": True, "beam_size": 1, "language": None})
    ]
    data = json.loads((tmp_path / "t.json").read_text(encoding="utf-8"))
    assert data["text"] == "Hello world" and data["language"] == "en"
    assert data["duration_s"] == 1.0 and data["words"][0]["probability"] == 0.9
    s.close()
    assert s.model is None


def test_whisper_reads_audio_b64_and_refuses_missing_input(fakes: Fakes, tmp_path: Path) -> None:
    s = load_model(WHISPER)
    enc = base64.b64encode(b"RIFF bytes").decode("ascii")
    s.generate("", out_path=tmp_path / "a.json", audio_b64=enc, language="en")
    path, kw = _calls(fakes, "transcribe")[0]
    assert Path(path).name == "input-audio" and not Path(path).exists()  # temp cleaned up
    assert kw["language"] == "en"
    with pytest.raises(ProviderError, match="no input audio"):
        s.generate(str(tmp_path / "nope.wav"), out_path=tmp_path / "b.json")
    with pytest.raises(ProviderError, match="not valid base64"):
        s.generate("", out_path=tmp_path / "c.json", audio_b64="!!!")
    with pytest.raises(ProviderError, match="language"):
        s.generate(str(_wav(tmp_path)), out_path=tmp_path / "d.json", language=3)


def test_input_audio_prefers_b64(tmp_path: Path) -> None:
    enc = base64.b64encode(b"abc").decode("ascii")
    p = _audio.input_audio("ignored", {"audio_b64": enc}, tmp_path, model="m")
    assert p.read_bytes() == b"abc"
    with pytest.raises(ProviderError, match="m: audio_b64 is empty"):
        _audio.input_audio("", {"audio_b64": ""}, tmp_path, model="m")
    with pytest.raises(ProviderError, match="base64 string"):
        _audio.input_audio("", {"audio_b64": 1}, tmp_path, model="m")


# CLAP


def test_to_mono_48k_downmixes_then_resamples(fakes: Fakes) -> None:
    stereo_44k = [[1.0, 0.0], [0.0, 1.0], [0.5, 0.5], [1.0, 1.0]]
    out = clap.to_mono_48k(stereo_44k, 44100)
    # channel mean first, then one resample of the 1-D mono signal from 44.1 to 48 kHz
    assert _calls(fakes, "resample") == [([0.5, 0.5, 0.5, 1.0], 44100, 48000)]
    assert _calls(fakes, "as_tensor") == ["float32"]
    assert out == [0.5, 0.5, 0.5, 0.5]  # 4 * 48000 / 44100 rounds to 4 samples, all float
    assert all(isinstance(x, float) for x in out)


def test_to_mono_48k_keeps_48k_mono_and_rejects_3d(fakes: Fakes) -> None:
    assert clap.to_mono_48k([[0.25], [0.75]], 48000) == [0.25, 0.75]
    assert _calls(fakes, "resample") == []
    with pytest.raises(ValueError, match="dims"):
        clap.to_mono_48k(FT([[[1.0]]]), 48000)


def test_clap_writes_embedding_and_caption_scores(fakes: Fakes, tmp_path: Path) -> None:
    s = load_model(CLAP)
    assert isinstance(s, clap.ClapSession)
    cdir = fakes.root / "clap" / CLAP.id / "larger_clap_general"
    assert _calls(fakes, "ClapModel") == [str(cdir)] == _calls(fakes, "ClapProcessor")
    fakes.read_result = ([[1.0, 0.0], [0.0, 1.0]], 44100)
    audio = _wav(tmp_path)
    s.generate(str(audio), out_path=tmp_path / "e.json")
    data = json.loads((tmp_path / "e.json").read_text(encoding="utf-8"))
    assert data == {"sample_rate": 48000, "embedding": [0.6, 0.8]}
    assert _calls(fakes, "sf.read")[0][1] == {"always_2d": True, "dtype": "float32"}
    proc = _calls(fakes, "processor")[0]
    assert proc["sampling_rate"] == 48000 and proc["audio"] == [0.5, 0.5]
    assert _calls(fakes, "resample")[0][1:] == (44100, 48000)

    caps = ["a person speaking", "an explosion", "piano music"]
    s.generate(str(audio), out_path=tmp_path / "s.json", captions=caps)
    data = json.loads((tmp_path / "s.json").read_text(encoding="utf-8"))
    assert data["top"] == "a person speaking"
    assert list(data["scores"]) == caps
    assert abs(sum(data["scores"].values()) - 1.0) < 1e-9
    assert _calls(fakes, "processor")[-1]["text"] == caps
    s.close()
    assert s.model is None and s.processor is None


@pytest.mark.parametrize("captions", [[], "a person speaking", [1], [" "]])
def test_clap_refuses_bad_captions(fakes: Fakes, tmp_path: Path, captions: Any) -> None:
    s = load_model(CLAP)
    with pytest.raises(ProviderError, match="captions"):
        s.generate(str(_wav(tmp_path)), out_path=tmp_path / "x.json", captions=captions)


def test_loader_modules_import_no_model_packages() -> None:
    code = (
        "import sys\n"
        "import spectracut_host.loaders.kokoro, spectracut_host.loaders.faster_whisper\n"
        "import spectracut_host.loaders.clap\n"
        "heavy = {'torch', 'torchaudio', 'kokoro', 'faster_whisper', 'transformers',"
        " 'numpy', 'soundfile'}\n"
        "print(sorted(heavy & set(sys.modules)))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout
    assert out.strip() == "[]"
