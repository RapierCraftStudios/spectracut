# SPDX-License-Identifier: AGPL-3.0-or-later
"""Model loaders for the local model runtime, run only on render hosts.

``load_model`` is the loader a host passes to ``ModelRuntime``. Each model family
is a factory ``module:function`` taking a ``ModelSpec`` and returning a session
(``generate`` and ``close``); the module is imported only when a model of that
family is first loaded, so hosts without a family's dependencies still start. A
host names its factory in ``SPECTRACUT_MODEL_ENTRY_<FAMILY>`` (family upper-cased,
``-`` as ``_``). Factories are installed code, never network-supplied.
"""

from __future__ import annotations

import base64
import importlib
import json
import os
import re
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import IO, Any, TextIO

from spectracut.providers.base import ProviderError
from spectracut.providers.local.runtime import ModelSession, ModelSpec
from spectracut.setup.model_assets import default_model_root

ENTRY_ENV_PREFIX = "SPECTRACUT_MODEL_ENTRY_"
_ENTRY = re.compile(r"^[A-Za-z_][\w.]*:[A-Za-z_]\w*$")
LOADERS_PACKAGE = "spectracut_host.loaders"
LOCK_NAME = ".models.lock"
LOCK_TIMEOUT_S = 900.0

FAMILIES = (
    "ezaudio",
    "moss-sfx",
    "hunyuan-foley",
    "ace-step",
    "kokoro",
    "llama-cpp",
    "diffusers-gguf",
    "faster-whisper",
    "clap",
)
#: The installed factory of every registry family (``spectracut setup`` installs its deps).
FACTORIES: dict[str, str] = {
    f: f"{LOADERS_PACKAGE}.{f.replace('-', '_')}:load" for f in FAMILIES
}


def entry_env(family: str) -> str:
    return ENTRY_ENV_PREFIX + family.upper().replace("-", "_")


def load_model(spec: ModelSpec) -> ModelSession:
    """Load ``spec`` with its family's factory: the env override, else the built-in one."""
    var = entry_env(spec.family)
    entry = os.environ.get(var, "").strip()
    builtin = not entry
    if builtin:
        entry = FACTORIES.get(spec.family, "")
        if not entry:
            raise ProviderError(
                "local", f"no loader for {spec.family} ({spec.id}): set {var}=module:factory"
            )
    elif not _ENTRY.match(entry):
        raise ProviderError("local", f"{var} must look like module:factory, got {entry!r}")
    module, _, attr = entry.partition(":")
    try:
        factory: Callable[[ModelSpec], Any] = getattr(importlib.import_module(module), attr)
    except ModuleNotFoundError as exc:
        if builtin:
            raise ProviderError(
                "local",
                f"family {spec.family} not installed on this host ({spec.id}: {exc}): "
                "run `spectracut setup`",
            ) from exc
        raise ProviderError("local", f"cannot load {entry} for {spec.id}: {exc}") from exc
    except (ImportError, AttributeError) as exc:
        raise ProviderError("local", f"cannot load {entry} for {spec.id}: {exc}") from exc
    return factory(spec)  # type: ignore[no-any-return]


def _try_lock(fh: IO[bytes]) -> bool:
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(fh: IO[bytes]) -> None:
    if os.name == "nt":
        import msvcrt

        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


@contextmanager
def model_lock(
    path: Path | None = None, *, timeout: float = LOCK_TIMEOUT_S, poll: float = 0.2
) -> Iterator[None]:
    """Hold the host-wide model lock (``<model root>/.models.lock``) for one loaded model.

    Waits up to ``timeout`` seconds for another process to close its model, then raises
    ``ProviderError`` (model host busy). Released when the context exits, even when the
    process dies (the OS drops the lock with the file handle).
    """
    path = path or default_model_root() / LOCK_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as fh:
        deadline = time.monotonic() + timeout
        while not _try_lock(fh):
            if time.monotonic() >= deadline:
                raise ProviderError(
                    "local", f"model host busy: another model holds {path} (waited {timeout:g}s)"
                )
            time.sleep(poll)
        try:
            yield
        finally:
            _unlock(fh)


def _reply(out: TextIO, message: dict[str, Any]) -> None:
    out.write(json.dumps(message) + "\n")
    out.flush()


def serve(
    inp: TextIO,
    out: TextIO,
    loader: Callable[[ModelSpec], ModelSession] = load_model,
    *,
    workdir: Path | None = None,
    lock: Callable[[], AbstractContextManager[None]] = model_lock,
) -> int:
    """Serve one model session over JSON lines (``spectracut-host models-serve``).

    Requests: ``{"op": "load", "model": id}`` (resolved by id from the model
    registry only, never from spec fields on the wire), ``{"op": "generate",
    "prompt": ..., "options": {...}}`` and ``{"op": "close"}``. Every request gets one
    reply line, ``{"ok": true, ...}`` or ``{"ok": false, "error": ...}``; a generate
    reply carries the output file as base64 ``data``. Returns on close or EOF, after
    closing the loaded model. ``lock`` is held from each load until that model is
    closed, so models never co-reside across processes on one host.
    """
    from spectracut.providers.local.registry import MODELS

    session: ModelSession | None = None
    loaded: str | None = None
    held: AbstractContextManager[None] | None = None

    def unload() -> None:
        nonlocal session, loaded, held
        try:
            if session is not None:
                session.close()
        finally:
            session, loaded = None, None
            if held is not None:
                held, h = None, held
                h.__exit__(None, None, None)

    try:
        with tempfile.TemporaryDirectory(dir=workdir, prefix="models-serve-") as tmp:
            n = 0
            for line in inp:
                if not line.strip():
                    continue
                try:
                    req = json.loads(line)
                    if not isinstance(req, dict):
                        raise ValueError("request must be a JSON object")
                    op = req.get("op")
                    if op == "close":
                        _reply(out, {"ok": True})
                        return 0
                    if op == "load":
                        spec = MODELS.get(str(req.get("model")))
                        if spec is None:
                            raise ValueError(f"unknown model {req.get('model')!r}")
                        if loaded != spec.id:
                            unload()
                            guard = lock()
                            guard.__enter__()
                            held = guard
                            try:
                                session, loaded = loader(spec), spec.id
                            except BaseException:
                                unload()
                                raise
                        _reply(out, {"ok": True, "model": spec.id})
                    elif op == "generate":
                        if session is None:
                            raise ValueError("no model loaded")
                        options = req.get("options") or {}
                        if not isinstance(options, dict):
                            raise ValueError("options must be an object")
                        n += 1
                        path = Path(tmp) / f"out-{n}"
                        session.generate(str(req.get("prompt", "")), out_path=path, **options)
                        data = base64.b64encode(path.read_bytes()).decode("ascii")
                        path.unlink(missing_ok=True)
                        _reply(out, {"ok": True, "data": data})
                    else:
                        raise ValueError(f"unknown op {op!r}")
                except Exception as exc:  # noqa: BLE001 - every failure is a reply, never a crash
                    _reply(out, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            return 0
    finally:
        unload()
