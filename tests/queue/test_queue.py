# SPDX-License-Identifier: AGPL-3.0-or-later
"""Job queue, executors, GPU leases, waiting_for_ai and the frame cache."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from spectracut.providers.base import ProviderError, ProviderExhausted
from spectracut.queue.cache import FrameCache
from spectracut.queue.executors.base import (
    Executor,
    ExecutorBusy,
    GpuCard,
    JobResult,
    JobStatus,
    LeasePool,
    NoCapableExecutor,
    Resources,
    metered_run,
)
from spectracut.queue.executors.hostpool import HostPoolExecutor, HostSlot
from spectracut.queue.profile import RenderProfile, frame_cache_key
from spectracut.queue.queue import Job, JobQueue, QueueStalled
from spectracut.studio import NodeSpec, ProductionGraph
from spectracut.studio.ledger import Usage

RECIPE = "sha256:" + "a" * 64
OTHER_RECIPE = "sha256:" + "b" * 64
PROFILE = RenderProfile(blender_version="4.5.3", gpu_model="NVIDIA GeForce RTX 4070 Ti", vram_gb=12)
OTHER_PROFILE = RenderProfile(blender_version="4.5.3", gpu_model="RTX 3070 Laptop", vram_gb=8)
T0 = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


class Clock:
    """Fake monotonic + wall clock; ``sleep`` advances both."""

    def __init__(self) -> None:
        self.t = 0.0

    def mono(self) -> float:
        return self.t

    def now(self) -> datetime:
        return T0 + timedelta(seconds=self.t)

    def sleep(self, seconds: float) -> None:
        self.t += seconds


Body = Callable[[Job], JobResult]


def _status(job: Job) -> JobStatus:
    """Read the status afresh (keeps mypy from narrowing it across ticks)."""
    return job.status


def _ok(job: Job) -> JobResult:
    return JobResult(output={"job": job.id})


class FakeExecutor:
    """Reference executor: one machine, in-process body."""

    def __init__(
        self,
        body: Body = _ok,
        *,
        cpus: float = 8,
        mem_gb: float = 32,
        cards: tuple[GpuCard, ...] = (),
        clock: Callable[[], float] = lambda: 0.0,
        name: str = "fake",
    ) -> None:
        self.name = name
        self.body = body
        self.pool = LeasePool(cpus, mem_gb, cards)
        self.clock = clock
        self.calls: list[str] = []

    def can_ever_run(self, req: Resources) -> bool:
        return self.pool.can_ever_run(req)

    def can_run_now(self, req: Resources) -> bool:
        return self.pool.can_run_now(req)

    def run(self, job: Job) -> JobResult:
        self.calls.append(job.id)
        return metered_run(
            self.pool,
            job,
            lambda: self.body(job),
            executor=self.name,
            host="local",
            clock=self.clock,
        )


MakeExecutor = Callable[..., Executor]


def _make_fake(
    body: Body = _ok,
    *,
    cpus: float = 8,
    mem_gb: float = 32,
    cards: tuple[GpuCard, ...] = (),
    clock: Callable[[], float] = lambda: 0.0,
    name: str = "fake",
) -> Executor:
    return FakeExecutor(body, cpus=cpus, mem_gb=mem_gb, cards=cards, clock=clock, name=name)


def _make_hostpool(
    body: Body = _ok,
    *,
    cpus: float = 8,
    mem_gb: float = 32,
    cards: tuple[GpuCard, ...] = (),
    clock: Callable[[], float] = lambda: 0.0,
    name: str = "hostpool",
) -> Executor:
    host = HostSlot(name="legion", profile=PROFILE, cpus=cpus, mem_gb=mem_gb, gpus=cards)
    return HostPoolExecutor([host], lambda _h, job: body(job), name=name, clock=clock)


EXECUTORS = pytest.mark.parametrize("make", [_make_fake, _make_hostpool], ids=["fake", "hostpool"])
CARD_12 = GpuCard(name="4070ti", vram_gb=12)
CARD_8 = GpuCard(name="3070", vram_gb=8)


# ---- executor conformance: the host pool passes the same suite as the fake --


@EXECUTORS
def test_executor_satisfies_protocol(make: MakeExecutor) -> None:
    assert isinstance(make(), Executor)


@EXECUTORS
def test_executor_runs_a_job(make: MakeExecutor) -> None:
    ex = make()
    result = ex.run(Job(id="j1", kind="cpu"))
    assert result.output == {"job": "j1"}


@EXECUTORS
def test_executor_refuses_what_it_can_never_run(make: MakeExecutor) -> None:
    ex = make(cpus=4, mem_gb=16, cards=(CARD_8,))
    assert ex.can_ever_run(Resources(cpus=4, mem_gb=16, gpus=1, vram_gb=8))
    assert not ex.can_ever_run(Resources(cpus=5))
    assert not ex.can_ever_run(Resources(mem_gb=17))
    assert not ex.can_ever_run(Resources(gpus=2))
    assert not ex.can_ever_run(Resources(gpus=1, vram_gb=12))


@EXECUTORS
def test_executor_leases_whole_gpus_and_releases_them(make: MakeExecutor) -> None:
    seen: list[bool] = []
    req = Resources(gpus=1, vram_gb=8)
    holder: list[Executor] = []

    def body(job: Job) -> JobResult:
        seen.append(holder[0].can_run_now(req))  # the only card is leased
        return JobResult(output=None)

    ex = make(body, cards=(CARD_8,))
    holder.append(ex)
    assert ex.can_run_now(req)
    ex.run(Job(id="g1", kind="model", resources=req))
    assert seen == [False]
    assert ex.can_run_now(req)


@EXECUTORS
def test_executor_releases_lease_on_failure(make: MakeExecutor) -> None:
    def body(job: Job) -> JobResult:
        raise RuntimeError("boom")

    ex = make(body, cards=(CARD_8,))
    req = Resources(gpus=1)
    with pytest.raises(RuntimeError, match="boom"):
        ex.run(Job(id="g1", kind="model", resources=req))
    assert ex.can_run_now(req)


@EXECUTORS
def test_executor_propagates_provider_exhausted(make: MakeExecutor) -> None:
    def body(job: Job) -> JobResult:
        raise ProviderExhausted("claude", T0)

    with pytest.raises(ProviderExhausted):
        make(body).run(Job(id="a1", kind="ai"))


@EXECUTORS
def test_executor_accounts_gpu_minutes(make: MakeExecutor) -> None:
    clock = Clock()

    def body(job: Job) -> JobResult:
        clock.sleep(120)
        return JobResult(output=None, usage=Usage(ai_usage={"claude": 1}))

    ex = make(body, cards=(CARD_12, CARD_8), clock=clock.mono)
    result = ex.run(Job(id="g2", kind="model", resources=Resources(gpus=2)))
    assert result.usage.gpu_minutes == pytest.approx(4.0)
    assert result.usage.ai_usage == {"claude": 1}


@EXECUTORS
def test_executor_busy_when_capacity_is_taken(make: MakeExecutor) -> None:
    inner: list[BaseException] = []
    holder: list[Executor] = []

    def body(job: Job) -> JobResult:
        if job.id == "outer":
            try:
                holder[0].run(Job(id="inner", kind="cpu", resources=Resources(cpus=8)))
            except ExecutorBusy as exc:
                inner.append(exc)
        return JobResult(output=None)

    ex = make(body, cpus=8)
    holder.append(ex)
    ex.run(Job(id="outer", kind="cpu", resources=Resources(cpus=8)))
    assert len(inner) == 1


# ---- host pool specifics ----------------------------------------------------


def test_hostpool_places_gpu_job_on_biggest_card_and_reports_profile() -> None:
    placed: list[str] = []
    legion = HostSlot(name="legion", profile=OTHER_PROFILE, cpus=8, mem_gb=16, gpus=(CARD_8,))
    big = HostSlot(name="mrdubey-1", profile=PROFILE, cpus=8, mem_gb=32, gpus=(CARD_12,))

    def dispatch(host: HostSlot, job: Job) -> JobResult:
        placed.append(host.name)
        return JobResult(output=None)

    ex = HostPoolExecutor([legion, big], dispatch)
    result = ex.run(Job(id="g", kind="model", resources=Resources(gpus=1)))
    assert placed == ["mrdubey-1"]
    assert result.render_profile == PROFILE
    ex.run(Job(id="c", kind="cpu"))
    assert placed[-1] == "legion"  # CPU work: first host that fits


# ---- resource routing in the queue -----------------------------------------


def test_jobs_only_go_to_executors_that_meet_them() -> None:
    cpu = FakeExecutor(name="cpu-box", cpus=4, mem_gb=8)
    gpu = FakeExecutor(name="gpu-box", cpus=8, mem_gb=32, cards=(CARD_12,))
    q = JobQueue([cpu, gpu], now=lambda: T0)
    small = q.submit(Job(id="small", kind="cpu", resources=Resources(cpus=2, mem_gb=4)))
    heavy = q.submit(Job(id="heavy", kind="model", resources=Resources(gpus=1, vram_gb=12)))
    big_mem = q.submit(Job(id="bigmem", kind="cpu", resources=Resources(cpus=2, mem_gb=16)))
    q.run_until_idle()
    assert (small.executor, heavy.executor, big_mem.executor) == ("cpu-box", "gpu-box", "gpu-box")
    assert cpu.calls == ["small"]
    assert all(j.status is JobStatus.SUCCEEDED for j in q.jobs)


def test_job_no_executor_can_meet_is_rejected() -> None:
    q = JobQueue([FakeExecutor(cards=(CARD_8,))])
    with pytest.raises(NoCapableExecutor):
        q.submit(Job(id="x", kind="model", resources=Resources(gpus=1, vram_gb=12)))
    with pytest.raises(NoCapableExecutor):
        q.submit(Job(id="y", kind="cpu", resources=Resources(cpus=64)))


def test_busy_executor_keeps_job_queued() -> None:
    ex = FakeExecutor(cards=(CARD_8,))
    q = JobQueue([ex], now=lambda: T0)
    lease = ex.pool.acquire(Resources(gpus=1), executor="fake", host="local", now=0)
    job = q.submit(Job(id="g", kind="model", resources=Resources(gpus=1)))
    assert q.tick() == []
    assert job.status is JobStatus.QUEUED and ex.calls == []
    with pytest.raises(QueueStalled):
        q.run_until_idle()
    ex.pool.release(lease)
    q.tick()
    assert _status(job) is JobStatus.SUCCEEDED


def test_blender_jobs_never_go_to_own_executors() -> None:
    q = JobQueue([FakeExecutor()])
    with pytest.raises(NoCapableExecutor):
        q.submit(Job(id="b", kind="blender"))


def test_executor_failure_fails_the_job() -> None:
    def body(job: Job) -> JobResult:
        raise RuntimeError("render host lost")

    q = JobQueue([FakeExecutor(body)], now=lambda: T0)
    job = q.submit(Job(id="f", kind="cpu"))
    q.tick()
    assert job.status is JobStatus.FAILED
    assert job.error == "RuntimeError: render host lost"


# ---- waiting_for_ai ----------------------------------------------------------


def test_provider_exhausted_parks_job_and_resumes_after_reset() -> None:
    clock = Clock()
    reset_at = T0 + timedelta(minutes=30)
    calls: list[datetime] = []

    def body(job: Job) -> JobResult:
        calls.append(clock.now())
        if len(calls) == 1:
            raise ProviderExhausted("claude", reset_at)
        return JobResult(output="critique done")

    q = JobQueue([FakeExecutor(body)], now=clock.now)
    job = q.submit(Job(id="critique", kind="ai"))
    q.tick()
    assert job.status is JobStatus.WAITING_FOR_AI
    assert job.resume_at == reset_at

    clock.sleep(29 * 60)
    assert q.tick() == []
    assert job.status is JobStatus.WAITING_FOR_AI and len(calls) == 1

    clock.sleep(60)
    q.tick()
    assert _status(job) is JobStatus.SUCCEEDED
    assert job.result is not None and job.result.output == "critique done"
    assert calls[1] >= reset_at
    assert job.status_history == [
        JobStatus.QUEUED,
        JobStatus.RUNNING,
        JobStatus.WAITING_FOR_AI,
        JobStatus.QUEUED,
        JobStatus.RUNNING,
        JobStatus.SUCCEEDED,
    ]


def test_run_until_idle_sleeps_until_reset() -> None:
    clock = Clock()
    reset_at = T0 + timedelta(hours=1)
    state = {"n": 0}

    def body(job: Job) -> JobResult:
        state["n"] += 1
        if state["n"] == 1:
            raise ProviderExhausted("codex", reset_at)
        return JobResult(output=None)

    q = JobQueue([FakeExecutor(body)], now=clock.now)
    job = q.submit(Job(id="w", kind="ai"))
    q.run_until_idle(sleep=clock.sleep)
    assert job.status is JobStatus.SUCCEEDED
    assert clock.now() == reset_at


def test_provider_exhausted_is_a_provider_error_with_aware_reset() -> None:
    exc = ProviderExhausted("claude", T0)
    assert isinstance(exc, ProviderError)
    assert exc.reset_at == T0 and T0.isoformat() in str(exc)
    with pytest.raises(ValueError):
        ProviderExhausted("claude", datetime(2026, 1, 1))  # noqa: DTZ001


# ---- frame cache -------------------------------------------------------------


def test_cached_job_completes_without_rendering(tmp_path: Path) -> None:
    cache = FrameCache(tmp_path)
    cache.put(RECIPE, PROFILE, JobResult(output={"frames": 3}, frames=["0001.png"]))
    ex = FakeExecutor()
    q = JobQueue([ex], cache=cache, now=lambda: T0)
    job = q.submit(Job(id="r", kind="cpu", recipe_hash=RECIPE, render_profile=PROFILE))
    q.run_until_idle()
    assert ex.calls == []
    assert job.status is JobStatus.SUCCEEDED and job.cached
    assert job.result is not None and job.result.frames == ["0001.png"]


def test_uncached_job_renders_once_then_hits_cache(tmp_path: Path) -> None:
    cache = FrameCache(tmp_path)
    ex = FakeExecutor()
    q = JobQueue([ex], cache=cache, now=lambda: T0)
    first = q.submit(Job(id="r1", kind="cpu", recipe_hash=RECIPE, render_profile=PROFILE))
    q.run_until_idle()
    second = q.submit(Job(id="r2", kind="cpu", recipe_hash=RECIPE, render_profile=PROFILE))
    q.run_until_idle()
    assert ex.calls == ["r1"]
    assert not first.cached and second.cached
    # a different profile is a different key: it renders
    q.submit(Job(id="r3", kind="cpu", recipe_hash=RECIPE, render_profile=OTHER_PROFILE))
    q.run_until_idle()
    assert ex.calls == ["r1", "r3"]


def test_frame_cache_keys_by_recipe_and_profile(tmp_path: Path) -> None:
    cache = FrameCache(tmp_path)
    entry = cache.put(RECIPE, PROFILE, JobResult(output=1))
    assert entry.key == frame_cache_key(RECIPE, PROFILE)
    assert (RECIPE, PROFILE) in cache
    assert (RECIPE, OTHER_PROFILE) not in cache
    assert (OTHER_RECIPE, PROFILE) not in cache
    reopened = FrameCache(tmp_path).get(RECIPE, PROFILE)
    assert reopened == entry
    assert not list(tmp_path.rglob("*.tmp"))


def test_corrupt_manifest_is_a_miss(tmp_path: Path) -> None:
    cache = FrameCache(tmp_path)
    cache.put(RECIPE, PROFILE, JobResult(output=1))
    (manifest,) = tmp_path.rglob("manifest.json")
    manifest.write_text("{not json")
    assert cache.get(RECIPE, PROFILE) is None
    cache.put(RECIPE, PROFILE, JobResult(output=2))
    hit = cache.get(RECIPE, PROFILE)
    assert hit is not None and hit.output == 2


def test_job_validates_ids_and_hash() -> None:
    with pytest.raises(ValueError):
        Job(id="Bad Id", kind="cpu")
    with pytest.raises(ValueError):
        Job(id="ok", kind="cpu", recipe_hash="md5:x")


def test_gpu_only_host_gets_no_build_lanes_and_capped_allowance() -> None:
    from spectracut.queue.executors.hostpool import computeshare_allowance

    gpu_only = HostSlot(
        name="main-pc", profile=PROFILE, cpus=8, mem_gb=4, gpus=(CARD_12,), build_lanes=False
    )
    ex = HostPoolExecutor([gpu_only], lambda _h, job: JobResult(output=job.id))
    assert ex.pick(Resources(), build=True) is None
    with pytest.raises(ExecutorBusy):
        ex.run(Job(id="b1", kind="build"))
    assert ex.run(Job(id="r1", kind="blender", resources=Resources(gpus=1))).output == "r1"
    assert computeshare_allowance(gpu_only)["build_lanes"] is False
    assert computeshare_allowance(gpu_only)["mem_gb"] == 4


# ---- thread safety (#191) ----------------------------------------------------


def _two_node_graph() -> ProductionGraph:
    graph = ProductionGraph("prod-1", now=lambda: T0)
    graph.add_node(NodeSpec(id="a", kind="render", department="lighting"))
    graph.add_node(NodeSpec(id="b", kind="render", department="lighting"))
    return graph


def test_shared_queue_runs_each_parallel_job_exactly_once() -> None:
    both_running = threading.Barrier(2, timeout=10)

    def body(job: Job) -> JobResult:
        both_running.wait()  # only passes if the two jobs really overlap
        return JobResult(output={"job": job.id})

    ex = FakeExecutor(body)
    q = JobQueue([ex], now=lambda: T0)
    runner = q.as_runner(lambda ctx: Job(id=f"{ctx.spec.id}-job", kind="model"), sleep=time.sleep)
    graph = _two_node_graph()
    report = graph.run(runner, max_workers=2)
    assert sorted(report.executed) == ["a", "b"] and not report.failed
    assert sorted(ex.calls) == ["a-job", "b-job"]
    assert [j.attempts for j in q.jobs] == [1, 1]


def test_run_until_idle_scoped_to_own_job_ignores_job_running_elsewhere() -> None:
    started, release = threading.Event(), threading.Event()

    def body(job: Job) -> JobResult:
        if job.id == "slow":
            started.set()
            assert release.wait(10)
        return _ok(job)

    q = JobQueue([FakeExecutor(body)], now=lambda: T0)
    slow = q.submit(Job(id="slow", kind="model"))
    t = threading.Thread(target=q.run_until_idle)
    t.start()
    try:
        assert started.wait(10)
        fast = q.submit(Job(id="fast", kind="model"))
        q.run_until_idle(jobs=["fast"], sleep=lambda _s: None)  # no QueueStalled
        assert fast.status is JobStatus.SUCCEEDED
        # unscoped waiters also wait for the foreign running job instead of stalling
        waiter = threading.Thread(target=q.run_until_idle)
        waiter.start()
        release.set()
        waiter.join(10)
        assert not waiter.is_alive()
    finally:
        release.set()
        t.join(10)
    assert slow.status is JobStatus.SUCCEEDED and slow.attempts == 1


def test_lease_pool_never_double_leases_a_card_under_contention() -> None:
    pool = LeasePool(64, 256, (CARD_8,))
    n = 16
    start = threading.Barrier(n, timeout=10)
    leases: list[object] = []
    busy: list[int] = []

    def grab() -> None:
        start.wait()
        try:
            leases.append(pool.acquire(Resources(gpus=1), executor="x", host="h", now=0))
        except ExecutorBusy:
            busy.append(1)

    threads = [threading.Thread(target=grab) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert len(leases) == 1 and len(busy) == n - 1
    pool.release(leases[0])  # type: ignore[arg-type]
    assert pool.free_cpus == 64 and pool.can_run_now(Resources(gpus=1))
