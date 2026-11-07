# SPDX-License-Identifier: AGPL-3.0-or-later
"""Flamenco integration: worker-tag routing, ``spectracut-shot`` jobs, REST client.

Blender jobs are dispatched by Flamenco. The control plane submits a
``spectracut-shot`` job pointing at a signed job file on shared storage; the job
type compiler (``flamenco_jobtypes/spectracut-shot.js``) turns it into tasks that
run ``spectracut-host run`` on the workers. Each job carries one worker tag
(a card such as ``4070ti`` or a capability such as ``eevee``), so it only runs
on workers that have that tag. :func:`worker_tags` derives a host's tags from its
render profile; ``spectracut setup`` creates them in the Manager and assigns them.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Final, cast

from spectracut.queue.profile import RenderProfile
from spectracut.queue.signing import JobEnvelope

JOB_TYPE: Final = "spectracut-shot"
JOB_TYPE_SCRIPT: Final = "spectracut-shot.js"
DEFAULT_CHUNK_SIZE: Final = 10
JOB_TYPES_DIR: Final = "flamenco_jobtypes"
ERROR_BODY_LIMIT: Final = 500  # characters of an HTTP error body kept in FlamencoError
DEFAULT_CAPABILITIES: tuple[str, ...] = ("eevee", "cycles", "workbench")

_VENDOR_WORDS = re.compile(r"\b(nvidia|geforce|quadro|amd|radeon|intel|arc|rtx|gtx)\b")


class NoMatchingWorkerTag(LookupError):
    """No Flamenco worker tag matches the requested card or capability."""


class FlamencoError(RuntimeError):
    """The Flamenco Manager could not be reached or rejected a request."""


@dataclass(frozen=True)
class WorkerTag:
    """A Flamenco worker tag as returned by ``GET /api/v3/worker-mgt/tags``."""

    id: str
    name: str
    description: str = ""


@dataclass(frozen=True)
class Worker:
    """A Flamenco worker as listed by ``GET /api/v3/worker-mgt/workers``."""

    id: str
    name: str
    status: str = ""


def gpu_slug(gpu_model: str) -> str | None:
    """``NVIDIA GeForce RTX 4070 Ti`` -> ``4070ti``; ``None`` when unknown."""
    if gpu_model == "unknown":
        return None
    slug = re.sub(r"[^a-z0-9]", "", _VENDOR_WORDS.sub("", gpu_model.lower()))
    return slug or None


def worker_tags(
    profile: RenderProfile, capabilities: Iterable[str] = DEFAULT_CAPABILITIES
) -> list[str]:
    """Flamenco worker tags for a host: GPU model, VRAM and capabilities."""
    tags: list[str] = []
    slug = gpu_slug(profile.gpu_model)
    if slug:
        tags.append(slug)
    if profile.vram_gb > 0:
        tags.append(f"vram-{profile.vram_gb}gb")
    for cap in capabilities:
        cap = cap.strip().lower()
        if cap and cap not in tags:
            tags.append(cap)
    return tags


def _tag(t: dict[str, Any]) -> WorkerTag:
    return WorkerTag(
        id=str(t["id"]), name=str(t["name"]), description=str(t.get("description", ""))
    )


def normalize_tag(name: str) -> str:
    """Lower-case a tag name and drop separators, so ``RTX 4070 Ti`` style input compares."""
    return re.sub(r"[\s_-]+", "", name.strip().lower())


def select_worker_tag(requirement: str, tags: Sequence[WorkerTag]) -> WorkerTag:
    """Return the tag named ``requirement`` (e.g. ``4070ti`` or ``eevee``).

    Raises :class:`NoMatchingWorkerTag` instead of falling back to an untagged
    job, which Flamenco would hand to any worker.
    """
    wanted = normalize_tag(requirement)
    if not wanted:
        raise NoMatchingWorkerTag("empty worker tag requirement")
    for tag in tags:
        if normalize_tag(tag.name) == wanted:
            return tag
    known = ", ".join(sorted(t.name for t in tags)) or "none"
    raise NoMatchingWorkerTag(f"no worker tag {requirement!r} (known: {known})")


def build_shot_job(
    signed_job_path: str,
    envelope: JobEnvelope,
    tag: WorkerTag,
    *,
    output_dir: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    priority: int = 50,
    assets_dir: str | None = None,
) -> dict[str, Any]:
    """Build the Flamenco ``SubmittedJob`` for one signed shot job.

    ``assets_dir`` (a path on shared storage) becomes the optional ``assets_dir``
    setting; Flamenco's two-way storage variables map it per worker platform.
    """
    if chunk_size < 1:
        raise ValueError("chunk_size must be >= 1")
    if envelope.required_tag is not None and normalize_tag(envelope.required_tag) != (
        normalize_tag(tag.name)
    ):
        raise ValueError(f"job requires worker tag {envelope.required_tag!r}, not {tag.name!r}")
    settings: dict[str, Any] = {
        "job_file": signed_job_path,
        "frames": f"{envelope.frame_start}-{envelope.frame_end}",
        "chunk_size": chunk_size,
        "output_dir": output_dir,
    }
    if assets_dir:
        settings["assets_dir"] = assets_dir
    return {
        "name": f"{envelope.job_id} ({envelope.recipe.shot_id})",
        "type": JOB_TYPE,
        "priority": priority,
        "submitter_platform": "linux",
        "worker_tag": tag.id,
        "settings": settings,
        "metadata": {
            "spectracut.job_id": envelope.job_id,
            "spectracut.recipe_hash": envelope.recipe_hash,
        },
    }


def job_type_script() -> Path:
    """Path of the ``spectracut-shot`` compiler to install in the Manager's ``scripts/``."""
    return Path(str(resources.files("spectracut.queue") / JOB_TYPES_DIR / JOB_TYPE_SCRIPT))


def job_type_sources() -> dict[str, str]:
    """Every packaged custom job type: file name -> content, sorted by name.

    ``spectracut setup`` installs these into the Manager's ``scripts/`` folder.
    """
    folder = resources.files("spectracut.queue") / JOB_TYPES_DIR
    files = sorted((f for f in folder.iterdir() if f.name.endswith(".js")), key=lambda f: f.name)
    return {f.name: f.read_text(encoding="utf-8") for f in files}


def job_type_names() -> list[str]:
    """The job type names the Manager lists for :func:`job_type_sources` (the file stems)."""
    return [name.removesuffix(".js") for name in job_type_sources()]


def _error_body(exc: urllib.error.HTTPError) -> str:
    """The response body of an HTTP error, decoded, stripped and truncated ("" if none)."""
    try:
        raw = exc.read()
    except (OSError, ValueError):
        return ""
    text = (raw or b"").decode("utf-8", errors="replace").strip()
    if len(text) > ERROR_BODY_LIMIT:
        text = text[:ERROR_BODY_LIMIT] + "…"
    return text


class FlamencoClient:
    """Minimal client for the Flamenco Manager REST API (v3)."""

    def __init__(self, base_url: str, *, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(self, method: str, path: str, body: Any = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            body = _error_body(exc)
            detail = f": {body}" if body else ""
            raise FlamencoError(f"{method} {path} failed: HTTP {exc.code}{detail}") from None
        except (urllib.error.URLError, OSError) as exc:
            raise FlamencoError(f"{method} {path} failed: {exc}") from None
        return json.loads(raw) if raw else None

    def _request_text(self, path: str) -> str:
        """``GET path`` read as text (the task log tail is ``text/plain``)."""
        req = urllib.request.Request(self.base_url + path, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            body = _error_body(exc)
            detail = f": {body}" if body else ""
            raise FlamencoError(f"GET {path} failed: HTTP {exc.code}{detail}") from None
        except (urllib.error.URLError, OSError) as exc:
            raise FlamencoError(f"GET {path} failed: {exc}") from None
        return bytes(raw).decode("utf-8", errors="replace")

    def list_tags(self) -> list[WorkerTag]:
        payload = cast(dict[str, Any], self._request("GET", "/api/v3/worker-mgt/tags") or {})
        return [_tag(t) for t in payload.get("tags", [])]

    def create_tag(self, name: str, description: str = "") -> None:
        self._request("POST", "/api/v3/worker-mgt/tags", {"name": name, "description": description})

    def delete_tag(self, tag_id: str) -> None:
        self._request("DELETE", f"/api/v3/worker-mgt/tag/{tag_id}")

    def list_workers(self) -> list[Worker]:
        payload = cast(dict[str, Any], self._request("GET", "/api/v3/worker-mgt/workers") or {})
        return [
            Worker(id=str(w["id"]), name=str(w["name"]), status=str(w.get("status", "")))
            for w in payload.get("workers", [])
        ]

    def worker_tags_of(self, worker_id: str) -> list[WorkerTag]:
        """The tags a worker is a member of (``GET /api/v3/worker-mgt/workers/{id}``)."""
        worker = cast(
            dict[str, Any], self._request("GET", f"/api/v3/worker-mgt/workers/{worker_id}") or {}
        )
        return [_tag(t) for t in worker.get("tags") or []]

    def set_worker_tags(self, worker_id: str, tag_ids: Sequence[str]) -> None:
        """Replace the worker's tag membership (``POST .../workers/{id}/settags``)."""
        self._request(
            "POST",
            f"/api/v3/worker-mgt/workers/{worker_id}/settags",
            {"tag_ids": list(tag_ids)},
        )

    def job_types(self) -> list[str]:
        """Names of the job types the Manager knows (``GET /api/v3/jobs/types``)."""
        payload = cast(dict[str, Any], self._request("GET", "/api/v3/jobs/types") or {})
        return [str(t["name"]) for t in payload.get("job_types") or []]

    def submit(self, job: dict[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], self._request("POST", "/api/v3/jobs", job))

    def job_status(self, job_id: str) -> str:
        job = cast(dict[str, Any], self._request("GET", f"/api/v3/jobs/{job_id}"))
        return str(job["status"])

    def job_tasks(self, job_id: str) -> list[dict[str, Any]]:
        """The job's task summaries (``GET /api/v3/jobs/{id}/tasks``): id, name, status."""
        payload = cast(dict[str, Any], self._request("GET", f"/api/v3/jobs/{job_id}/tasks") or {})
        return [dict(t) for t in payload.get("tasks") or []]

    def task_log_tail(self, task_id: str) -> str:
        """The last lines of a task's log (``GET /api/v3/tasks/{id}/logtail``); ``''`` if none."""
        return self._request_text(f"/api/v3/tasks/{task_id}/logtail")

    def cancel_job(self, job_id: str, reason: str) -> None:
        """Ask the Manager to cancel a job (``POST /api/v3/jobs/{id}/setstatus``)."""
        self._request(
            "POST",
            f"/api/v3/jobs/{job_id}/setstatus",
            {"status": "cancel-requested", "reason": reason},
        )
