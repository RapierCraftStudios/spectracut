# SPDX-License-Identifier: AGPL-3.0-or-later
"""Qwen2.5-Omni-7B GGUF audio critic, run through the pinned llama.cpp ``llama-mtmd-cli``.

``load(spec)`` checks the weights and the binary and returns a session whose ``generate``
runs the CLI once on one audio clip and writes the critic's text to ``out_path``. The
process exits before ``generate`` returns, so nothing stays resident on the GPU between
calls. Peak VRAM is sampled from ``nvidia-smi`` (the CLI is not a torch process).

Options: ``audio_path`` (a path on this host) or ``audio_b64`` (wav bytes, for remote
callers), ``n_predict`` (200), ``temperature`` (0.2), ``ctx`` (8192), ``ngl`` (99),
``timeout`` (600 s). The binary is ``SPECTRACUT_LLAMA_MTMD_CLI``, else
``<model dir>/bin/llama-mtmd-cli[.exe]``, else ``PATH``.
"""

from __future__ import annotations

import base64
import binascii
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any, Protocol

from spectracut.providers.base import ProviderError
from spectracut.providers.local.runtime import ModelSpec
from spectracut.setup.model_assets import BIN_DIR, asset_for

from ._base import BaseSession, check_vram_spec, model_dir, weights_dir

BIN_ENV = "SPECTRACUT_LLAMA_MTMD_CLI"
BIN_NAME = "llama-mtmd-cli"
MIB = 1024**2
SAMPLE_S = 0.3


class Proc(Protocol):
    returncode: int | None

    def poll(self) -> int | None: ...
    def kill(self) -> None: ...
    def wait(self) -> int: ...


Spawn = Callable[[list[str], IO[bytes], IO[bytes]], Proc]
Smi = Callable[[], int | None]
"""GPU memory in use, in MiB, or ``None`` when it cannot be read."""


def _popen(argv: list[str], out: IO[bytes], err: IO[bytes]) -> Proc:
    return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=err)


def nvidia_smi_used_mib() -> int | None:
    """``memory.used`` of the first GPU in MiB, or ``None`` without a working nvidia-smi."""
    try:
        done = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return int(done.stdout.strip().splitlines()[0]) if done.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def _binary(spec: ModelSpec) -> Path:
    env = os.environ.get(BIN_ENV, "").strip()
    if env:
        path = Path(env)
        if not path.is_file():
            raise ProviderError("local", f"{spec.id}: {BIN_ENV}={env} is not a file")
        return path
    name = BIN_NAME + (".exe" if sys.platform == "win32" else "")
    local = model_dir(spec) / BIN_DIR / name
    if local.is_file():
        return local
    found = shutil.which(BIN_NAME)
    if found:
        return Path(found)
    raise ProviderError(
        "local", f"{spec.id}: {BIN_NAME} not found ({local}): run `spectracut setup`"
    )


class LlamaCppSession(BaseSession):
    def __init__(
        self,
        spec: ModelSpec,
        binary: Path,
        gguf: Path,
        mmproj: Path,
        *,
        spawn: Spawn = _popen,
        smi: Smi = nvidia_smi_used_mib,
        sample_s: float = SAMPLE_S,
    ) -> None:
        super().__init__(spec)
        self.binary, self.gguf, self.mmproj = binary, gguf, mmproj
        self._spawn, self._smi, self._sample_s = spawn, smi, sample_s
        self._peak: int | None = None

    def peak_vram_bytes(self) -> int | None:
        return self._peak

    def _audio(self, out_path: Path, options: dict[str, Any]) -> tuple[Path, bool]:
        if options.get("audio_b64") is not None:
            try:
                data = base64.b64decode(str(options["audio_b64"]), validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ProviderError("local", f"{self.spec.id}: bad audio_b64") from exc
            fd, name = tempfile.mkstemp(suffix=".wav", dir=out_path.parent)
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            return Path(name), True
        raw = options.get("audio_path")
        if not raw or not Path(str(raw)).is_file():
            raise ProviderError(
                "local", f"{self.spec.id}: needs audio_b64 or an existing audio_path"
            )
        return Path(str(raw)), False

    def _generate(self, prompt: str, out_path: Path, **options: Any) -> None:
        audio, temp = self._audio(out_path, options)
        try:
            argv = [
                str(self.binary),
                "-m", str(self.gguf),
                "--mmproj", str(self.mmproj),
                "--audio", str(audio),
                "-p", prompt,
                "-ngl", str(options.get("ngl", 99)),
                "-c", str(options.get("ctx", 8192)),
                "--temp", str(options.get("temperature", 0.2)),
                "-n", str(options.get("n_predict", 200)),
            ]  # fmt: skip
            text = self._run(argv, float(options.get("timeout", 600)))
        finally:
            if temp:
                audio.unlink(missing_ok=True)
        if not text:
            raise ProviderError("local", f"{self.spec.id}: the critic produced no text")
        out_path.write_text(text, encoding="utf-8")

    def _run(self, argv: list[str], timeout: float) -> str:
        base = self._smi()
        peak = base
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            try:
                proc = self._spawn(argv, out, err)
            except OSError as exc:
                raise ProviderError(
                    "local", f"{self.spec.id}: cannot start {argv[0]}: {exc}"
                ) from exc
            deadline = time.monotonic() + timeout
            while proc.poll() is None:
                used = self._smi()
                if used is not None and (peak is None or used > peak):
                    peak = used
                if time.monotonic() >= deadline:
                    proc.kill()
                    proc.wait()
                    raise ProviderError("local", f"{self.spec.id}: timed out after {timeout:g}s")
                time.sleep(self._sample_s)
            if base is not None and peak is not None:
                self._peak = max(self._peak or 0, (peak - base) * MIB)
            out.seek(0)
            err.seek(0)
            text = out.read().decode("utf-8", "replace").strip()
            tail = "\n".join(err.read().decode("utf-8", "replace").splitlines()[-20:])
        if proc.returncode != 0:
            raise ProviderError(
                "local", f"{self.spec.id}: {BIN_NAME} exited {proc.returncode}: {tail}"
            )
        return text


def load(spec: ModelSpec) -> LlamaCppSession:
    check_vram_spec(spec)
    include = asset_for(spec.id).weights[0].include
    mmproj_name = next(n for n in include if n.startswith("mmproj"))
    gguf_name = next(n for n in include if n != mmproj_name)
    weights = weights_dir(spec)
    gguf, mmproj = weights / gguf_name, weights / mmproj_name
    for path in (gguf, mmproj):
        if not path.is_file():
            raise ProviderError("local", f"{spec.id}: {path} missing: run `spectracut setup`")
    return LlamaCppSession(spec, _binary(spec), gguf, mmproj)
