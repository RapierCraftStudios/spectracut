# SPDX-License-Identifier: AGPL-3.0-or-later
"""Remote model sessions (#289): the control plane runs local models on the model host.

The ssh process is replaced by an in-process channel that runs the real host loop
(``spectracut_host.models.serve``) on a pair of pipes, with a fake model loader.
"""

from __future__ import annotations

import base64
import io
import json
import os
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import IO, Any

import pytest
from spectracut_host.models import serve

from spectracut.providers.base import ProviderError
from spectracut.providers.local.registry import MODELS, MockLocalProvider, RuntimeProvider
from spectracut.providers.local.remote import RemoteLoader, RemoteSession, serve_argv
from spectracut.providers.local.runtime import ModelRuntime, ModelSpec
from spectracut.queue.executors.base import GpuCard, LeasePool

SFX = MODELS["ezaudio-xl"]


class FakeModel:
    """A loaded model: writes the mock provider's wav and counts calls."""

    def __init__(self, spec: ModelSpec, log: list[str]) -> None:
        self.spec = spec
        self.log = log

    def generate(self, prompt: str, **options: Any) -> Any:
        out = Path(options.pop("out_path"))
        self.log.append(f"generate:{prompt}")
        return MockLocalProvider(self.spec.kind).generate(prompt, out, **options)

    def close(self) -> None:
        self.log.append("close")


class ServeChannel:
    """``ssh host spectracut-host models-serve`` in-process: the real loop on pipes."""

    def __init__(self, log: list[str]) -> None:
        r_in, w_in = os.pipe()
        r_out, w_out = os.pipe()
        self.stdin: IO[str] | None = open(w_in, "w", encoding="utf-8")  # noqa: SIM115
        self.stdout: IO[str] | None = open(r_out, encoding="utf-8")  # noqa: SIM115
        host_in = open(r_in, encoding="utf-8")  # noqa: SIM115
        host_out = open(w_out, "w", encoding="utf-8")  # noqa: SIM115
        self.loads: list[str] = []

        def loader(spec: ModelSpec) -> FakeModel:
            self.loads.append(spec.id)
            return FakeModel(spec, log)

        def run() -> None:
            with host_in, host_out:
                self.code = serve(host_in, host_out, loader)

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        self.killed = False

    def wait(self, timeout: float | None = None) -> int:
        self.thread.join(timeout)
        return 0

    def kill(self) -> None:
        self.killed = True


class Spawner:
    def __init__(self) -> None:
        self.argv: list[list[str]] = []
        self.channels: list[ServeChannel] = []
        self.log: list[str] = []

    def __call__(self, argv: Sequence[str]) -> ServeChannel:
        self.argv.append(list(argv))
        channel = ServeChannel(self.log)
        self.channels.append(channel)
        return channel


def _runtime(spawn: Spawner, os_name: str = "windows") -> ModelRuntime:
    pool = LeasePool(8, 28, (GpuCard(name="RTX 3070 Laptop", vram_gb=8),))
    return ModelRuntime(RemoteLoader("legion", os_name, spawn=spawn), pool, host="legion")


def test_a_batch_loads_once_on_the_host_and_writes_outputs_locally(tmp_path: Path) -> None:
    spawn = Spawner()
    provider = RuntimeProvider(SFX, _runtime(spawn))
    with provider.batch():
        outs = [provider.generate(f"door {n}", tmp_path / f"o{n}.wav") for n in range(3)]
    assert len(spawn.channels) == 1 and spawn.channels[0].loads == ["ezaudio-xl"]
    for n, out in enumerate(outs):
        expected = MockLocalProvider("sfx").generate(f"door {n}", tmp_path / "ref.wav")
        assert out.read_bytes() == expected.read_bytes()
    assert spawn.log == ["generate:door 0", "generate:door 1", "generate:door 2", "close"]
    assert spawn.channels[0].code == 0


def test_serve_argv_runs_spectracut_host_over_ssh() -> None:
    win = serve_argv("legion", "windows")
    assert win[:3] == ["ssh", "-o", "BatchMode=yes"] and win[3] == "legion"
    enc = win[4].rsplit(" ", 1)[1]
    assert "spectracut-host.exe') models-serve" in base64.b64decode(enc).decode("utf-16-le")
    linux = serve_argv("mrdubey-1", "linux")
    assert linux[3] == "mrdubey-1" and "spectracut-host models-serve" in linux[4]


def test_an_error_reply_names_the_host(tmp_path: Path) -> None:
    spawn = Spawner()
    runtime = _runtime(spawn)
    bogus = ModelSpec("not-a-model", "sfx", "ezaudio", 8)
    with pytest.raises(ProviderError, match=r"model host 'legion'.*unknown model"):
        with runtime.session(bogus):
            pass


class DeadChannel:
    """ssh exits at once (host offline): stdout hits EOF."""

    def __init__(self) -> None:
        self.stdin: IO[str] | None = io.StringIO()
        self.stdout: IO[str] | None = io.StringIO("")
        self.killed = False

    def wait(self, timeout: float | None = None) -> int:
        return 255

    def kill(self) -> None:
        self.killed = True


def test_an_unreachable_host_is_a_provider_error_naming_it() -> None:
    dead = DeadChannel()
    loader = RemoteLoader("legion", "windows", spawn=lambda argv: dead)
    with pytest.raises(ProviderError, match=r"model host 'legion'.*exited"):
        loader(SFX)
    assert dead.killed


def test_cannot_start_ssh_names_the_host() -> None:
    def missing(argv: Sequence[str]) -> DeadChannel:
        raise FileNotFoundError("ssh")

    with pytest.raises(ProviderError, match=r"model host 'legion': cannot start ssh"):
        RemoteLoader("legion", spawn=missing)(SFX)


def test_close_sends_the_close_op_and_never_waits_on_eof() -> None:
    sent: list[dict[str, Any]] = []

    class Replying(io.StringIO):
        def write(self, s: str) -> int:
            sent.append(json.loads(s))
            return len(s)

    class Channel(DeadChannel):
        def __init__(self) -> None:
            super().__init__()
            self.stdin = Replying()
            self.stdout = io.StringIO('{"ok": true}\n{"ok": true}\n')

    session = RemoteLoader("legion", spawn=lambda argv: Channel())(SFX)
    assert isinstance(session, RemoteSession)
    session.close()
    session.close()  # idempotent
    assert sent == [{"op": "load", "model": "ezaudio-xl"}, {"op": "close"}]


def test_serve_refuses_unknown_ops_and_generate_before_load() -> None:
    inp = io.StringIO(
        '{"op": "generate", "prompt": "x"}\n{"op": "exec", "code": "1"}\n[1]\n{"op": "close"}\n'
    )
    out = io.StringIO()
    assert serve(inp, out, lambda spec: FakeModel(spec, [])) == 0
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [r["ok"] for r in replies] == [False, False, False, True]
    assert "no model loaded" in replies[0]["error"] and "unknown op" in replies[1]["error"]
