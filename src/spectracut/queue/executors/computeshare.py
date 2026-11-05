# SPDX-License-Identifier: AGPL-3.0-or-later
"""``ComputeshareExecutor``: run jobs in computeshare sandboxes (optional, off by default).

A second executor next to the host pool. For each job it asks the computeshare
router (``POST /sandboxes``) for a sandbox running ``spectracut-host --once
--job <id>``. The sandbox pulls the signed job over the normal host protocol
(``SPECTRACUT_CONTROL_URL``) and writes frames straight to SpectraCut storage;
only the small JSON result manifest comes back through the router.

- **Lease:** the router renews a sandbox's lease only while its client keeps
  polling ``GET /sandboxes/:id``, so the executor polls every
  ``poll_interval_s`` (well under the router's 10-minute lease) while the job
  runs.
- **Cleanup:** the sandbox is deleted (``DELETE /sandboxes/:id``) when the job
  ends, fails, times out or is cancelled.
- **Busy router:** a ``503`` raises
  :class:`~spectracut.queue.executors.base.ExecutorBusy` with ``retry_at`` from
  ``Retry-After``; the queue keeps the job queued until then.
- **CPU only for now:** GPU placement waits on the computeshare GPU work, so
  jobs asking for ``gpus > 0`` are never offered here.
- **Off by default:** :func:`computeshare_executor` returns ``None`` unless
  ``[integrations.computeshare]`` has ``enabled = true``. The router token is
  read from the environment variable named by ``token_env``, never from config.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any, Final, Protocol

from pydantic import ValidationError

from spectracut.queue.executors.base import ExecutorBusy, JobResult, JobStatus, Resources
from spectracut.queue.profile import RenderProfile

if TYPE_CHECKING:
    from spectracut.queue.queue import Job

DEFAULT_IMAGE: Final = "spectracut-worker:cpu"
DEFAULT_TOKEN_ENV: Final = "COMPUTESHARE_TOKEN"
DEFAULT_OUTPUT_DIR: Final = "/srv/spectracut/output"
DEFAULT_RESULT_FILE: Final = "result.json"
DEFAULT_RETRY_AFTER_S: Final = 60.0
CONTROL_URL_ENV: Final = "SPECTRACUT_CONTROL_URL"
HTTP_TIMEOUT_S: Final = 30.0

#: Sandbox states the router reports while the job is still going.
RUNNING_STATES: Final = frozenset({"starting", "running"})


class RouterBusy(RuntimeError):
    """The router answered ``503``: no host has a free reservation right now."""

    def __init__(self, retry_after_s: float | None, detail: str = "") -> None:
        self.retry_after_s = retry_after_s
        super().__init__(f"computeshare router busy{': ' + detail if detail else ''}")


class RouterError(RuntimeError):
    """Any other router failure (bad status, unreachable, bad JSON)."""


class ComputeshareJobFailed(RuntimeError):
    """The sandbox ended without a usable result."""


class ComputeshareTimeout(RuntimeError):
    """The sandbox did not finish within the executor's timeout."""


class ComputeshareCancelled(RuntimeError):
    """The job was cancelled while its sandbox ran."""


def parse_retry_after(value: str | None, *, now: datetime) -> float | None:
    """Seconds to wait from a ``Retry-After`` header (delta-seconds or HTTP-date)."""
    if value is None or not value.strip():
        return None
    text = value.strip()
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - now).total_seconds())


class ComputeshareApi(Protocol):
    """The router calls the executor makes."""

    def launch(self, spec: dict[str, Any]) -> dict[str, Any]:
        """``POST /sandboxes``. Raises :class:`RouterBusy` on ``503``."""
        ...

    def status(self, sandbox_id: str) -> dict[str, Any]:
        """``GET /sandboxes/:id``; ``{"state": "gone"}`` when unknown. Renews the lease."""
        ...

    def result(self, sandbox_id: str) -> dict[str, Any] | None:
        """``GET /sandboxes/:id/result``; ``None`` when there is no result."""
        ...

    def delete(self, sandbox_id: str) -> None:
        """``DELETE /sandboxes/:id``; a missing sandbox is not an error."""
        ...


class RouterClient:
    """Stdlib HTTP client for the computeshare router (bearer token from the env)."""

    def __init__(
        self,
        router_url: str,
        *,
        token_env: str = DEFAULT_TOKEN_ENV,
        timeout_s: float = HTTP_TIMEOUT_S,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        url = router_url.rstrip("/")
        if not url.startswith(("http://", "https://")):
            raise ValueError("computeshare router_url must be http(s)")
        self.router_url = url
        self.token_env = token_env
        self.timeout_s = timeout_s
        self._now = now

    def _request(
        self, method: str, path: str, body: Mapping[str, Any] | None = None
    ) -> tuple[int, Any]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{self.router_url}{path}", data=data, method=method)  # noqa: S310
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        token = os.environ.get(self.token_env)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:  # noqa: S310
                return int(resp.status), _json(resp.read())
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            if exc.code == 503:
                retry = parse_retry_after(exc.headers.get("Retry-After"), now=self._now())
                detail = _json(raw)
                msg = detail.get("error", "") if isinstance(detail, dict) else ""
                raise RouterBusy(retry, str(msg)) from exc
            return exc.code, _json(raw)
        except (urllib.error.URLError, OSError) as exc:
            raise RouterError(f"{method} {path}: {getattr(exc, 'reason', exc)}") from exc

    def launch(self, spec: dict[str, Any]) -> dict[str, Any]:
        code, body = self._request("POST", "/sandboxes", {"spec": spec})
        if not 200 <= code < 300:
            raise RouterError(f"POST /sandboxes returned {code}: {body}")
        return body if isinstance(body, dict) else {}

    def status(self, sandbox_id: str) -> dict[str, Any]:
        code, body = self._request("GET", f"/sandboxes/{_quote(sandbox_id)}")
        if code == 404:
            return {"state": "gone"}
        if not 200 <= code < 300 or not isinstance(body, dict):
            raise RouterError(f"GET /sandboxes/{sandbox_id} returned {code}")
        return body

    def result(self, sandbox_id: str) -> dict[str, Any] | None:
        code, body = self._request("GET", f"/sandboxes/{_quote(sandbox_id)}/result")
        if code == 404:
            return None
        if not 200 <= code < 300:
            raise RouterError(f"GET /sandboxes/{sandbox_id}/result returned {code}")
        return body if isinstance(body, dict) else None

    def delete(self, sandbox_id: str) -> None:
        code, _ = self._request("DELETE", f"/sandboxes/{_quote(sandbox_id)}")
        if code != 404 and not 200 <= code < 300:
            raise RouterError(f"DELETE /sandboxes/{sandbox_id} returned {code}")


def _quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def _json(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None


@dataclass(frozen=True)
class ComputeshareConfig:
    """``[integrations.computeshare]`` settings the executor uses."""

    router_url: str
    image: str = DEFAULT_IMAGE
    token_env: str = DEFAULT_TOKEN_ENV
    control_url: str | None = None
    """Control-plane URL the sandbox pulls the job from; defaults to $SPECTRACUT_CONTROL_URL."""
    output_dir: str = DEFAULT_OUTPUT_DIR
    """SpectraCut storage path, as seen inside the sandbox, the host writes frames to."""
    result_file: str = DEFAULT_RESULT_FILE
    poll_interval_s: float = 30.0
    timeout_s: float | None = None
    default_retry_after_s: float = DEFAULT_RETRY_AFTER_S
    capacity: Resources = field(default_factory=lambda: Resources(cpus=2.0, mem_gb=3.0))
    """Largest job one sandbox takes (the router's lane slice by default)."""

    @classmethod
    def from_table(cls, table: Mapping[str, Any]) -> ComputeshareConfig:
        router = str(table.get("router_url") or "").strip()
        if not router.startswith(("http://", "https://")):
            raise ValueError("[integrations.computeshare] needs an http(s) router_url")
        cap = table.get("capacity")
        capacity = Resources.model_validate(cap) if isinstance(cap, Mapping) else None
        timeout = table.get("timeout_s")
        kwargs: dict[str, Any] = {
            "router_url": router.rstrip("/"),
            "image": str(table.get("image") or DEFAULT_IMAGE),
            "token_env": str(table.get("token_env") or DEFAULT_TOKEN_ENV),
            "control_url": str(table["control_url"]) if table.get("control_url") else None,
            "output_dir": str(table.get("output_dir") or DEFAULT_OUTPUT_DIR),
            "result_file": str(table.get("result_file") or DEFAULT_RESULT_FILE),
            "poll_interval_s": float(table.get("poll_interval_s") or 30.0),
            "timeout_s": float(timeout) if timeout is not None else None,
            "default_retry_after_s": float(
                table.get("default_retry_after_s") or DEFAULT_RETRY_AFTER_S
            ),
        }
        if capacity is not None:
            kwargs["capacity"] = capacity
        return cls(**kwargs)


class ComputeshareExecutor:
    """Runs queue jobs as ``spectracut-host --once`` in computeshare sandboxes."""

    def __init__(
        self,
        config: ComputeshareConfig,
        client: ComputeshareApi | None = None,
        *,
        name: str = "computeshare",
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        on_status: Callable[[Job, JobStatus], None] | None = None,
    ) -> None:
        self.name = name
        self.config = config
        self.client: ComputeshareApi = client or RouterClient(
            config.router_url, token_env=config.token_env, now=now
        )
        self._sleep = sleep
        self._clock = clock
        self._now = now
        self._on_status = on_status
        self._cancelled: set[str] = set()
        self._lock = threading.Lock()

    # ---- capacity ----------------------------------------------------------

    def can_ever_run(self, req: Resources) -> bool:
        cap = self.config.capacity
        if req.gpus > 0:  # whole-GPU leases wait on computeshare#2
            return False
        return req.cpus <= cap.cpus and req.mem_gb <= cap.mem_gb

    def can_run_now(self, req: Resources) -> bool:
        # The router decides free capacity; a 503 parks the job until Retry-After.
        return self.can_ever_run(req)

    # ---- cancel ------------------------------------------------------------

    def cancel(self, job_id: str) -> None:
        """Ask a running job to stop; its sandbox is deleted on the next poll."""
        with self._lock:
            self._cancelled.add(job_id)

    def _take_cancel(self, job_id: str) -> bool:
        with self._lock:
            if job_id in self._cancelled:
                self._cancelled.discard(job_id)
                return True
            return False

    # ---- running -----------------------------------------------------------

    def sandbox_spec(self, job: Job) -> dict[str, Any]:
        """The ``POST /sandboxes`` spec for ``job`` (never carries a secret)."""
        cfg = self.config
        output_dir = str(job.payload.get("output_dir") or cfg.output_dir)
        env: dict[str, str] = {}
        control = cfg.control_url or os.environ.get(CONTROL_URL_ENV)
        if control:
            env[CONTROL_URL_ENV] = control
        res = job.resources
        return {
            "id": f"spectracut-{job.id}-{job.attempts}",
            "image": cfg.image,
            "command": ["spectracut-host", "--once", "--job", job.id, "--output", output_dir],
            "env": env,
            "result_file": cfg.result_file,
            "cpus": res.cpus,
            "mem_gb": res.mem_gb,
            "gpus": res.gpus,
            "vram_gb": res.vram_gb,
            "labels": {"spectracut.job": job.id, "spectracut.kind": job.kind},
        }

    def _observe(self, job: Job, status: JobStatus) -> None:
        job.observe(status)
        if self._on_status is not None:
            self._on_status(job, status)

    def run(self, job: Job) -> JobResult:
        self._take_cancel(job.id)  # a stale cancel must not kill a fresh attempt
        spec = self.sandbox_spec(job)
        sandbox_id = str(spec["id"])
        try:
            launched = self.client.launch(spec)
        except RouterBusy as exc:
            wait = (
                exc.retry_after_s
                if exc.retry_after_s is not None
                else self.config.default_retry_after_s
            )
            raise ExecutorBusy(
                f"{self.name}: {exc}; retry after {wait:g}s",
                retry_at=self._now() + timedelta(seconds=wait),
            ) from exc
        sandbox_id = str(launched.get("id") or sandbox_id)
        started = self._clock()
        try:
            result = self._wait(job, sandbox_id, launched, started)
        except BaseException:
            self._delete_quietly(sandbox_id)  # keep the original error
            raise
        self.client.delete(sandbox_id)
        return result

    def _delete_quietly(self, sandbox_id: str) -> None:
        try:
            self.client.delete(sandbox_id)
        except RouterError:
            pass  # the router destroys sandboxes nobody polls; never mask the job error

    def _wait(
        self, job: Job, sandbox_id: str, launched: Mapping[str, Any], started: float
    ) -> JobResult:
        timeout = self.config.timeout_s
        while True:
            if self._take_cancel(job.id):
                raise ComputeshareCancelled(f"job {job.id} cancelled (sandbox {sandbox_id})")
            state = str(self.client.status(sandbox_id).get("state") or "")
            if state == "gone":
                raise ComputeshareJobFailed(f"sandbox {sandbox_id} is gone")
            if state not in RUNNING_STATES:  # exited
                return self._result(job, sandbox_id, launched, started)
            self._observe(job, JobStatus.RUNNING)
            if timeout is not None and self._clock() - started >= timeout:
                raise ComputeshareTimeout(f"sandbox {sandbox_id} still {state} after {timeout:g}s")
            self._sleep(self.config.poll_interval_s)

    def _result(
        self, job: Job, sandbox_id: str, launched: Mapping[str, Any], started: float
    ) -> JobResult:
        manifest = self.client.result(sandbox_id)
        if not manifest:
            raise ComputeshareJobFailed(f"sandbox {sandbox_id} exited without a result")
        if manifest.get("job_id") not in (None, job.id):
            raise ComputeshareJobFailed(
                f"sandbox {sandbox_id} returned a result for job {manifest.get('job_id')!r}"
            )
        profile: RenderProfile | None = job.render_profile
        raw_profile = manifest.get("render_profile")
        if isinstance(raw_profile, Mapping):
            try:
                profile = RenderProfile.model_validate(raw_profile)
            except ValidationError:
                profile = job.render_profile
        frames = manifest.get("frames")
        output: dict[str, Any] = {
            "sandbox_id": sandbox_id,
            "agent": launched.get("agent"),
            "output_dir": str(job.payload.get("output_dir") or self.config.output_dir),
            "result": manifest,
            "minutes": max(0.0, self._clock() - started) / 60.0,
        }
        return JobResult(
            output=output,
            frames=[str(f) for f in frames] if isinstance(frames, list) else [],
            render_profile=profile,
        )


def computeshare_executor(
    table: Mapping[str, Any] | None,
    *,
    client: ComputeshareApi | None = None,
    **kwargs: Any,
) -> ComputeshareExecutor | None:
    """The executor for ``[integrations.computeshare]``, or ``None`` when it is off.

    Off unless ``enabled = true`` (the same rule ``spectracut doctor`` uses).
    """
    if not isinstance(table, Mapping) or table.get("enabled") is not True:
        return None
    return ComputeshareExecutor(ComputeshareConfig.from_table(table), client, **kwargs)


__all__ = [
    "ComputeshareApi",
    "ComputeshareCancelled",
    "ComputeshareConfig",
    "ComputeshareExecutor",
    "ComputeshareJobFailed",
    "ComputeshareTimeout",
    "RouterBusy",
    "RouterClient",
    "RouterError",
    "computeshare_executor",
    "parse_retry_after",
]
