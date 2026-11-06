# SPDX-License-Identifier: AGPL-3.0-or-later
"""Executor protocol, resource requests and whole-GPU lease accounting.

An executor runs one queue job at a time on capacity it owns (a pool of
registered hosts, the Flamenco farm, optional computeshare sandboxes via
``ComputeshareExecutor``). Jobs declare the
:class:`Resources` they need; the queue only hands a job to an executor that
can meet them. GPUs are leased as whole cards, never sliced: a job asking for
``gpus=1, vram_gb=12`` gets one free card with at least 12 GB of VRAM.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Protocol, runtime_checkable

from pydantic import Field, JsonValue

from spectracut.queue.profile import RenderProfile
from spectracut.spec._base import NonEmptyStr, StrictModel
from spectracut.studio.ledger import Usage, usage_of

if TYPE_CHECKING:
    from spectracut.queue.queue import Job


class JobStatus(StrEnum):
    """Lifecycle of a queue job."""

    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_AI = "waiting_for_ai"
    """AI capacity ran out (``ProviderExhausted``); resumes after ``resume_at``."""
    SUCCEEDED = "succeeded"
    FAILED = "failed"


TERMINAL = frozenset({JobStatus.SUCCEEDED, JobStatus.FAILED})


class Resources(StrictModel):
    """What a job needs. ``vram_gb`` is the VRAM required on *each* leased GPU."""

    cpus: float = Field(default=1.0, gt=0)
    mem_gb: float = Field(default=0.0, ge=0)
    gpus: int = Field(default=0, ge=0)
    vram_gb: int = Field(default=0, ge=0)


class GpuCard(StrictModel):
    """One whole GPU an executor can lease."""

    name: NonEmptyStr
    vram_gb: Annotated[int, Field(ge=0)] = 0


class JobResult(StrictModel):
    """What an executor returns for a finished job."""

    output: JsonValue = None
    frames: list[str] = Field(default_factory=list)
    usage: Usage = Usage()
    render_profile: RenderProfile | None = None
    """Profile of the machine that rendered, when known (keys the frame cache)."""


class NoCapableExecutor(LookupError):
    """No executor can ever meet a job's resource request."""


class ExecutorBusy(RuntimeError):
    """The executor can meet the request, but not with its free capacity right now.

    ``retry_at``, when set, is the earliest time the job should be tried again
    (e.g. a computeshare router ``503`` + ``Retry-After``); the queue keeps the
    job queued until then. Without it the job is retried on the next tick.
    """

    def __init__(self, *args: object, retry_at: datetime | None = None) -> None:
        super().__init__(*args)
        self.retry_at = retry_at


@dataclass(frozen=True)
class Lease:
    """Capacity held by one running job."""

    executor: str
    host: str
    cpus: float
    mem_gb: float
    cards: tuple[int, ...]
    """Indexes of the leased GPU cards in the pool."""
    started_at: float


class LeasePool:
    """Capacity bookkeeping for one machine: CPUs, memory and whole GPU cards."""

    def __init__(self, cpus: float, mem_gb: float, cards: Sequence[GpuCard] = ()) -> None:
        self.cpus = cpus
        self.mem_gb = mem_gb
        self.cards = tuple(cards)
        self._used_cpus = 0.0
        self._used_mem = 0.0
        self._busy_cards: set[int] = set()

    @property
    def free_cpus(self) -> float:
        return self.cpus - self._used_cpus

    @property
    def free_mem_gb(self) -> float:
        return self.mem_gb - self._used_mem

    def _eligible(self, req: Resources, *, free_only: bool) -> list[int]:
        idx = [
            i
            for i, card in enumerate(self.cards)
            if card.vram_gb >= req.vram_gb and not (free_only and i in self._busy_cards)
        ]
        # Bigger cards first: heavy jobs go to the card with the most VRAM.
        return sorted(idx, key=lambda i: -self.cards[i].vram_gb)

    def can_ever_run(self, req: Resources) -> bool:
        return (
            req.cpus <= self.cpus
            and req.mem_gb <= self.mem_gb
            and len(self._eligible(req, free_only=False)) >= req.gpus
        )

    def can_run_now(self, req: Resources) -> bool:
        return (
            req.cpus <= self.free_cpus
            and req.mem_gb <= self.free_mem_gb
            and len(self._eligible(req, free_only=True)) >= req.gpus
        )

    def best_free_vram(self, req: Resources) -> int:
        cards = self._eligible(req, free_only=True)
        return self.cards[cards[0]].vram_gb if cards else 0

    def acquire(self, req: Resources, *, executor: str, host: str, now: float) -> Lease:
        if not self.can_run_now(req):
            raise ExecutorBusy(f"{executor}/{host} cannot fit {req.model_dump()} now")
        cards = tuple(self._eligible(req, free_only=True)[: req.gpus])
        self._used_cpus += req.cpus
        self._used_mem += req.mem_gb
        self._busy_cards.update(cards)
        return Lease(executor, host, req.cpus, req.mem_gb, cards, now)

    def release(self, lease: Lease) -> None:
        self._used_cpus = max(0.0, self._used_cpus - lease.cpus)
        self._used_mem = max(0.0, self._used_mem - lease.mem_gb)
        self._busy_cards.difference_update(lease.cards)


def metered_run(
    pool: LeasePool,
    job: Job,
    body: Callable[[], JobResult],
    *,
    executor: str,
    host: str,
    clock: Callable[[], float],
) -> JobResult:
    """Lease ``job.resources`` on ``pool``, run ``body``, release, and add GPU minutes.

    The leased cards are billed whether ``body`` returns or raises: on failure
    the minutes are added to the exception's ``usage`` (see
    :func:`~spectracut.studio.ledger.usage_of`) and the same exception re-raised.
    """
    lease = pool.acquire(job.resources, executor=executor, host=host, now=clock())
    try:
        result = body()
    except Exception as exc:
        pool.release(lease)
        carried = usage_of(exc)
        minutes = len(lease.cards) * max(0.0, clock() - lease.started_at) / 60.0
        exc.usage = carried.model_copy(  # type: ignore[attr-defined]
            update={"gpu_minutes": carried.gpu_minutes + minutes}
        )
        raise
    except BaseException:
        pool.release(lease)
        raise
    pool.release(lease)
    minutes = len(lease.cards) * max(0.0, clock() - lease.started_at) / 60.0
    usage = result.usage.model_copy(update={"gpu_minutes": result.usage.gpu_minutes + minutes})
    return result.model_copy(update={"usage": usage})


@runtime_checkable
class Executor(Protocol):
    """Runs queue jobs on capacity it owns. Implementations must be pluggable."""

    name: str

    def can_ever_run(self, req: Resources) -> bool:
        """Whether the executor's total capacity could ever meet ``req``."""
        ...

    def can_run_now(self, req: Resources) -> bool:
        """Whether its free capacity meets ``req`` right now."""
        ...

    def run(self, job: Job) -> JobResult:
        """Run ``job`` to completion. Raises on failure, ``ProviderExhausted`` included."""
        ...
