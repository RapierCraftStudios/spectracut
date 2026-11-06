# SPDX-License-Identifier: AGPL-3.0-or-later
"""ComputeshareExecutor: lease renewal, cleanup, 503/Retry-After and off-by-default."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from spectracut.queue.executors.base import (
    Executor,
    ExecutorBusy,
    GpuCard,
    JobResult,
    JobStatus,
    Resources,
)
from spectracut.queue.executors.computeshare import (
    ComputeshareCancelled,
    ComputeshareConfig,
    ComputeshareExecutor,
    ComputeshareJobFailed,
    ComputeshareTimeout,
    RouterBusy,
    RouterClient,
    computeshare_executor,
    parse_retry_after,
)
from spectracut.queue.executors.hostpool import HostPoolExecutor, HostSlot
from spectracut.queue.profile import RenderProfile
from spectracut.queue.queue import Job, JobQueue, QueueStalled

T0 = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
PROFILE = RenderProfile(blender_version="4.5.3", gpu_model="cpu", vram_gb=0)
ROUTER = "http://router.test"


def _status(job: Job) -> JobStatus:
    """Read the status afresh (keeps mypy from narrowing it across ticks)."""
    return job.status


class Clock:
    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def mono(self) -> float:
        return self.t

    def now(self) -> datetime:
        return T0 + timedelta(seconds=self.t)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


class FakeRouter:
    """Records every call; ``states`` is the sequence ``GET /sandboxes/:id`` returns."""

    def __init__(
        self,
        states: list[str] | None = None,
        *,
        result: dict[str, Any] | None = None,
        busy: list[float | None] | None = None,
    ) -> None:
        self.states = list(states or ["running", "exited"])
        self.result_body = result
        self.busy = list(busy or [])
        self.calls: list[tuple[str, str]] = []
        self.specs: list[dict[str, Any]] = []
        self.on_status: Any = None

    def launch(self, spec: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("POST", spec["id"]))
        if self.busy:
            raise RouterBusy(self.busy.pop(0), "no host has a free reservation")
        self.specs.append(spec)
        return {"id": spec["id"], "state": "starting", "agent": "legion"}

    def status(self, sandbox_id: str) -> dict[str, Any]:
        self.calls.append(("GET", sandbox_id))
        if self.on_status is not None:
            self.on_status()
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return {"id": sandbox_id, "state": state}

    def result(self, sandbox_id: str) -> dict[str, Any] | None:
        self.calls.append(("RESULT", sandbox_id))
        return self.result_body

    def delete(self, sandbox_id: str) -> None:
        self.calls.append(("DELETE", sandbox_id))

    def count(self, method: str) -> int:
        return sum(1 for m, _ in self.calls if m == method)


def manifest(job_id: str = "capture-1") -> dict[str, Any]:
    return {
        "job_id": job_id,
        "frames": ["0001.png", "0002.png"],
        "render_profile": PROFILE.model_dump(mode="json"),
    }


def make(router: FakeRouter, clock: Clock, **cfg: Any) -> ComputeshareExecutor:
    config = ComputeshareConfig(
        router_url=ROUTER,
        control_url="http://control.test",
        output_dir="/srv/spectracut/out",
        poll_interval_s=cfg.pop("poll_interval_s", 30.0),
        **cfg,
    )
    return ComputeshareExecutor(config, router, sleep=clock.sleep, clock=clock.mono, now=clock.now)


def capture_job(**kw: Any) -> Job:
    return Job(
        id=kw.pop("id", "capture-1"), kind="capture", resources=Resources(cpus=2, mem_gb=2), **kw
    )


# ---- protocol and launch spec -----------------------------------------------


def test_is_an_executor_and_launches_spectracut_host_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COMPUTESHARE_TOKEN", "s3cret-token")
    router, clock = FakeRouter(result=manifest()), Clock()
    ex = make(router, clock)
    assert isinstance(ex, Executor)
    job = capture_job()
    result = ex.run(job)
    spec = router.specs[0]
    assert spec["command"] == [
        "spectracut-host",
        "--once",
        "--job",
        "capture-1",
        "--output",
        "/srv/spectracut/out",
    ]
    assert spec["image"] == "spectracut-worker:cpu"
    assert spec["env"] == {"SPECTRACUT_CONTROL_URL": "http://control.test"}
    assert (spec["cpus"], spec["mem_gb"], spec["gpus"]) == (2, 2, 0)
    assert "s3cret-token" not in json.dumps(spec)
    assert result.frames == ["0001.png", "0002.png"]
    assert result.render_profile == PROFILE
    assert isinstance(result.output, dict) and result.output["output_dir"] == "/srv/spectracut/out"


def test_gpu_jobs_are_never_offered_and_cap_is_respected() -> None:
    ex = make(FakeRouter(), Clock())
    assert not ex.can_ever_run(Resources(cpus=1, gpus=1, vram_gb=8))
    assert not ex.can_ever_run(Resources(cpus=8))
    assert ex.can_ever_run(Resources(cpus=2, mem_gb=3))
    assert ex.can_run_now(Resources(cpus=1))


def test_gpu_gate_holds_for_can_run_now_and_leaves_the_router_untouched() -> None:
    # GPU placement waits on computeshare#2, #3, #5 and #6 (docs/setup/computeshare.md).
    router = FakeRouter()
    ex = make(router, Clock())
    for gpus in (1, 2):
        req = Resources(cpus=1, mem_gb=1, gpus=gpus, vram_gb=24)
        assert not ex.can_ever_run(req)
        assert not ex.can_run_now(req)
    assert not router.calls


# ---- lease renewal and cleanup (R2) ----------------------------------------


def test_polls_to_renew_the_lease_while_running_then_deletes_on_completion() -> None:
    router = FakeRouter(["starting", "running", "running", "running", "exited"], result=manifest())
    clock = Clock()
    job = capture_job()
    make(router, clock, poll_interval_s=30.0).run(job)
    assert router.count("GET") == 5
    assert clock.sleeps == [30.0] * 4  # one status poll (lease renewal) per interval
    assert router.calls[-1] == ("DELETE", "spectracut-capture-1-0")
    assert router.count("DELETE") == 1
    assert JobStatus.RUNNING in job.status_history


def test_deletes_sandbox_on_cancel() -> None:
    router, clock = FakeRouter(["running"]), Clock()
    ex = make(router, clock)
    polls = {"n": 0}

    def cancel_on_third_poll() -> None:
        polls["n"] += 1
        if polls["n"] == 3:
            ex.cancel("capture-1")

    router.on_status = cancel_on_third_poll
    with pytest.raises(ComputeshareCancelled):
        ex.run(capture_job())
    assert router.calls[-1] == ("DELETE", "spectracut-capture-1-0")
    assert router.count("DELETE") == 1


def test_deletes_sandbox_on_missing_result_and_on_timeout() -> None:
    router, clock = FakeRouter(["exited"], result=None), Clock()
    with pytest.raises(ComputeshareJobFailed):
        make(router, clock).run(capture_job())
    assert router.calls[-1][0] == "DELETE"

    router, clock = FakeRouter(["running"]), Clock()
    with pytest.raises(ComputeshareTimeout):
        make(router, clock, timeout_s=90.0).run(capture_job())
    assert router.calls[-1][0] == "DELETE"


def test_failed_job_in_queue_still_deletes_sandbox() -> None:
    router, clock = FakeRouter(["gone"]), Clock()
    q = JobQueue([make(router, clock)], now=clock.now)
    job = q.submit(capture_job())
    q.tick()
    assert job.status is JobStatus.FAILED
    assert "gone" in (job.error or "")
    assert router.count("DELETE") == 1


# ---- 503 + Retry-After (R3) --------------------------------------------------


def test_router_503_leaves_job_queued_and_retries_after_retry_after() -> None:
    router, clock = FakeRouter(busy=[120.0], result=manifest()), Clock()
    q = JobQueue([make(router, clock)], now=clock.now)
    job = q.submit(capture_job())

    assert q.tick() == []
    assert _status(job) is JobStatus.QUEUED
    assert job.resume_at == T0 + timedelta(seconds=120)
    assert router.count("POST") == 1

    clock.t = 119.0
    q.tick()
    assert _status(job) is JobStatus.QUEUED
    assert router.count("POST") == 1  # not retried before Retry-After

    clock.t = 120.0
    q.tick()
    assert _status(job) is JobStatus.SUCCEEDED
    assert router.count("POST") == 2
    assert job.resume_at is None and job.error is None


def test_run_until_idle_sleeps_through_retry_after_instead_of_stalling() -> None:
    router, clock = FakeRouter(busy=[45.0, None], result=manifest()), Clock()
    q = JobQueue([make(router, clock, default_retry_after_s=15.0)], now=clock.now)
    job = q.submit(capture_job())
    q.run_until_idle(sleep=clock.sleep)
    assert job.status is JobStatus.SUCCEEDED
    assert clock.sleeps[:2] == [45.0, 15.0]  # Retry-After, then the default when absent


def test_executor_busy_raises_with_retry_at() -> None:
    clock = Clock()
    with pytest.raises(ExecutorBusy) as info:
        make(FakeRouter(busy=[30.0]), clock).run(capture_job())
    assert info.value.retry_at == T0 + timedelta(seconds=30)


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("120", 120.0),
        (" 5 ", 5.0),
        (format_datetime(T0 + timedelta(seconds=90), usegmt=True), 90.0),
        ("garbage", None),
        (None, None),
        ("", None),
    ],
)
def test_parse_retry_after(header: str | None, expected: float | None) -> None:
    assert parse_retry_after(header, now=T0) == expected


# ---- off by default (R4, R5) -------------------------------------------------


@pytest.mark.parametrize("table", [None, {}, {"enabled": False}, {"enabled": "yes"}])
def test_unconfigured_or_disabled_gives_no_executor(table: dict[str, Any] | None) -> None:
    assert computeshare_executor(table) is None


def test_enabled_needs_http_router_url() -> None:
    with pytest.raises(ValueError, match="router_url"):
        computeshare_executor({"enabled": True})
    ex = computeshare_executor(
        {"enabled": True, "router_url": "http://router.test/", "image": "img:1"},
        client=FakeRouter(),
    )
    assert ex is not None and ex.config.router_url == ROUTER and ex.config.image == "img:1"


def test_unconfigured_queue_behaves_exactly_as_before() -> None:
    def run_once(extra: list[Executor]) -> list[tuple[str, list[JobStatus], Any]]:
        slot = HostSlot("h1", PROFILE, cpus=4, mem_gb=8, gpus=(GpuCard(name="g", vram_gb=8),))
        pool = HostPoolExecutor([slot], lambda h, j: JobResult(output={"host": h.name}))
        q = JobQueue([pool, *extra])
        jobs = [q.submit(Job(id=f"j{i}", kind="cpu")) for i in range(3)]
        q.run_until_idle(sleep=lambda s: None)
        return [(j.id, j.status_history, j.result and j.result.output) for j in jobs]

    off = computeshare_executor({"enabled": False})
    baseline = run_once([])
    assert off is None
    assert run_once([]) == baseline
    assert all(h[-1] is JobStatus.SUCCEEDED for _, h, _ in baseline)


def test_busy_without_retry_at_still_stalls_like_before() -> None:
    class Busy:
        name = "busy"

        def can_ever_run(self, req: Resources) -> bool:
            return True

        def can_run_now(self, req: Resources) -> bool:
            return True

        def run(self, job: Job) -> JobResult:
            raise ExecutorBusy("full")

    q = JobQueue([Busy()])
    job = q.submit(Job(id="j", kind="cpu"))
    with pytest.raises(QueueStalled):
        q.run_until_idle(sleep=lambda s: None)
    assert job.status is JobStatus.QUEUED and job.resume_at is None


# ---- RouterClient over real HTTP ---------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    seen: list[tuple[str, str, str | None, Any]] = []
    busy = True

    def log_message(self, *args: Any) -> None:
        pass

    def _send(self, code: int, body: Any, headers: dict[str, str] | None = None) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _record(self) -> Any:
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n)) if n else None
        self.seen.append((self.command, self.path, self.headers.get("Authorization"), body))
        return body

    def do_POST(self) -> None:
        body = self._record()
        if type(self).busy:
            type(self).busy = False
            self._send(503, {"error": "no host has a free reservation"}, {"Retry-After": "42"})
            return
        self._send(200, {"id": body["spec"]["id"], "state": "starting", "agent": "legion"})

    def do_GET(self) -> None:
        self._record()
        if self.path.endswith("/result"):
            self._send(200, manifest())
        elif "missing" in self.path:
            self._send(404, {"state": "gone"})
        else:
            self._send(200, {"state": "exited"})

    def do_DELETE(self) -> None:
        self._record()
        self._send(200, {"ok": True})


@pytest.fixture
def router_url() -> Iterator[str]:
    _Handler.seen, _Handler.busy = [], True
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_router_client_sends_bearer_token_and_maps_503(
    router_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SC_CS_TOKEN", "tok-123")
    client = RouterClient(router_url, token_env="SC_CS_TOKEN")
    with pytest.raises(RouterBusy) as info:
        client.launch({"id": "sb-1", "command": ["true"]})
    assert info.value.retry_after_s == 42.0
    assert client.launch({"id": "sb-1", "command": ["true"]})["agent"] == "legion"
    assert client.status("sb-1") == {"state": "exited"}
    assert client.status("missing") == {"state": "gone"}
    assert client.result("sb-1") == manifest()
    client.delete("sb-1")
    assert all(auth == "Bearer tok-123" for _, _, auth, _ in _Handler.seen)
    assert [(m, p) for m, p, _, _ in _Handler.seen][-1] == ("DELETE", "/sandboxes/sb-1")
    assert _Handler.seen[0][3] == {"spec": {"id": "sb-1", "command": ["true"]}}


def test_router_client_rejects_non_http_url() -> None:
    with pytest.raises(ValueError):
        RouterClient("ftp://router")
