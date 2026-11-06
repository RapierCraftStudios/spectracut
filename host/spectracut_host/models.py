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
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

from spectracut.providers.base import ProviderError
from spectracut.providers.local.runtime import ModelSession, ModelSpec

ENTRY_ENV_PREFIX = "SPECTRACUT_MODEL_ENTRY_"
_ENTRY = re.compile(r"^[A-Za-z_][\w.]*:[A-Za-z_]\w*$")


def entry_env(family: str) -> str:
    return ENTRY_ENV_PREFIX + family.upper().replace("-", "_")


def load_model(spec: ModelSpec) -> ModelSession:
    """Load ``spec`` with its family's installed factory."""
    var = entry_env(spec.family)
    entry = os.environ.get(var, "").strip()
    if not entry:
        raise ProviderError(
            "local", f"no loader for {spec.family} ({spec.id}): set {var}=module:factory"
        )
    if not _ENTRY.match(entry):
        raise ProviderError("local", f"{var} must look like module:factory, got {entry!r}")
    module, _, attr = entry.partition(":")
    try:
        factory: Callable[[ModelSpec], Any] = getattr(importlib.import_module(module), attr)
    except (ImportError, AttributeError) as exc:
        raise ProviderError("local", f"cannot load {entry} for {spec.id}: {exc}") from exc
    return factory(spec)  # type: ignore[no-any-return]


def _reply(out: TextIO, message: dict[str, Any]) -> None:
    out.write(json.dumps(message) + "\n")
    out.flush()


def serve(
    inp: TextIO,
    out: TextIO,
    loader: Callable[[ModelSpec], ModelSession] = load_model,
    *,
    workdir: Path | None = None,
) -> int:
    """Serve one model session over JSON lines (``spectracut-host models-serve``).

    Requests: ``{"op": "load", "model": id}`` (resolved by id from the model
    registry only, never from spec fields on the wire), ``{"op": "generate",
    "prompt": ..., "options": {...}}`` and ``{"op": "close"}``. Every request gets one
    reply line, ``{"ok": true, ...}`` or ``{"ok": false, "error": ...}``; a generate
    reply carries the output file as base64 ``data``. Returns on close or EOF, after
    closing the loaded model.
    """
    from spectracut.providers.local.registry import MODELS

    session: ModelSession | None = None
    loaded: str | None = None
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
                            if session is not None:
                                session.close()
                                session, loaded = None, None
                            session, loaded = loader(spec), spec.id
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
        if session is not None:
            session.close()
