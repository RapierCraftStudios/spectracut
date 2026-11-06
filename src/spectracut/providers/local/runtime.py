# SPDX-License-Identifier: AGPL-3.0-or-later
"""The local model runtime: GPU leases and warm model sessions.

A model is loaded once per batch. :meth:`ModelRuntime.session` takes a whole-GPU
lease from the host's :class:`~spectracut.queue.executors.base.LeasePool` (the
same pool renders lease from, so a model never lands on a card that is rendering),
loads the model through an injected loader, and keeps the session warm until the
last nested ``session`` for that model exits. Loaders are injected: this module
imports no model code, and ``spectracut_host.models`` supplies the real ones.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Protocol

from spectracut.queue.executors.base import Lease, LeasePool, Resources


@dataclass(frozen=True)
class ModelSpec:
    """One local model: what it is for, which loader family runs it and what it needs."""

    id: str
    kind: str
    family: str
    vram_gb: int = 0
    cpus: float = 1.0
    mem_gb: float = 0.0
    source: str = ""
    """Weights source (a Hugging Face repo id or a path)."""

    @property
    def resources(self) -> Resources:
        return Resources(cpus=self.cpus, mem_gb=self.mem_gb, gpus=1, vram_gb=self.vram_gb)


class ModelSession(Protocol):
    """A loaded model. ``generate`` may be called many times; ``close`` frees the GPU."""

    def generate(self, prompt: str, **options: Any) -> Any: ...

    def close(self) -> None: ...


Loader = Callable[[ModelSpec], ModelSession]


@dataclass
class _Warm:
    session: ModelSession
    lease: Lease
    users: int = 1


class ModelRuntime:
    """Warm sessions over a lease pool. ``loads`` counts loads per model id."""

    def __init__(
        self,
        loader: Loader,
        pool: LeasePool,
        *,
        host: str = "local",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._loader = loader
        self._pool = pool
        self._host = host
        self._clock = clock
        self._lock = threading.RLock()
        self._warm: dict[str, _Warm] = {}
        self.loads: dict[str, int] = {}

    @contextmanager
    def session(self, spec: ModelSpec) -> Iterator[ModelSession]:
        """Yield the warm session for ``spec``, loading it on first use.

        Raises ``ExecutorBusy`` when no free card fits. Nested uses share one load;
        the lease is released and the model closed when the outermost use exits.
        """
        with self._lock:
            warm = self._warm.get(spec.id)
            if warm is not None:
                warm.users += 1
            else:
                lease = self._pool.acquire(
                    spec.resources, executor="models", host=self._host, now=self._clock()
                )
                try:
                    session = self._loader(spec)
                except BaseException:
                    self._pool.release(lease)
                    raise
                warm = self._warm[spec.id] = _Warm(session, lease)
                self.loads[spec.id] = self.loads.get(spec.id, 0) + 1
        try:
            yield warm.session
        finally:
            with self._lock:
                warm.users -= 1
                if warm.users == 0:
                    del self._warm[spec.id]
                    try:
                        warm.session.close()
                    finally:
                        self._pool.release(warm.lease)
