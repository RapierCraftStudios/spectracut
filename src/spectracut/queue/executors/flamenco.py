# SPDX-License-Identifier: AGPL-3.0-or-later
"""``FlamencoExecutor``: Blender jobs go to the Flamenco Manager.

A ``blender`` job's payload names a signed job file on shared storage and its
:class:`~spectracut.queue.signing.JobEnvelope`. The executor resolves the
worker tag, submits a ``spectracut-shot`` job and polls its status until it
finishes, mirroring every status change onto the queue job.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Final, Protocol

from spectracut.queue.executors.base import JobResult, JobStatus, Resources
from spectracut.queue.flamenco import (
    DEFAULT_CHUNK_SIZE,
    WorkerTag,
    build_shot_job,
    select_worker_tag,
)
from spectracut.queue.signing import JobEnvelope
from spectracut.studio.node import Tier

if TYPE_CHECKING:
    from spectracut.queue.queue import Job

#: Flamenco job priority per quality tier: heavier tiers go first.
TIER_PRIORITY: Final[dict[Tier, int]] = {
    Tier.PREVIEW: 25,
    Tier.REVIEW: 50,
    Tier.FINAL: 75,
    Tier.HERO: 90,
}

#: Flamenco job statuses mapped to queue statuses. Unknown ones count as running.
FLAMENCO_STATUS: Final[dict[str, JobStatus]] = {
    "under-construction": JobStatus.QUEUED,
    "queued": JobStatus.QUEUED,
    "paused": JobStatus.QUEUED,
    "pause-requested": JobStatus.QUEUED,
    "requeueing": JobStatus.QUEUED,
    "active": JobStatus.RUNNING,
    "cancel-requested": JobStatus.RUNNING,
    "completed": JobStatus.SUCCEEDED,
    "failed": JobStatus.FAILED,
    "canceled": JobStatus.FAILED,
}


class FlamencoJobFailed(RuntimeError):
    """The Flamenco job ended ``failed`` or ``canceled``."""

    def __init__(self, flamenco_id: str, status: str) -> None:
        self.flamenco_id = flamenco_id
        self.status = status
        super().__init__(f"flamenco job {flamenco_id} ended {status}")


class FlamencoTimeout(RuntimeError):
    """The Flamenco job did not finish within the executor's timeout."""


class FlamencoApi(Protocol):
    """The part of :class:`~spectracut.queue.flamenco.FlamencoClient` the executor uses."""

    def list_tags(self) -> list[WorkerTag]: ...

    def submit(self, job: dict[str, Any]) -> dict[str, Any]: ...

    def job_status(self, job_id: str) -> str: ...


def map_status(flamenco_status: str) -> JobStatus:
    return FLAMENCO_STATUS.get(flamenco_status, JobStatus.RUNNING)


class FlamencoExecutor:
    """Submits ``blender`` jobs to Flamenco and waits for them."""

    def __init__(
        self,
        client: FlamencoApi,
        *,
        output_dir: str,
        name: str = "flamenco",
        sleep: Callable[[float], None] = time.sleep,
        poll_interval: float = 2.0,
        timeout_s: float | None = None,
        capacity: Resources | None = None,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        on_status: Callable[[Job, JobStatus], None] | None = None,
    ) -> None:
        self.name = name
        self.client = client
        self.output_dir = output_dir
        self._sleep = sleep
        self.poll_interval = poll_interval
        self.timeout_s = timeout_s
        self.capacity = capacity
        self.chunk_size = chunk_size
        self._on_status = on_status

    def can_ever_run(self, req: Resources) -> bool:
        cap = self.capacity
        if cap is None:  # the farm's own workers decide; Flamenco queues the rest
            return True
        return (
            req.cpus <= cap.cpus
            and req.mem_gb <= cap.mem_gb
            and req.gpus <= cap.gpus
            and req.vram_gb <= cap.vram_gb
        )

    def can_run_now(self, req: Resources) -> bool:
        return self.can_ever_run(req)

    def _observe(self, job: Job, status: JobStatus) -> None:
        job.observe(status)
        if self._on_status is not None:
            self._on_status(job, status)

    def run(self, job: Job) -> JobResult:
        payload = job.payload
        envelope = JobEnvelope.model_validate(payload["envelope"])
        job_file = str(payload["job_file"])
        requirement = payload.get("worker_tag") or envelope.required_tag
        if not requirement:
            raise ValueError(f"blender job {job.id} names no worker tag")
        tag = select_worker_tag(str(requirement), self.client.list_tags())
        output_dir = str(payload.get("output_dir") or self.output_dir)
        submitted = self.client.submit(
            build_shot_job(
                job_file,
                envelope,
                tag,
                output_dir=output_dir,
                chunk_size=self.chunk_size,
                priority=TIER_PRIORITY[job.tier],
            )
        )
        flamenco_id = str(submitted["id"])
        waited = 0.0
        while True:
            raw = self.client.job_status(flamenco_id)
            status = map_status(raw)
            if status is JobStatus.SUCCEEDED:
                return JobResult(
                    output={
                        "flamenco_job_id": flamenco_id,
                        "status": raw,
                        "output_dir": output_dir,
                        "worker_tag": tag.name,
                    },
                    render_profile=job.render_profile,
                )
            if status is JobStatus.FAILED:
                raise FlamencoJobFailed(flamenco_id, raw)
            self._observe(job, status)
            if self.timeout_s is not None and waited >= self.timeout_s:
                raise FlamencoTimeout(f"flamenco job {flamenco_id} still {raw} after {waited}s")
            self._sleep(self.poll_interval)
            waited += self.poll_interval
