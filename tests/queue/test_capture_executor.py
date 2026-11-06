# SPDX-License-Identifier: AGPL-3.0-or-later
"""CaptureExecutor: capture jobs run the Node CLI and return the frame list."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from spectracut.queue.executors.base import JobStatus, NoCapableExecutor, Resources
from spectracut.queue.executors.capture import CaptureExecutor, CaptureFailed, CaptureRun
from spectracut.queue.queue import Job, JobQueue


class FakeCli:
    def __init__(self, *, code: int = 0, write_manifest: bool = True) -> None:
        self.code = code
        self.write_manifest = write_manifest
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str], timeout: float | None) -> CaptureRun:
        self.calls.append(list(argv))
        spec = json.loads(Path(argv[-1]).read_text())
        if self.code:
            return CaptureRun(self.code, "", "boom: browser missing")
        if self.write_manifest:
            out = Path(spec["outDir"])
            manifest = {"fps": spec["fps"], "frames": [{"index": 0}, {"index": 1}]}
            (out / "manifest.json").write_text(json.dumps(manifest))
        return CaptureRun(0, "{}", "")


def _job(tmp_path: Path, **kw: object) -> Job:
    payload = {"spec": {"fps": 30, "script": []}, "out_dir": str(tmp_path / "cap")}
    return Job(id="cap-1", kind="capture", payload=payload, **kw)  # type: ignore[arg-type]


def test_capture_job_runs_cli_and_returns_frames(tmp_path: Path) -> None:
    cli = FakeCli()
    queue = JobQueue([CaptureExecutor(runner=cli)], route={"capture": ["capture"]})
    job = queue.submit(_job(tmp_path))
    queue.run_until_idle()
    assert job.status is JobStatus.SUCCEEDED
    assert job.result is not None
    assert [Path(f).name for f in job.result.frames] == ["000000.png", "000001.png"]
    assert cli.calls[0][0] == "spectracut-capture"


def test_cli_failure_fails_the_job_with_stderr(tmp_path: Path) -> None:
    executor = CaptureExecutor(runner=FakeCli(code=1))
    with pytest.raises(CaptureFailed, match="browser missing"):
        executor.run(_job(tmp_path))


def test_missing_manifest_is_a_failure(tmp_path: Path) -> None:
    executor = CaptureExecutor(runner=FakeCli(write_manifest=False))
    with pytest.raises(CaptureFailed, match="manifest"):
        executor.run(_job(tmp_path))


def test_bad_payload_is_rejected(tmp_path: Path) -> None:
    executor = CaptureExecutor(runner=FakeCli())
    with pytest.raises(CaptureFailed, match="out_dir"):
        executor.run(Job(id="cap-2", kind="capture", payload={}))


def test_resources_are_respected(tmp_path: Path) -> None:
    executor = CaptureExecutor(cpus=1.0, runner=FakeCli())
    assert not executor.can_ever_run(Resources(cpus=4.0))
    queue = JobQueue([executor], route={"capture": ["capture"]})
    with pytest.raises(NoCapableExecutor):
        queue.submit(_job(tmp_path, resources=Resources(cpus=4.0)))
