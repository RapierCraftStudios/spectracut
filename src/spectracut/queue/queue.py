# SPDX-License-Identifier: AGPL-3.0-or-later
"""The job queue: production-graph node jobs run through pluggable executors.

- Every job declares :class:`~spectracut.queue.executors.base.Resources`
  (cpus, mem_gb, gpus, vram_gb) and only goes to an executor that can meet them.
- ``blender`` jobs go to Flamenco (:class:`FlamencoExecutor`); AI, model, audio
  and CPU jobs stay on SpectraCut's own executors (the host pool by default).
- A job whose recipe hash and render profile are in the :class:`FrameCache`
  completes without rendering and never reaches an executor.
- A :class:`~spectracut.providers.base.ProviderExhausted` error parks the job in
  ``waiting_for_ai`` until its ``reset_at``; then it runs again.
- An :class:`~spectracut.queue.executors.base.ExecutorBusy` with ``retry_at``
  (a computeshare ``503`` + ``Retry-After``) keeps the job ``queued`` and skips
  it until ``retry_at``.
- :meth:`JobQueue.as_runner` plugs the queue into
  :meth:`~spectracut.studio.graph.ProductionGraph.run`, so a job's outcome
  becomes its node's status (clean or failed).
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from pydantic import JsonValue

from spectracut.providers.base import ProviderExhausted
from spectracut.queue.cache import FrameCache
from spectracut.queue.executors.base import (
    TERMINAL,
    Executor,
    ExecutorBusy,
    JobResult,
    JobStatus,
    NoCapableExecutor,
    Resources,
)
from spectracut.queue.executors.flamenco import FlamencoExecutor
from spectracut.queue.profile import RenderProfile
from spectracut.studio.graph import RunContext, Runner, RunResult
from spectracut.studio.node import Tier

__all__ = ["Job", "JobFailed", "JobQueue", "QueueStalled"]

BLENDER: str = "blender"
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")


class JobFailed(RuntimeError):
    """A job driven through :meth:`JobQueue.as_runner` ended failed."""


class QueueStalled(RuntimeError):
    """Jobs remain queued but no executor can take them and none are waiting."""


@dataclass(eq=False)
class Job:
    """One unit of work for a production-graph node."""

    id: str
    kind: str
    """``blender`` (Flamenco), or ``ai``, ``model``, ``audio``, ``cpu``, ..."""
    node_id: str | None = None
    tier: Tier = Tier.PREVIEW
    resources: Resources = field(default_factory=Resources)
    recipe_hash: str | None = None
    render_profile: RenderProfile | None = None
    payload: dict[str, JsonValue] = field(default_factory=dict)
    status: JobStatus = JobStatus.QUEUED
    resume_at: datetime | None = None
    cached: bool = False
    executor: str | None = None
    attempts: int = 0
    error: str | None = None
    result: JobResult | None = None
    status_history: list[JobStatus] = field(default_factory=list)

    def __post_init__(self) -> None:
        for what, value in (("id", self.id), ("kind", self.kind)):
            if not _SLUG.match(value):
                raise ValueError(f"job {what} must be a slug, got {value!r}")
        if self.recipe_hash is not None and not _HASH.match(self.recipe_hash):
            raise ValueError(
                f"recipe_hash must be a sha256: content hash, got {self.recipe_hash!r}"
            )
        if not self.status_history:
            self.status_history.append(self.status)

    @property
    def cacheable(self) -> bool:
        return self.recipe_hash is not None and self.render_profile is not None

    @property
    def done(self) -> bool:
        return self.status in TERMINAL

    def observe(self, status: JobStatus) -> None:
        """Set the status, recording it in the history when it changed."""
        self.status = status
        if self.status_history[-1] is not status:
            self.status_history.append(status)


class JobQueue:
    """Places jobs on executors, with the frame cache and AI-capacity waiting."""

    def __init__(
        self,
        executors: Sequence[Executor],
        *,
        cache: FrameCache | None = None,
        route: Mapping[str, Sequence[str]] | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        names = [e.name for e in executors]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate executor names: {names}")
        self.executors = tuple(executors)
        self.cache = cache
        self.route = {k: tuple(v) for k, v in (route or {}).items()}
        self._now = now
        self._jobs: dict[str, Job] = {}

    # ---- placement ---------------------------------------------------------

    def executors_for(self, job: Job) -> list[Executor]:
        """Executors allowed for the job's kind, in preference order."""
        if job.kind in self.route:
            by_name = {e.name: e for e in self.executors}
            return [by_name[n] for n in self.route[job.kind] if n in by_name]
        is_blender = job.kind == BLENDER
        return [e for e in self.executors if isinstance(e, FlamencoExecutor) is is_blender]

    def submit(self, job: Job) -> Job:
        old = self._jobs.get(job.id)
        if old is not None and not old.done:
            raise ValueError(f"job {job.id!r} is already queued")
        if not any(e.can_ever_run(job.resources) for e in self.executors_for(job)):
            raise NoCapableExecutor(
                f"no executor for {job.kind!r} jobs can meet {job.resources.model_dump()}"
            )
        self._jobs[job.id] = job
        return job

    def job(self, job_id: str) -> Job:
        return self._jobs[job_id]

    @property
    def jobs(self) -> tuple[Job, ...]:
        return tuple(self._jobs.values())

    # ---- running -----------------------------------------------------------

    def tick(self, now: datetime | None = None) -> list[Job]:
        """Advance every runnable job once; return the jobs that changed state."""
        now = now or self._now()
        progressed: list[Job] = []
        for job in list(self._jobs.values()):
            if job.status is JobStatus.WAITING_FOR_AI:
                if job.resume_at is None or job.resume_at > now:
                    continue
                job.resume_at = None
                job.observe(JobStatus.QUEUED)
            if job.status is JobStatus.QUEUED and job.resume_at is not None:
                if job.resume_at > now:
                    continue
                job.resume_at = None
            if job.status is JobStatus.QUEUED and self._advance(job):
                progressed.append(job)
        return progressed

    def _advance(self, job: Job) -> bool:
        if self.cache is not None and job.recipe_hash and job.render_profile is not None:
            hit = self.cache.get(job.recipe_hash, job.render_profile)
            if hit is not None:
                job.result, job.cached = hit.to_result(), True
                job.observe(JobStatus.SUCCEEDED)
                return True
        executor = next((e for e in self.executors_for(job) if e.can_run_now(job.resources)), None)
        if executor is None:
            return False
        job.executor = executor.name
        job.attempts += 1
        job.observe(JobStatus.RUNNING)
        try:
            result = executor.run(job)
        except ProviderExhausted as exc:
            job.resume_at = exc.reset_at
            job.error = str(exc)
            job.observe(JobStatus.WAITING_FOR_AI)
            return True
        except ExecutorBusy as exc:
            job.observe(JobStatus.QUEUED)
            if exc.retry_at is not None:
                job.resume_at = exc.retry_at
                job.error = str(exc)
            return False
        except Exception as exc:  # noqa: BLE001 - an executor failure fails the job
            job.error = f"{type(exc).__name__}: {exc}"
            job.observe(JobStatus.FAILED)
            return True
        job.result, job.error = result, None
        profile = result.render_profile or job.render_profile
        if self.cache is not None and job.recipe_hash and profile is not None:
            self.cache.put(job.recipe_hash, profile, result)
        job.observe(JobStatus.SUCCEEDED)
        return True

    def run_until_idle(
        self, *, sleep: Callable[[float], None] = time.sleep, max_ticks: int = 100_000
    ) -> None:
        """Tick until every job is succeeded or failed, sleeping through AI waits."""
        for _ in range(max_ticks):
            progressed = self.tick()
            open_jobs = [j for j in self._jobs.values() if not j.done]
            if not open_jobs:
                return
            if progressed:
                continue
            waits = [j.resume_at for j in open_jobs if j.resume_at is not None]
            if not waits:
                raise QueueStalled(f"jobs stuck queued: {[j.id for j in open_jobs]}")
            sleep(max(0.0, (min(waits) - self._now()).total_seconds()))
        raise QueueStalled(f"not idle after {max_ticks} ticks")

    def as_runner(
        self,
        make_job: Callable[[RunContext], Job],
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> Runner:
        """A production-graph ``Runner`` that builds each node through this queue."""

        def runner(ctx: RunContext) -> RunResult:
            job = make_job(ctx)
            if job.node_id is None:
                job.node_id = ctx.spec.id
            self.submit(job)
            self.run_until_idle(sleep=sleep)
            if job.status is not JobStatus.SUCCEEDED or job.result is None:
                raise JobFailed(f"job {job.id} {job.status.value}: {job.error}")
            return RunResult(output=job.result.output, usage=job.result.usage)

        return runner
