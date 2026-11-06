# SPDX-License-Identifier: AGPL-3.0-or-later
"""Remote model sessions: run a local model on the model host over ssh.

The control plane is usually not the model host (``legion`` is, see
docs/engineering/local-model-selection.md). :class:`RemoteLoader` is a
:class:`~spectracut.providers.local.runtime.ModelRuntime` loader that starts
``spectracut-host models-serve`` on the host through ``ssh <alias>`` and keeps it
running while the session is warm, so a model loads once per batch.

The protocol is JSON lines. The client sends ``{"op": "load", "model": id}``,
``{"op": "generate", "prompt": ..., "options": {...}}`` and ``{"op": "close"}``,
and the host answers each with one line: ``{"ok": true, ...}``, or
``{"ok": false, "error": ...}``. A generate reply carries the output bytes as
base64 in ``data``. The client never waits on stdin EOF, because Windows OpenSSH
can lose it (see :mod:`spectracut.setup.transport`). The host resolves models
only by id from its registry and runs only installed loaders.
"""

from __future__ import annotations

import base64
import binascii
import json
import shlex
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import IO, Any, Protocol

from spectracut.providers.base import ProviderError
from spectracut.providers.local.runtime import ModelSpec

SERVE_COMMAND = "models-serve"
CLOSE_TIMEOUT_S = 30.0


class Channel(Protocol):
    """A running ``models-serve`` process: line-based stdin/stdout."""

    @property
    def stdin(self) -> IO[str] | None: ...

    @property
    def stdout(self) -> IO[str] | None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def kill(self) -> None: ...


Spawn = Callable[[Sequence[str]], Channel]


def serve_argv(host: str, os: str) -> list[str]:
    """``ssh`` argv that runs ``spectracut-host models-serve`` on ``host``."""
    if os.lower().startswith("windows"):
        # As setup runs PowerShell steps (spectracut.setup.plan): an encoded command
        # survives the ssh -> cmd/PowerShell quoting on Windows OpenSSH.
        cmd = (
            "$ProgressPreference = 'SilentlyContinue'; "
            f"& (Join-Path $env:USERPROFILE '.local/bin/spectracut-host.exe') {SERVE_COMMAND}"
        )
        enc = base64.b64encode(cmd.encode("utf-16-le")).decode("ascii")
        remote = f"powershell -NoProfile -NonInteractive -EncodedCommand {enc}"
    else:
        cmd = f'PATH="$HOME/.local/bin:$PATH" spectracut-host {SERVE_COMMAND}'
        remote = f"sh -c {shlex.quote(cmd)}"
    return ["ssh", "-o", "BatchMode=yes", host, remote]


def _popen(argv: Sequence[str]) -> Channel:
    return subprocess.Popen(  # noqa: S603 - fixed argv, host alias from the registry
        list(argv),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )


class RemoteSession:
    """A model loaded on the model host; ``generate`` writes the returned bytes locally."""

    def __init__(self, host: str, spec: ModelSpec, channel: Channel) -> None:
        self.host = host
        self.spec = spec
        self._channel = channel
        self._closed = False

    def _fail(self, what: str) -> ProviderError:
        return ProviderError("local", f"model host {self.host!r}: {self.spec.id}: {what}")

    def request(self, message: dict[str, Any]) -> dict[str, Any]:
        """Send one request line and read the reply line."""
        stdin, stdout = self._channel.stdin, self._channel.stdout
        if stdin is None or stdout is None:
            raise self._fail("no channel to models-serve")
        try:
            stdin.write(json.dumps(message) + "\n")
            stdin.flush()
            line = stdout.readline()
        except (OSError, ValueError) as exc:
            raise self._fail(f"ssh channel failed: {exc}") from exc
        if not line:
            raise self._fail(
                "models-serve exited (is the host online and spectracut-host installed?)"
            )
        try:
            reply = json.loads(line)
        except json.JSONDecodeError as exc:
            raise self._fail(f"bad reply from models-serve: {line[:200]!r}") from exc
        if not isinstance(reply, dict):
            raise self._fail(f"bad reply from models-serve: {line[:200]!r}")
        if not reply.get("ok"):
            raise self._fail(str(reply.get("error") or "models-serve refused the request"))
        return reply

    def generate(self, prompt: str, **options: Any) -> Path:
        out = options.pop("out_path", None)
        if out is None:
            raise self._fail("generate needs out_path")
        reply = self.request({"op": "generate", "prompt": prompt, "options": options})
        try:
            data = base64.b64decode(str(reply.get("data", "")), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise self._fail("bad output payload from models-serve") from exc
        path = Path(out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.request({"op": "close"})
        except ProviderError:
            pass
        finally:
            for stream in (self._channel.stdin, self._channel.stdout):
                try:
                    if stream is not None:
                        stream.close()
                except OSError:
                    pass
            try:
                self._channel.wait(timeout=CLOSE_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                self._channel.kill()


class RemoteLoader:
    """A :data:`~spectracut.providers.local.runtime.Loader` that loads on ``host``."""

    def __init__(self, host: str, os: str = "linux", *, spawn: Spawn = _popen) -> None:
        self.host = host
        self.os = os
        self._spawn = spawn

    def __call__(self, spec: ModelSpec) -> RemoteSession:
        argv = serve_argv(self.host, self.os)
        try:
            channel = self._spawn(argv)
        except OSError as exc:
            raise ProviderError(
                "local", f"model host {self.host!r}: cannot start ssh: {exc}"
            ) from exc
        session = RemoteSession(self.host, spec, channel)
        try:
            session.request({"op": "load", "model": spec.id})
        except BaseException:
            session._closed = True
            channel.kill()
            raise
        return session
