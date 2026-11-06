# SPDX-License-Identifier: AGPL-3.0-or-later
"""``CaptureExecutor``: live-UI capture jobs run the Node ``spectracut-capture`` CLI.

A ``capture`` job's payload carries ``spec`` (the ``launchAndCapture`` options,
minus ``outDir``) and ``out_dir``. The executor leases CPU and memory, writes the
spec next to the frames, runs the CLI through an injected ``runner`` (a
subprocess by default, never a shell) and reads the frame manifest back.
"""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Final, NamedTuple

from spectracut.queue.executors.base import (
    ExecutorBusy,
    JobResult,
    LeasePool,
    Resources,
    metered_run,
)

if TYPE_CHECKING:
    from spectracut.queue.queue import Job

__all__ = ["CAPTURE_KIND", "CaptureExecutor", "CaptureFailed", "CaptureRun"]

CAPTURE_KIND: Final = "capture"
_STDERR_TAIL: Final = 2000


class CaptureFailed(RuntimeError):
    """The capture process exited non-zero, timed out or left no manifest."""


class CaptureRun(NamedTuple):
    """What the runner reports for one process."""

    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[Sequence[str], float | None], CaptureRun]


def subprocess_runner(argv: Sequence[str], timeout: float | None) -> CaptureRun:
    """Run ``argv`` without a shell; a timeout becomes returncode 124."""
    try:
        done = subprocess.run(  # noqa: S603 - argv list, no shell
            list(argv), capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return CaptureRun(124, "", f"capture timed out after {timeout}s")
    return CaptureRun(done.returncode, done.stdout, done.stderr)


class CaptureExecutor:
    """Runs ``capture`` jobs on this machine within a CPU and memory budget."""

    def __init__(
        self,
        *,
        cpus: float = 2.0,
        mem_gb: float = 4.0,
        command: Sequence[str] = ("spectracut-capture",),
        runner: Runner = subprocess_runner,
        timeout: float | None = 600.0,
        name: str = "capture",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self._command = tuple(command)
        self._runner = runner
        self._timeout = timeout
        self._clock = clock
        self._pool = LeasePool(cpus, mem_gb)

    def can_ever_run(self, req: Resources) -> bool:
        return self._pool.can_ever_run(req)

    def can_run_now(self, req: Resources) -> bool:
        return self._pool.can_run_now(req)

    def run(self, job: Job) -> JobResult:
        if not self._pool.can_run_now(job.resources):
            raise ExecutorBusy(f"{self.name}: no capacity for {job.resources.model_dump()} now")
        return metered_run(
            self._pool,
            job,
            lambda: self._capture(job),
            executor=self.name,
            host="local",
            clock=self._clock,
        )

    def _capture(self, job: Job) -> JobResult:
        spec = job.payload.get("spec")
        out_dir = job.payload.get("out_dir")
        if not isinstance(spec, dict) or not isinstance(out_dir, str) or not out_dir:
            raise CaptureFailed(f"job {job.id!r} needs a spec object and an out_dir")
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        spec_path = out / "spec.json"
        spec_path.write_text(json.dumps({**spec, "outDir": str(out)}), encoding="utf-8")
        run = self._runner([*self._command, str(spec_path)], self._timeout)
        if run.returncode != 0:
            tail = run.stderr.strip()[-_STDERR_TAIL:]
            raise CaptureFailed(f"capture exited {run.returncode}: {tail}")
        try:
            manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
            frames = [
                str(out / "frames" / f"{int(f['index']):06d}.png") for f in manifest["frames"]
            ]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise CaptureFailed(f"capture left no readable manifest in {out}: {exc}") from exc
        return JobResult(
            output={
                "out_dir": str(out),
                "manifest": str(out / "manifest.json"),
                "events": str(out / "events.json"),
                "fps": manifest.get("fps"),
            },
            frames=frames,
        )
