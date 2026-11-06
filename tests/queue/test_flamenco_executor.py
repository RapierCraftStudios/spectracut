# SPDX-License-Identifier: AGPL-3.0-or-later
"""FlamencoExecutor: Blender jobs go to Flamenco, status mirrors into the graph."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from spectracut.queue.cache import FrameCache
from spectracut.queue.executors.base import JobResult, JobStatus, Resources
from spectracut.queue.executors.flamenco import (
    TIER_PRIORITY,
    FlamencoExecutor,
    FlamencoJobFailed,
    FlamencoTimeout,
    map_status,
)
from spectracut.queue.flamenco import JOB_TYPE, WorkerTag
from spectracut.queue.profile import RenderProfile
from spectracut.queue.queue import Job, JobQueue
from spectracut.queue.signing import JobEnvelope
from spectracut.spec.recipe import Recipe, RecipeStep
from spectracut.studio import NodeSpec, NodeStatus, ProductionGraph, RunContext, Tier

PROFILE = RenderProfile(blender_version="4.5.3", gpu_model="NVIDIA GeForce RTX 4070 Ti", vram_gb=12)
T0 = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
TAGS = [WorkerTag(id="uuid-4070", name="4070ti"), WorkerTag(id="uuid-eevee", name="eevee")]


def _envelope(required_tag: str | None = "4070ti") -> JobEnvelope:
    recipe = Recipe(
        id="r1",
        shot_id="shot-1",
        blender_version="4.5",
        steps=[RecipeStep(index=0, tool="scene.clear", args={})],
    )
    return JobEnvelope(
        job_id="job-1",
        recipe=recipe,
        recipe_hash=recipe.content_hash,
        frame_start=1,
        frame_end=24,
        output="shot-1",
        required_tag=required_tag,
        issued_at=T0,
        expires_at=T0 + timedelta(hours=1),
    )


class FakeFlamenco:
    """Records calls; ``job_status`` replays a scripted status sequence."""

    def __init__(self, statuses: list[str]) -> None:
        self.statuses = list(statuses)
        self.submitted: list[dict[str, Any]] = []
        self.tag_calls = 0
        self.status_calls = 0

    def list_tags(self) -> list[WorkerTag]:
        self.tag_calls += 1
        return list(TAGS)

    def submit(self, job: dict[str, Any]) -> dict[str, Any]:
        self.submitted.append(job)
        return {"id": f"fl-{len(self.submitted)}", **job}

    def job_status(self, job_id: str) -> str:
        self.status_calls += 1
        return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]


def _blender_job(job_id: str = "shot-1", *, tier: Tier = Tier.FINAL) -> Job:
    env = _envelope()
    return Job(
        id=job_id,
        kind="blender",
        tier=tier,
        resources=Resources(gpus=1, vram_gb=12),
        recipe_hash=env.recipe_hash,
        render_profile=PROFILE,
        payload={"job_file": "/shared/jobs/job-1.json", "envelope": env.model_dump(mode="json")},
    )


def _executor(client: FakeFlamenco, sleeps: list[float] | None = None) -> FlamencoExecutor:
    sink = sleeps if sleeps is not None else []
    return FlamencoExecutor(client, output_dir="/shared/frames", sleep=sink.append)


def test_blender_job_is_submitted_and_status_mirrors() -> None:
    client = FakeFlamenco(["queued", "queued", "active", "active", "completed"])
    sleeps: list[float] = []
    q = JobQueue([_executor(client, sleeps)], now=lambda: T0)
    job = q.submit(_blender_job())
    q.run_until_idle()

    (submitted,) = client.submitted
    assert submitted["type"] == JOB_TYPE
    assert submitted["worker_tag"] == "uuid-4070"
    assert submitted["priority"] == TIER_PRIORITY[Tier.FINAL]
    assert submitted["settings"]["job_file"] == "/shared/jobs/job-1.json"
    assert submitted["settings"]["frames"] == "1-24"
    assert submitted["settings"]["output_dir"] == "/shared/frames"
    assert job.status is JobStatus.SUCCEEDED and job.executor == "flamenco"
    assert job.status_history == [
        JobStatus.QUEUED,
        JobStatus.RUNNING,  # handed to Flamenco
        JobStatus.QUEUED,  # Flamenco: queued
        JobStatus.RUNNING,  # Flamenco: active
        JobStatus.SUCCEEDED,
    ]
    assert len(sleeps) == 4
    assert job.result is not None and job.result.output == {
        "flamenco_job_id": "fl-1",
        "status": "completed",
        "output_dir": "/shared/frames",
        "worker_tag": "4070ti",
    }


def _graph_with_shot() -> ProductionGraph:
    graph = ProductionGraph("prod-1", now=lambda: T0)
    graph.add_node(NodeSpec(id="shot-1", kind="render", department="lighting"))
    return graph


def _make_job(ctx: RunContext) -> Job:
    return _blender_job(f"{ctx.spec.id}-a{ctx.attempt}")


def test_completed_flamenco_job_makes_graph_node_clean() -> None:
    client = FakeFlamenco(["active", "completed"])
    q = JobQueue([_executor(client)], now=lambda: T0)
    graph = _graph_with_shot()
    report = graph.run(q.as_runner(_make_job, sleep=lambda _s: None))
    assert report.executed == ["shot-1"]
    assert graph.status("shot-1") is NodeStatus.CLEAN
    assert graph.output("shot-1") == q.job("shot-1-a1").result.output  # type: ignore[union-attr]
    assert q.job("shot-1-a1").node_id == "shot-1"


@pytest.mark.parametrize("final", ["failed", "canceled"])
def test_failed_flamenco_job_makes_graph_node_failed(final: str) -> None:
    client = FakeFlamenco(["active", final])
    q = JobQueue([_executor(client)], now=lambda: T0)
    graph = _graph_with_shot()
    report = graph.run(q.as_runner(_make_job, sleep=lambda _s: None))
    assert report.failed == ["shot-1"]
    assert graph.status("shot-1") is NodeStatus.FAILED
    error = graph.state("shot-1").error or ""
    assert f"flamenco job fl-1 ended {final}" in error
    assert q.job("shot-1-a1").status is JobStatus.FAILED


def test_cached_recipe_skips_flamenco_entirely(tmp_path: Path) -> None:
    cache = FrameCache(tmp_path)
    env = _envelope()
    cache.put(env.recipe_hash, PROFILE, JobResult(output={"frames": "cached"}))
    client = FakeFlamenco(["completed"])
    q = JobQueue([_executor(client)], cache=cache, now=lambda: T0)
    graph = _graph_with_shot()
    graph.run(q.as_runner(_make_job, sleep=lambda _s: None))
    assert client.submitted == [] and client.tag_calls == 0 and client.status_calls == 0
    assert graph.status("shot-1") is NodeStatus.CLEAN
    assert graph.output("shot-1") == {"frames": "cached"}
    assert q.job("shot-1-a1").cached


def test_rendered_recipe_is_cached_for_next_time(tmp_path: Path) -> None:
    cache = FrameCache(tmp_path)
    client = FakeFlamenco(["completed"])
    q = JobQueue([_executor(client)], cache=cache, now=lambda: T0)
    q.submit(_blender_job("first"))
    q.run_until_idle()
    second = q.submit(_blender_job("second"))
    q.run_until_idle()
    assert len(client.submitted) == 1
    assert second.cached and second.status is JobStatus.SUCCEEDED


def test_non_blender_kinds_never_go_to_flamenco() -> None:
    from spectracut.queue.executors.base import NoCapableExecutor

    client = FakeFlamenco(["completed"])
    q = JobQueue([_executor(client)], now=lambda: T0)
    for kind in ("ai", "model", "audio", "cpu"):
        with pytest.raises(NoCapableExecutor):
            q.submit(Job(id=f"j-{kind}", kind=kind))
    assert client.submitted == []


def test_tier_sets_flamenco_priority() -> None:
    client = FakeFlamenco(["completed"])
    q = JobQueue([_executor(client)], now=lambda: T0)
    q.submit(_blender_job("p", tier=Tier.PREVIEW))
    q.submit(_blender_job("h", tier=Tier.HERO))
    q.run_until_idle()
    assert [j["priority"] for j in client.submitted] == [
        TIER_PRIORITY[Tier.PREVIEW],
        TIER_PRIORITY[Tier.HERO],
    ]
    assert TIER_PRIORITY[Tier.PREVIEW] < TIER_PRIORITY[Tier.HERO]


def test_unknown_worker_tag_fails_the_job() -> None:
    client = FakeFlamenco(["completed"])
    q = JobQueue([_executor(client)], now=lambda: T0)
    job = _blender_job()
    job.payload["worker_tag"] = "4090"
    q.submit(job)
    q.run_until_idle()
    assert job.status is JobStatus.FAILED and "NoMatchingWorkerTag" in (job.error or "")
    assert client.submitted == []


def test_status_mapping() -> None:
    assert map_status("completed") is JobStatus.SUCCEEDED
    assert map_status("canceled") is JobStatus.FAILED
    assert map_status("paused") is JobStatus.QUEUED
    assert map_status("something-new") is JobStatus.RUNNING


def test_run_reports_gpu_minutes_submit_to_complete() -> None:
    client = FakeFlamenco(["queued", "active", "completed"])
    ticks = iter([100.0, 100.0 + 90.0])
    executor = FlamencoExecutor(
        client, output_dir="/shared/frames", sleep=lambda _s: None, clock=lambda: next(ticks)
    )
    result = executor.run(_blender_job())
    assert result.usage.gpu_minutes == pytest.approx(1.5)


def test_assets_dir_reaches_the_flamenco_job() -> None:
    client = FakeFlamenco(["completed"])
    executor = FlamencoExecutor(
        client, output_dir="/shared/frames", sleep=lambda _s: None, assets_dir="/shared/assets"
    )
    executor.run(_blender_job())
    job = _blender_job("shot-2")
    job.payload["assets_dir"] = "/shared/brand-assets"
    executor.run(job)
    first, second = client.submitted
    assert first["settings"]["assets_dir"] == "/shared/assets"
    assert second["settings"]["assets_dir"] == "/shared/brand-assets"
    plain = FakeFlamenco(["completed"])
    FlamencoExecutor(plain, output_dir="/o", sleep=lambda _s: None).run(_blender_job())
    assert "assets_dir" not in plain.submitted[0]["settings"]


class _Ticker:
    """Monotonic clock that advances only when the executor sleeps."""

    def __init__(self) -> None:
        self.t = 0.0

    def mono(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


@pytest.mark.parametrize("final", ["failed", "canceled"])
def test_failed_flamenco_job_reports_gpu_minutes(final: str) -> None:
    ticker = _Ticker()
    client = FakeFlamenco(["active", "active", final])
    executor = FlamencoExecutor(
        client,
        output_dir="/shared/frames",
        sleep=ticker.sleep,
        poll_interval=60.0,
        clock=ticker.mono,
    )
    with pytest.raises(FlamencoJobFailed, match=f"flamenco job fl-1 ended {final}") as info:
        executor.run(_blender_job())
    assert info.value.usage.gpu_minutes == pytest.approx(2.0)

    graph = _graph_with_shot()
    client = FakeFlamenco(["active", "active", final])
    executor.client = client
    q = JobQueue([executor], now=lambda: T0)
    graph.run(q.as_runner(_make_job, sleep=lambda _s: None))
    (entry,) = graph.ledger.entries()
    assert entry.outcome == "failed" and entry.gpu_minutes == pytest.approx(2.0)


def test_flamenco_timeout_reports_gpu_minutes() -> None:
    ticker = _Ticker()
    client = FakeFlamenco(["active"])
    executor = FlamencoExecutor(
        client,
        output_dir="/shared/frames",
        sleep=ticker.sleep,
        poll_interval=60.0,
        timeout_s=180.0,
        clock=ticker.mono,
    )
    with pytest.raises(FlamencoTimeout, match="still active after 180.0s") as info:
        executor.run(_blender_job())
    assert info.value.usage.gpu_minutes == pytest.approx(3.0)
