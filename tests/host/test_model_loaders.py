# SPDX-License-Identifier: AGPL-3.0-or-later
"""Built-in loader table, env override, host-wide model lock and the shared session base."""

from __future__ import annotations

import io
import json
import sys
import types
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from spectracut_host import models as host_models
from spectracut_host.loaders import _base
from spectracut_host.loaders._base import MAX_VRAM_GB, BaseSession, check_vram_spec, weights_dir
from spectracut_host.models import (
    FACTORIES,
    LOCK_NAME,
    entry_env,
    load_model,
    model_lock,
    serve,
)

from spectracut.providers.base import ProviderError
from spectracut.providers.local.registry import MODELS
from spectracut.providers.local.runtime import ModelSpec
from spectracut.setup.model_assets import MODEL_ROOT_ENV

KOKORO = MODELS["kokoro-82m"]


def test_factory_table_covers_every_registry_family() -> None:
    assert set(FACTORIES) == {m.family for m in MODELS.values()}
    assert FACTORIES["moss-sfx"] == "spectracut_host.loaders.moss_sfx:load"
    assert FACTORIES["diffusers-gguf"] == "spectracut_host.loaders.diffusers_gguf:load"
    assert all(
        v.startswith("spectracut_host.loaders.") and v.endswith(":load") for v in FACTORIES.values()
    )


def test_builtin_factory_is_used_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = types.ModuleType("spectracut_host.loaders.kokoro")
    mod.load = lambda spec: ("builtin", spec)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "spectracut_host.loaders.kokoro", mod)
    monkeypatch.delenv(entry_env("kokoro"), raising=False)
    assert load_model(KOKORO) == ("builtin", KOKORO)  # type: ignore[comparison-overlap]


def test_env_override_wins_over_builtin(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = types.ModuleType("spectracut_host.loaders.kokoro")
    mod.load = lambda spec: "builtin"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "spectracut_host.loaders.kokoro", mod)
    monkeypatch.setenv(entry_env("kokoro"), f"{__name__}:override")
    assert load_model(KOKORO) == "override"  # type: ignore[comparison-overlap]
    monkeypatch.setenv(entry_env("kokoro"), "bad entry")
    with pytest.raises(ProviderError, match="module:factory"):
        load_model(KOKORO)


def override(spec: ModelSpec) -> str:
    return "override"


def test_missing_family_module_says_run_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(entry_env("clap"), raising=False)
    spec = MODELS["larger-clap-general"]
    with pytest.raises(
        ProviderError, match=r"family clap not installed on this host.*spectracut setup"
    ):
        load_model(spec)


def test_unknown_family_keeps_no_loader_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(entry_env("nope"), raising=False)
    with pytest.raises(ProviderError, match="no loader for nope"):
        load_model(ModelSpec("x", "sfx", "nope", 2))


def test_model_lock_excludes_a_second_holder(tmp_path: Path) -> None:
    path = tmp_path / LOCK_NAME
    with model_lock(path):
        with pytest.raises(ProviderError, match="model host busy"):
            with model_lock(path, timeout=0.3, poll=0.05):
                pass
    with model_lock(path, timeout=0.3):
        pass  # released on exit


def test_model_lock_defaults_to_the_model_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(MODEL_ROOT_ENV, str(tmp_path / "root"))
    with model_lock():
        assert (tmp_path / "root" / LOCK_NAME).exists()


class Session:
    def __init__(self, spec: ModelSpec, log: list[str]) -> None:
        self.spec, self.log = spec, log
        log.append(f"load {spec.id}")

    def generate(self, prompt: str, *, out_path: Path, **options: Any) -> Path:
        out_path.write_bytes(b"x")
        return out_path

    def close(self) -> None:
        self.log.append(f"close {self.spec.id}")


def test_serve_holds_the_lock_from_load_until_close() -> None:
    log: list[str] = []

    @contextmanager
    def lock() -> Iterator[None]:
        log.append("lock")
        try:
            yield
        finally:
            log.append("unlock")

    inp = io.StringIO(
        '{"op": "load", "model": "kokoro-82m"}\n'
        '{"op": "load", "model": "larger-clap-general"}\n'
        '{"op": "close"}\n'
    )
    out = io.StringIO()
    assert serve(inp, out, lambda s: Session(s, log), lock=lock) == 0  # type: ignore[arg-type,return-value]
    assert [json.loads(r)["ok"] for r in out.getvalue().splitlines()] == [True, True, True]
    assert log == [
        "lock",
        "load kokoro-82m",
        "close kokoro-82m",
        "unlock",
        "lock",
        "load larger-clap-general",
        "close larger-clap-general",
        "unlock",
    ]


def test_serve_releases_the_lock_when_load_fails_and_on_eof(tmp_path: Path) -> None:
    path = tmp_path / LOCK_NAME

    def boom(spec: ModelSpec) -> Any:
        raise RuntimeError("no gpu")

    out = io.StringIO()
    serve(
        io.StringIO('{"op": "load", "model": "kokoro-82m"}\n'),
        out,
        boom,
        lock=lambda: model_lock(path),
    )
    assert "no gpu" in json.loads(out.getvalue())["error"]
    with model_lock(path, timeout=0.2):
        pass
    serve(
        io.StringIO('{"op": "load", "model": "kokoro-82m"}\n'),
        io.StringIO(),
        lambda s: Session(s, []),  # type: ignore[arg-type,return-value]
        lock=lambda: model_lock(path),
    )
    with model_lock(path, timeout=0.2):
        pass  # EOF closed the model and released the lock


def test_serve_reports_busy_host(tmp_path: Path) -> None:
    path = tmp_path / LOCK_NAME
    out = io.StringIO()
    with model_lock(path):
        serve(
            io.StringIO('{"op": "load", "model": "kokoro-82m"}\n'),
            out,
            lambda s: Session(s, []),  # type: ignore[arg-type,return-value]
            lock=lambda: model_lock(path, timeout=0.2, poll=0.05),
        )
    assert "model host busy" in json.loads(out.getvalue())["error"]


class Writes(BaseSession):
    def __init__(self, spec: ModelSpec, write: bool = True) -> None:
        super().__init__(spec)
        self.write, self.unloaded = write, False

    def _generate(self, prompt: str, out_path: Path, **options: Any) -> None:
        if self.write:
            out_path.write_text(prompt)

    def _unload(self) -> None:
        self.unloaded = True


def test_base_session_writes_out_path_and_closes(tmp_path: Path) -> None:
    s = Writes(KOKORO)
    out = s.generate("hi", out_path=tmp_path / "a" / "b.wav")
    assert out.read_text() == "hi"
    s.close()
    assert s.unloaded and s.closed
    with pytest.raises(ProviderError, match="closed"):
        s.generate("x", out_path=tmp_path / "c.wav")
    with pytest.raises(ProviderError, match="no output"):
        Writes(KOKORO, write=False).generate("x", out_path=tmp_path / "d.wav")


def test_vram_guards(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert MAX_VRAM_GB == 8
    with pytest.raises(ProviderError, match="needs 12 GB"):
        check_vram_spec(ModelSpec("big", "sfx", "ezaudio", 12))
    s = Writes(KOKORO)  # declares 2 GB
    monkeypatch.setattr(s, "peak_vram_bytes", lambda: 3 * _base.GIB)
    with pytest.raises(ProviderError, match="peak VRAM 3.00 GB exceeds 2 GB"):
        s.generate("x", out_path=tmp_path / "e.wav")
    monkeypatch.setattr(s, "peak_vram_bytes", lambda: _base.GIB)
    s.generate("x", out_path=tmp_path / "f.wav")


def test_weights_dir_under_the_model_root(tmp_path: Path) -> None:
    with pytest.raises(ProviderError, match="spectracut setup"):
        weights_dir(KOKORO, root=tmp_path)
    d = tmp_path / "kokoro" / "kokoro-82m" / "Kokoro-82M"
    d.mkdir(parents=True)
    assert weights_dir(KOKORO, root=tmp_path) == d
    with pytest.raises(ProviderError, match="not one of its weights repos"):
        weights_dir(KOKORO, "x/y", root=tmp_path)


def test_host_models_imports_no_torch() -> None:
    assert "torch" not in host_models.__dict__
