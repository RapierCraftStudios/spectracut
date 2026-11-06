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

import importlib
import os
import re
from collections.abc import Callable
from typing import Any

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
