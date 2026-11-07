# SPDX-License-Identifier: AGPL-3.0-or-later
"""The llama-cpp critic loader, with a fake process and a fake nvidia-smi."""

from __future__ import annotations

import base64
import io
import json
import shutil
import sys
from contextlib import nullcontext
from pathlib import Path
from typing import IO, cast

import pytest
from spectracut_host import models as host_models
from spectracut_host.loaders import llama_cpp
from spectracut_host.loaders._base import GIB
from spectracut_host.loaders.llama_cpp import LlamaCppSession, load
from spectracut_host.models import FACTORIES, load_model, serve

from spectracut.providers.base import ProviderError
from spectracut.providers.local.registry import MODELS
from spectracut.providers.local.runtime import ModelSession, ModelSpec
from spectracut.setup.model_assets import MODEL_ROOT_ENV

SPEC = MODELS["qwen2.5-omni-7b-q4"]
WAV = b"RIFFfakewav"


class FakeProc:
    def __init__(self, code: int, polls: int = 1) -> None:
        self.returncode: int | None = None
        self._code, self._polls, self.killed = code, polls, False

    def poll(self) -> int | None:
        if self.killed:
            return self.returncode
        if self._polls <= 0:
            self.returncode = self._code
            return self.returncode
        self._polls -= 1
        return None

    def kill(self) -> None:
        self.killed, self.returncode = True, -9

    def wait(self) -> int:
        return -9


class Spawner:
    def __init__(
        self, stdout: str = " a clean clip \n", code: int = 0, stderr: str = "", polls: int = 1
    ) -> None:
        self.stdout, self.code, self.stderr, self.polls = stdout, code, stderr, polls
        self.argv: list[str] = []
        self.audio: bytes | None = None

    def __call__(self, argv: list[str], out: IO[bytes], err: IO[bytes]) -> FakeProc:
        self.argv = argv
        self.audio = Path(argv[argv.index("--audio") + 1]).read_bytes()
        out.write(self.stdout.encode())
        err.write(self.stderr.encode())
        return FakeProc(self.code, self.polls)


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(MODEL_ROOT_ENV, str(tmp_path / "root"))
    cli = tmp_path / "root" / "cli"
    cli.parent.mkdir(parents=True)
    cli.write_bytes(b"")
    monkeypatch.setenv(llama_cpp.BIN_ENV, str(cli))
    w = tmp_path / "root" / "llama-cpp" / SPEC.id / "Qwen2.5-Omni-7B-GGUF"
    w.mkdir(parents=True)
    (w / "Qwen2.5-Omni-7B-Q4_K_M.gguf").write_bytes(b"g")
    (w / "mmproj-Qwen2.5-Omni-7B-Q8_0.gguf").write_bytes(b"m")
    return tmp_path / "root"


def session(root: Path, spawn: Spawner, smi: list[int | None] | None = None) -> LlamaCppSession:
    values = iter(smi or [])
    s = load(SPEC)
    s._spawn = spawn
    s._smi = lambda: next(values, None)
    s._sample_s = 0
    return s


def test_factory_resolves_to_this_module() -> None:
    assert FACTORIES["llama-cpp"] == "spectracut_host.loaders.llama_cpp:load"
    assert host_models.FACTORIES["llama-cpp"].startswith(llama_cpp.__name__.rsplit(".", 1)[0])


def test_argv_text_and_options(root: Path, tmp_path: Path) -> None:
    wav = tmp_path / "in.wav"
    wav.write_bytes(WAV)
    sp = Spawner()
    s = session(root, sp)
    out = s.generate("rate it", out_path=tmp_path / "o" / "c.txt", audio_path=str(wav), ngl=10)
    assert out.read_text() == "a clean clip"
    a = sp.argv
    assert a[a.index("-m") + 1].endswith("Q4_K_M.gguf")
    assert a[a.index("--mmproj") + 1].endswith("Q8_0.gguf")
    assert a[a.index("--audio") + 1] == str(wav)
    assert a[a.index("-p") + 1] == "rate it"
    assert [a[a.index(f) + 1] for f in ("-ngl", "-c", "--temp", "-n")] == [
        "10",
        "8192",
        "0.2",
        "200",
    ]
    assert wav.exists()


def test_audio_b64_is_a_temp_file_removed_afterwards(root: Path, tmp_path: Path) -> None:
    sp = Spawner()
    out_dir = tmp_path / "o"
    session(root, sp).generate(
        "p", out_path=out_dir / "c.txt", audio_b64=base64.b64encode(WAV).decode()
    )
    assert sp.audio == WAV
    assert not Path(sp.argv[sp.argv.index("--audio") + 1]).exists()
    assert [p.name for p in out_dir.iterdir()] == ["c.txt"]


@pytest.mark.parametrize(
    ("spawner", "options", "match"),
    [
        (Spawner(code=2, stderr="boom"), {"audio_b64": "AAAA"}, "exited 2: boom"),
        (Spawner(stdout="  \n"), {"audio_b64": "AAAA"}, "no text"),
        (Spawner(polls=10**9), {"audio_b64": "AAAA", "timeout": 0}, "timed out"),
        (Spawner(), {}, "audio_b64 or an existing audio_path"),
        (Spawner(), {"audio_path": "/nope.wav"}, "audio_b64 or an existing audio_path"),
        (Spawner(), {"audio_b64": "!!"}, "bad audio_b64"),
    ],
)
def test_failures_raise_provider_error(
    root: Path, tmp_path: Path, spawner: Spawner, options: dict[str, object], match: str
) -> None:
    with pytest.raises(ProviderError, match=match):
        session(root, spawner).generate("p", out_path=tmp_path / "c.txt", **options)


def test_missing_weights_and_binary_say_run_setup(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _n: None)
    monkeypatch.delenv(llama_cpp.BIN_ENV)
    with pytest.raises(ProviderError, match=r"llama-mtmd-cli not found.*spectracut setup"):
        load(SPEC)
    next((root / "llama-cpp").rglob("mmproj*")).unlink()
    with pytest.raises(ProviderError, match=r"missing: run `spectracut setup`"):
        load(SPEC)
    monkeypatch.setenv(MODEL_ROOT_ENV, str(root / "empty"))
    with pytest.raises(ProviderError, match="spectracut setup"):
        load(SPEC)


def test_binary_resolution_order(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(llama_cpp.BIN_ENV)
    monkeypatch.setattr(shutil, "which", lambda _n: "/usr/bin/llama-mtmd-cli")
    assert load(SPEC).binary == Path("/usr/bin/llama-mtmd-cli")
    exe = root / "llama-cpp" / SPEC.id / "bin" / llama_cpp_name()
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    assert load(SPEC).binary == exe
    env = root / "env-cli"
    env.write_bytes(b"")
    monkeypatch.setenv(llama_cpp.BIN_ENV, str(env))
    assert load(SPEC).binary == env


def llama_cpp_name() -> str:
    return llama_cpp.BIN_NAME + (".exe" if sys.platform == "win32" else "")


def test_peak_vram_is_enforced(root: Path, tmp_path: Path) -> None:
    ok = session(root, Spawner(), [1000, 1000 + 7000])
    ok.generate("p", out_path=tmp_path / "a.txt", audio_b64="AAAA")
    assert ok.peak_vram_bytes() == 7000 * 1024**2
    bad = session(root, Spawner(), [1000, 1000 + 9000])
    with pytest.raises(ProviderError, match="peak VRAM .* exceeds 8 GB"):
        bad.generate("p", out_path=tmp_path / "b.txt", audio_b64="AAAA")
    assert bad.peak_vram_bytes() == 9000 * 1024**2 > 8 * GIB
    none = session(root, Spawner())
    none.generate("p", out_path=tmp_path / "c.txt", audio_b64="AAAA")
    assert none.peak_vram_bytes() is None


def test_serve_round_trip(root: Path, tmp_path: Path) -> None:
    sp = Spawner()
    lines = [
        {"op": "load", "model": SPEC.id},
        {"op": "generate", "prompt": "p", "options": {"audio_b64": base64.b64encode(WAV).decode()}},
        {"op": "close"},
    ]
    out = io.StringIO()
    inp = io.StringIO("".join(json.dumps(x) + "\n" for x in lines))

    def loader(spec: ModelSpec) -> ModelSession:
        return cast(ModelSession, session(root, sp))

    assert serve(inp, out, loader, workdir=tmp_path, lock=nullcontext) == 0
    replies = [json.loads(x) for x in out.getvalue().splitlines()]
    assert all(r["ok"] for r in replies)
    assert base64.b64decode(replies[1]["data"]) == b"a clean clip"
    assert load_model.__module__ == "spectracut_host.models"
