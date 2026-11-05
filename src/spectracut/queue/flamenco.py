# SPDX-License-Identifier: AGPL-3.0-or-later
"""Flamenco integration: worker-tag routing, ``spectracut-shot`` jobs, REST client.

Blender jobs are dispatched by Flamenco. The control plane submits a
``spectracut-shot`` job pointing at a signed job file on shared storage; the job
type compiler (``flamenco_jobtypes/spectracut-shot.js``) turns it into tasks that
run ``spectracut-host run`` on the workers. Each job carries one worker tag
(a card such as ``4070ti`` or a capability such as ``eevee``), so it only runs
on workers that have that tag.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Final, cast

from spectracut.queue.signing import JobEnvelope

JOB_TYPE: Final = "spectracut-shot"
JOB_TYPE_SCRIPT: Final = "spectracut-shot.js"
DEFAULT_CHUNK_SIZE: Final = 10


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
    return Path(str(resources.files("spectracut.queue") / "flamenco_jobtypes" / JOB_TYPE_SCRIPT))


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
            raise FlamencoError(f"{method} {path} failed: HTTP {exc.code}") from None
        except (urllib.error.URLError, OSError) as exc:
            raise FlamencoError(f"{method} {path} failed: {exc}") from None
        return json.loads(raw) if raw else None

    def list_tags(self) -> list[WorkerTag]:
        payload = cast(dict[str, Any], self._request("GET", "/api/v3/worker-mgt/tags") or {})
        return [
            WorkerTag(
                id=str(t["id"]), name=str(t["name"]), description=str(t.get("description", ""))
            )
            for t in payload.get("tags", [])
        ]

    def submit(self, job: dict[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], self._request("POST", "/api/v3/jobs", job))

    def job_status(self, job_id: str) -> str:
        job = cast(dict[str, Any], self._request("GET", f"/api/v3/jobs/{job_id}"))
        return str(job["status"])
