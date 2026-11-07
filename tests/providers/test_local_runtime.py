# SPDX-License-Identifier: AGPL-3.0-or-later
"""Local model runtime: warm sessions, GPU leases, config-driven models and mocks.

The e2e test runs real sound-effect generations on a render host::

    SPECTRACUT_E2E=1 SPECTRACUT_MODEL_ENTRY_EZAUDIO=module:factory \
    uv run pytest -m e2e tests/providers/test_local_runtime.py
"""

from __future__ import annotations

import os
import wave
from pathlib import Path
from typing import Any

import pytest

from spectracut.providers.base import ProviderError
from spectracut.providers.local.registry import (
    KINDS,
    MODELS,
    LocalProvider,
    get_provider,
    resolve_model,
)
from spectracut.providers.local.runtime import ModelRuntime, ModelSpec
from spectracut.queue.executors.base import ExecutorBusy, GpuCard, LeasePool, Resources

SFX = MODELS["ezaudio-xl"]


@pytest.fixture(autouse=True)
def _real_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run against the real runtime path even when the ambient env selects mock providers."""
    monkeypatch.delenv("SPECTRACUT_PROVIDERS", raising=False)


class FakeSession:
    def __init__(self, spec: ModelSpec, log: list[str]) -> None:
        self.spec, self.log = spec, log

    def generate(self, prompt: str, **options: Any) -> None:
        out = options["out_path"]
        out.write_bytes(f"{self.spec.id}:{prompt}".encode())

    def close(self) -> None:
        self.log.append(f"close {self.spec.id}")


class Loader:
    def __init__(self) -> None:
        self.log: list[str] = []

    def __call__(self, spec: ModelSpec) -> FakeSession:
        self.log.append(f"load {spec.id}")
        return FakeSession(spec, self.log)


def make_runtime(cards: int = 1, vram: int = 8) -> tuple[ModelRuntime, Loader, LeasePool]:
    pool = LeasePool(8, 32, [GpuCard(name=f"gpu{i}", vram_gb=vram) for i in range(cards)])
    loader = Loader()
    return ModelRuntime(loader, pool), loader, pool


def test_batch_of_20_loads_model_once(tmp_path: Path) -> None:
    runtime, loader, _ = make_runtime()
    provider = get_provider("sfx", runtime, config={})
    with provider.batch():
        for i in range(20):
            provider.generate(f"whoosh {i}", tmp_path / f"{i}.wav")
    assert runtime.loads == {"ezaudio-xl": 1}
    assert loader.log == ["load ezaudio-xl", "close ezaudio-xl"]
    assert len(list(tmp_path.glob("*.wav"))) == 20


def test_unbatched_calls_reload_each_time(tmp_path: Path) -> None:
    runtime, _, _ = make_runtime()
    provider = get_provider("sfx", runtime, config={})
    provider.generate("a", tmp_path / "a")
    provider.generate("b", tmp_path / "b")
    assert runtime.loads == {"ezaudio-xl": 2}


def test_model_job_never_runs_on_a_rendering_card() -> None:
    runtime, _, pool = make_runtime(cards=1)
    render = pool.acquire(Resources(gpus=1), executor="hostpool", host="h", now=0.0)
    with pytest.raises(ExecutorBusy), runtime.session(SFX):
        pass
    assert runtime.loads == {}
    pool.release(render)
    with runtime.session(SFX):
        # the card is leased while the model is warm: a render cannot take it
        assert not pool.can_run_now(Resources(gpus=1))
    assert pool.can_run_now(Resources(gpus=1))


def test_lease_released_when_load_fails() -> None:
    pool = LeasePool(8, 32, [GpuCard(name="g", vram_gb=8)])

    def broken(spec: ModelSpec) -> FakeSession:
        raise RuntimeError("weights missing")

    runtime = ModelRuntime(broken, pool)
    with pytest.raises(RuntimeError), runtime.session(SFX):
        pass
    assert pool.can_run_now(Resources(gpus=1))


def test_model_needing_more_vram_than_any_card_is_refused() -> None:
    runtime, _, _ = make_runtime(vram=4)
    with pytest.raises(ExecutorBusy), runtime.session(SFX):
        pass


def test_swapping_model_is_config_only(tmp_path: Path) -> None:
    def pipeline(config: dict[str, Any]) -> tuple[str, bytes]:
        runtime, _, _ = make_runtime()
        provider = get_provider("sfx", runtime, config=config)
        out = provider.generate("door slam", tmp_path / "x.wav")
        return provider.name, out.read_bytes()

    assert pipeline({})[0] == "ezaudio-xl"
    name, data = pipeline({"sfx": "moss-sfx-8b-nf4"})
    assert name == "moss-sfx-8b-nf4"
    assert data.startswith(b"moss-sfx-8b-nf4:")


def test_env_overrides_config_and_bad_model_is_rejected() -> None:
    env = {"SPECTRACUT_MODEL_SFX": "moss-sfx-8b-nf4"}
    assert resolve_model("sfx", {"sfx": "ezaudio-xl"}, env).id == "moss-sfx-8b-nf4"
    with pytest.raises(ProviderError, match="unknown model"):
        resolve_model("sfx", {"sfx": "nope"}, {})
    with pytest.raises(ProviderError, match="is for tts"):
        resolve_model("sfx", {"sfx": "kokoro-82m"}, {})
    with pytest.raises(ProviderError, match="unknown model kind"):
        resolve_model("smell", {}, {})


@pytest.mark.parametrize("kind", KINDS)
def test_every_kind_has_a_mock(kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPECTRACUT_PROVIDERS", "mock")
    provider = get_provider(kind, config={})
    assert isinstance(provider, LocalProvider)
    out = provider.generate("hello", tmp_path / "o.bin")
    again = provider.generate("hello", tmp_path / "o2.bin")
    assert out.read_bytes() == again.read_bytes() and out.stat().st_size > 0
    with provider.batch():
        pass
    if kind in ("sfx", "foley", "music", "tts"):
        with wave.open(str(out)) as w:
            assert w.getnframes() > 0


def test_real_provider_needs_a_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SPECTRACUT_PROVIDERS", raising=False)
    with pytest.raises(ProviderError, match="ModelRuntime is required"):
        get_provider("sfx", config={})


def test_host_loader_requires_installed_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    from spectracut_host.models import entry_env, load_model

    monkeypatch.delenv(entry_env("ezaudio"), raising=False)
    with pytest.raises(ProviderError, match="SPECTRACUT_MODEL_ENTRY_EZAUDIO"):
        load_model(SFX)
    monkeypatch.setenv(entry_env("ezaudio"), "os; rm:x")
    with pytest.raises(ProviderError, match="module:factory"):
        load_model(SFX)
    monkeypatch.setenv(entry_env("ezaudio"), f"{__name__}:fake_factory")
    assert load_model(SFX).spec is SFX  # type: ignore[attr-defined]


def fake_factory(spec: ModelSpec) -> FakeSession:
    return FakeSession(spec, [])


@pytest.mark.e2e
@pytest.mark.skipif(os.environ.get("SPECTRACUT_E2E") != "1", reason="set SPECTRACUT_E2E=1")
def test_e2e_real_sfx_batch_loads_once(tmp_path: Path) -> None:
    from spectracut_host.models import load_model

    count = {"n": 0}

    def counting(spec: ModelSpec) -> Any:
        count["n"] += 1
        return load_model(spec)

    pool = LeasePool(4, 16, [GpuCard(name="gpu", vram_gb=8)])
    runtime = ModelRuntime(counting, pool)
    provider = get_provider("sfx", runtime, config={})
    with provider.batch():
        for i in range(20):
            out = provider.generate(f"short impact {i}", tmp_path / f"{i}.wav", duration_s=2)
            assert out.stat().st_size > 0
    assert count["n"] == 1


# ---- which models setup installed on the model host (#326) --------------------


def test_installed_models_reads_setup_state(tmp_path: Path) -> None:
    from spectracut.providers.local.installed import installed_models
    from spectracut.setup.model_assets import model_version
    from spectracut.setup.plan import MODEL_PREFIX, HostState, SetupState, save_state

    assert installed_models(tmp_path, "legion") is None  # no setup state: unknown
    components = {
        f"{MODEL_PREFIX}ezaudio-xl": model_version("ezaudio-xl"),
        f"{MODEL_PREFIX}kokoro-82m": "stale-version",
        f"{MODEL_PREFIX}no-such-model": "x",
        "models-deps": "1",
    }
    state = SetupState(
        hosts={"legion": HostState(components=components), "bare": HostState(components={})}
    )
    save_state(state, tmp_path)
    assert installed_models(tmp_path, "legion") == frozenset({"ezaudio-xl"})
    assert installed_models(tmp_path, "bare") == frozenset()  # setup --skip-models
    assert installed_models(tmp_path, "elsewhere") is None  # set up by hand
    (tmp_path / "setup-state.json").write_text("{not json", encoding="utf-8")
    assert installed_models(tmp_path, "legion") is None


def test_model_runtime_keeps_the_installed_set() -> None:
    pool = LeasePool(8, 32, [GpuCard(name="g", vram_gb=8)])
    assert ModelRuntime(lambda s: None, pool).installed is None  # type: ignore[arg-type,return-value]
    runtime = ModelRuntime(lambda s: None, pool, installed=["a", "a"])  # type: ignore[arg-type,return-value]
    assert runtime.installed == frozenset({"a"})
