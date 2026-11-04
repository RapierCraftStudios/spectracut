# SPDX-License-Identifier: AGPL-3.0-or-later
"""Preview dispatch: run a recipe as a signed queue job on a render host.

The control plane never runs Blender. A preview is a signed :class:`JobEnvelope`
(the recipe, its hash, frame 1) placed on the :class:`JobQueue`, routed to a
:class:`HostPoolExecutor` whose transport hands the signed job file to the host
agent (``spectracut-host run``) as a subprocess. The host's execution guard
verifies the signature and the tool allowlist before it starts Blender. The
harness reads back the rendered frame and the steps' ``report.json``.

This module never imports ``bpy``, ``bpy_runtime`` or ``spectracut_host``.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import JsonValue

from spectracut.harness.errors import HarnessError, code_from_host_output
from spectracut.harness.recorder import DEFAULT_BLENDER_VERSION
from spectracut.queue.executors.base import JobResult, JobStatus
from spectracut.queue.executors.hostpool import HostPoolExecutor, HostSlot
from spectracut.queue.profile import RenderProfile
from spectracut.queue.queue import BLENDER, Job, JobQueue
from spectracut.queue.signing import (
    SIGNING_KEY_ENV,
    JobEnvelope,
    load_private_key,
    sign_job,
)
from spectracut.spec.recipe import Recipe, RecipeStep

BLENDER_ENV = "SPECTRACUT_BLENDER"
BLENDER_VERSION_ENV = "SPECTRACUT_BLENDER_VERSION"
HARNESS_DIR_ENV = "SPECTRACUT_HARNESS_DIR"
HOST_CMD_ENV = "SPECTRACUT_HOST_CMD"
TIMEOUT_ENV = "SPECTRACUT_PREVIEW_TIMEOUT"
DEFAULT_TIMEOUT_S = 600.0
JOB_TTL = timedelta(minutes=15)
FRAME_NAME = "frame_0001.png"

# Host agent exit codes (host/spectracut_host/agent.py): 2 refused, 3 render failed, 4 pull failed.
_EXIT_REFUSED, _EXIT_RENDER_FAILED, _EXIT_PULL_FAILED = 2, 3, 4

Step = tuple[str, dict[str, JsonValue]]


def default_host_cmd() -> list[str]:
    """``spectracut-host`` on PATH, else the host agent module in this interpreter."""
    found = shutil.which("spectracut-host")
    return [found] if found else [sys.executable, "-m", "spectracut_host.agent"]


@dataclass(frozen=True)
class HarnessConfig:
    """Where previews render. Secrets are not stored here: the signing key stays in env."""

    work_dir: Path
    blender: str = "blender"
    blender_version: str = DEFAULT_BLENDER_VERSION
    host_cmd: tuple[str, ...] = field(default_factory=lambda: tuple(default_host_cmd()))
    timeout_s: float = DEFAULT_TIMEOUT_S

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> HarnessConfig:
        env = os.environ if env is None else env
        work = env.get(HARNESS_DIR_ENV)
        work_dir = Path(work) if work else Path(tempfile.mkdtemp(prefix="spectracut-harness-"))
        host_cmd = shlex.split(env[HOST_CMD_ENV]) if env.get(HOST_CMD_ENV) else default_host_cmd()
        try:
            timeout = float(env.get(TIMEOUT_ENV) or DEFAULT_TIMEOUT_S)
        except ValueError:
            raise HarnessError("config.invalid", f"{TIMEOUT_ENV} must be a number") from None
        return cls(
            work_dir=work_dir,
            blender=env.get(BLENDER_ENV) or "blender",
            blender_version=env.get(BLENDER_VERSION_ENV) or DEFAULT_BLENDER_VERSION,
            host_cmd=tuple(host_cmd),
            timeout_s=timeout,
        )


@dataclass(frozen=True)
class PreviewResult:
    """One rendered preview: the frame and any steps' reports."""

    job_id: str
    recipe_hash: str
    frame: bytes
    reports: list[dict[str, Any]]
    output_dir: Path

    def report(self, tool: str) -> dict[str, Any]:
        found = [r["report"] for r in self.reports if r.get("tool") == tool]
        if not found:
            raise HarnessError("render.failed", f"the host returned no {tool} report")
        last: dict[str, Any] = found[-1]
        return last


def _host_env(env: Mapping[str, str]) -> dict[str, str]:
    """The host agent's env: everything but the control plane's signing key."""
    return {k: v for k, v in env.items() if k != SIGNING_KEY_ENV}


class PreviewDispatcher:
    """Signs preview jobs and runs them through the queue on the local host pool."""

    def __init__(
        self,
        config: HarnessConfig,
        *,
        signing_key: Ed25519PrivateKey | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.config = config
        self._key = signing_key
        self._env = os.environ if env is None else env
        self._count = 0
        self._errors: dict[str, HarnessError] = {}
        slot = HostSlot(
            name="local",
            profile=RenderProfile(blender_version=config.blender_version),
            cpus=1.0,
            mem_gb=0.0,
        )
        self.queue = JobQueue(
            [HostPoolExecutor([slot], self._run_on_host, name="hostpool")],
            route={BLENDER: ["hostpool"]},
        )

    def _signing_key(self) -> Ed25519PrivateKey:
        if self._key is None:
            try:
                self._key = load_private_key(self._env.get(SIGNING_KEY_ENV))
            except ValueError as exc:
                raise HarnessError("config.invalid", str(exc)) from None
        return self._key

    def job_recipe(self, recipe: Recipe, extra: Sequence[Step] = ()) -> Recipe:
        steps = [(s.tool, s.args) for s in recipe.steps] + list(extra)
        return recipe.model_copy(
            update={
                "steps": [RecipeStep(index=i, tool=t, args=a) for i, (t, a) in enumerate(steps)]
            }
        )

    def render(self, recipe: Recipe, extra: Sequence[Step] = ()) -> PreviewResult:
        """Render frame 1 of ``recipe`` plus ``extra`` steps on a render host."""
        job_recipe = self.job_recipe(recipe, extra)
        if not job_recipe.steps:
            raise HarnessError("recipe.empty", "nothing to render: no tool calls recorded yet")
        self._count += 1
        recipe_hash = job_recipe.content_hash
        job_id = f"preview-{self._count:04d}-{recipe_hash[7:19]}"
        now = datetime.now(UTC)
        envelope = JobEnvelope(
            job_id=job_id,
            recipe=job_recipe,
            recipe_hash=recipe_hash,
            frame_start=1,
            frame_end=1,
            output=job_id,
            issued_at=now,
            expires_at=now + JOB_TTL,
        )
        signed = sign_job(envelope, self._signing_key())
        job_dir = self.config.work_dir / "jobs" / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        job_file = job_dir / "job.json"
        job_file.write_text(signed.model_dump_json(), encoding="utf-8")
        output = job_dir / "out"
        job = Job(
            id=job_id,
            kind=BLENDER,
            recipe_hash=recipe_hash,
            payload={"job_file": str(job_file), "output": str(output)},
        )
        self.queue.submit(job)
        self.queue.run_until_idle()
        error = self._errors.pop(job_id, None)
        if error is not None:
            raise error
        if job.status is not JobStatus.SUCCEEDED or job.result is None:
            raise HarnessError("render.failed", f"preview job {job_id} {job.status}: {job.error}")
        frame_path = output / FRAME_NAME
        try:
            frame = frame_path.read_bytes()
        except OSError:
            raise HarnessError("render.failed", f"the host wrote no {FRAME_NAME}") from None
        report_path = output / "report.json"
        reports: list[dict[str, Any]] = []
        if report_path.exists():
            reports = json.loads(report_path.read_text(encoding="utf-8")).get("steps", [])
        return PreviewResult(job_id, recipe_hash, frame, reports, output)

    def _run_on_host(self, host: HostSlot, job: Job) -> JobResult:
        try:
            return self._invoke_host(job)
        except HarnessError as exc:
            # Keep the typed error: the queue only records the failure as text.
            self._errors[job.id] = exc
            raise

    def _invoke_host(self, job: Job) -> JobResult:
        job_file, output = str(job.payload["job_file"]), str(job.payload["output"])
        cmd = [
            *self.config.host_cmd,
            "run",
            "--job-file",
            job_file,
            "--output",
            output,
            "--blender",
            self.config.blender,
        ]
        try:
            proc = subprocess.run(
                cmd,
                env=_host_env(self._env),
                capture_output=True,
                text=True,
                timeout=self.config.timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise HarnessError(
                "render.timeout",
                f"preview job {job.id} did not finish within {self.config.timeout_s:g}s",
            ) from None
        except OSError as exc:
            raise HarnessError("host.unavailable", f"can't start the host agent: {exc}") from None
        if proc.returncode != 0:
            tail = "\n".join((proc.stderr or "").splitlines()[-10:])
            if proc.returncode == _EXIT_REFUSED:
                code = code_from_host_output(tail, "host.refused")
            elif proc.returncode == _EXIT_PULL_FAILED:
                code = "host.unavailable"
            else:
                code = code_from_host_output(tail, "render.failed")
            raise HarnessError(code, f"preview job {job.id} failed on the host: {tail}")
        frames = sorted(str(p) for p in Path(output).glob("frame_*.png"))
        return JobResult(output={"job_id": job.id}, frames=frames)
