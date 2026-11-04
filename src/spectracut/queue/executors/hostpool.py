# SPDX-License-Identifier: AGPL-3.0-or-later
"""The default executor: a pool of directly registered ``spectracut-host`` machines.

The control plane never runs Blender or models itself. Placing a job means
leasing capacity on one registered host and handing the job to an injected
``dispatch`` transport (e.g. publishing a signed job the host pulls).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from spectracut.queue.executors.base import (
    ExecutorBusy,
    GpuCard,
    JobResult,
    LeasePool,
    Resources,
    metered_run,
)
from spectracut.queue.profile import RenderProfile

if TYPE_CHECKING:
    from spectracut.queue.queue import Job


@dataclass(frozen=True)
class HostSlot:
    """A registered host and the capacity it lends to the pool."""

    name: str
    profile: RenderProfile
    cpus: float
    mem_gb: float
    gpus: tuple[GpuCard, ...] = field(default=())
    build_lanes: bool = True
    """False on a gpu-only host: render and GPU work only, never build lanes."""


BUILD_KIND = "build"


def computeshare_allowance(slot: HostSlot) -> dict[str, object]:
    """What this host lends computeshare: capped memory, whole GPUs, and whether build lanes run."""
    return {
        "host": slot.name,
        "cpus": slot.cpus,
        "mem_gb": slot.mem_gb,
        "gpus": [g.model_dump() for g in slot.gpus],
        "build_lanes": slot.build_lanes,
    }


Dispatch = Callable[[HostSlot, "Job"], JobResult]


class HostPoolExecutor:
    """Places jobs on registered hosts by free CPUs, memory and whole GPUs."""

    def __init__(
        self,
        hosts: Sequence[HostSlot],
        dispatch: Dispatch,
        *,
        name: str = "hostpool",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        names = [h.name for h in hosts]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate host names: {names}")
        self.name = name
        self.hosts = tuple(hosts)
        self._dispatch = dispatch
        self._clock = clock
        self._pools = {h.name: LeasePool(h.cpus, h.mem_gb, h.gpus) for h in hosts}

    def can_ever_run(self, req: Resources) -> bool:
        return any(p.can_ever_run(req) for p in self._pools.values())

    def can_run_now(self, req: Resources) -> bool:
        return any(p.can_run_now(req) for p in self._pools.values())

    def pick(self, req: Resources, *, build: bool = False) -> HostSlot | None:
        """First registered host that fits now; for GPU jobs, the one with the biggest card.

        A ``build`` job skips hosts without build lanes (gpu-only hosts).
        """
        fits = [
            h
            for h in self.hosts
            if self._pools[h.name].can_run_now(req) and (h.build_lanes or not build)
        ]
        if not fits:
            return None
        if req.gpus:
            return max(fits, key=lambda h: self._pools[h.name].best_free_vram(req))
        return fits[0]

    def run(self, job: Job) -> JobResult:
        host = self.pick(job.resources, build=job.kind == BUILD_KIND)
        if host is None:
            raise ExecutorBusy(f"{self.name}: no host fits {job.resources.model_dump()} now")
        result = metered_run(
            self._pools[host.name],
            job,
            lambda: self._dispatch(host, job),
            executor=self.name,
            host=host.name,
            clock=self._clock,
        )
        if result.render_profile is None:
            result = result.model_copy(update={"render_profile": host.profile})
        return result
