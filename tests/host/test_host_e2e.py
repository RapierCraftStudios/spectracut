# SPDX-License-Identifier: AGPL-3.0-or-later
"""Opt-in end-to-end checks on a real render host and a real Flamenco Manager.

Real Blender (run on a registered Blender 4.5 host)::

    SPECTRACUT_E2E=1 uv run pytest -m e2e tests/host/test_host_e2e.py -k blender

needs ``blender`` (4.5) on PATH or ``SPECTRACUT_BLENDER``.

Flamenco (Manager with native Windows and Linux workers on shared storage)::

    SPECTRACUT_E2E=1 SPECTRACUT_FLAMENCO_URL=http://manager:8080 \
    SPECTRACUT_FLAMENCO_TAGS=windows-4070ti,linux-3090 \
    SPECTRACUT_FLAMENCO_SHARED=/mnt/flamenco \
    uv run pytest -m e2e tests/host/test_host_e2e.py -k flamenco

``SPECTRACUT_FLAMENCO_SHARED`` is the shared-storage path as the workers see it.
After ``spectracut setup`` no hand-placed key is needed: the signing key is read
from ``<SPECTRACUT_HOME>/job-signing.key`` (``SPECTRACUT_JOB_SIGNING_KEY`` still
overrides it), and setup wrote each worker's ``host.json`` with the matching
public key.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from spectracut.queue.flamenco import FlamencoClient, build_shot_job, select_worker_tag
from spectracut.queue.signing import JobEnvelope, SignedJob, load_private_key, sign_job

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(os.environ.get("SPECTRACUT_E2E") != "1", reason="set SPECTRACUT_E2E=1"),
]

BLENDER = os.environ.get("SPECTRACUT_BLENDER") or shutil.which("blender")
HOST_BIN = shutil.which("spectracut-host", path=str(Path(sys.executable).parent))


def _env(registration: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("SPECTRACUT_")}
    env["SPECTRACUT_HOST_CONFIG"] = str(registration)
    assert BLENDER is not None
    env["SPECTRACUT_BLENDER"] = BLENDER
    return env


@pytest.mark.skipif(BLENDER is None, reason="needs Blender 4.5 on PATH or SPECTRACUT_BLENDER")
def test_blender_signed_job_renders_unsigned_refused(
    tmp_path: Path, registration: Path, sign: Callable[..., SignedJob]
) -> None:
    assert HOST_BIN is not None
    signed = sign(frames=(1, 2))
    job = tmp_path / "job-1.json"
    job.write_text(signed.model_dump_json())
    out = tmp_path / "out"
    ok = subprocess.run(
        [HOST_BIN, "run", "--job-file", str(job), "--output", str(out)],
        capture_output=True,
        text=True,
        env=_env(registration),
        check=False,
    )
    assert ok.returncode == 0, ok.stderr
    assert len(list(out.glob("frame_*"))) == 2
    assert json.loads((out / "result.json").read_text())["frame_cache_key"].startswith("sha256:")

    job.write_text(SignedJob(envelope=signed.envelope).model_dump_json())
    refused = subprocess.run(
        [HOST_BIN, "run", "--job-file", str(job), "--output", str(tmp_path / "refused")],
        capture_output=True,
        text=True,
        env=_env(registration),
        check=False,
    )
    assert refused.returncode == 2


@pytest.mark.skipif(BLENDER is None, reason="needs Blender 4.5 on PATH or SPECTRACUT_BLENDER")
def test_blender_once_pulls_one_job_runs_and_exits(
    tmp_path: Path, registration: Path, sign: Callable[..., SignedJob]
) -> None:
    assert HOST_BIN is not None
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    (jobs / "job-9.json").write_text(sign(job_id="job-9", frames=(1, 1)).model_dump_json())
    result = subprocess.run(
        [
            HOST_BIN,
            "--once",
            "--job",
            "job-9",
            "--jobs-dir",
            str(jobs),
            "--output",
            str(tmp_path / "o"),
        ],
        capture_output=True,
        text=True,
        env=_env(registration),
        check=False,
        timeout=600,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "o" / "result.json").exists()


FLAMENCO = os.environ.get("SPECTRACUT_FLAMENCO_URL")


@pytest.mark.skipif(not FLAMENCO, reason="set SPECTRACUT_FLAMENCO_URL")
def test_flamenco_recipe_renders_on_each_tagged_worker(
    make_envelope: Callable[..., JobEnvelope],
) -> None:
    assert FLAMENCO is not None
    shared = Path(os.environ["SPECTRACUT_FLAMENCO_SHARED"])
    key = load_private_key()
    client = FlamencoClient(FLAMENCO)
    tags = client.list_tags()
    for name in os.environ.get("SPECTRACUT_FLAMENCO_TAGS", "").split(","):
        tag = select_worker_tag(name, tags)
        envelope = make_envelope(job_id=f"e2e-{tag.name}", frames=(1, 2))
        job_file = shared / "jobs" / f"{envelope.job_id}.json"
        job_file.parent.mkdir(parents=True, exist_ok=True)
        job_file.write_text(sign_job(envelope, key).model_dump_json())
        out = shared / "out" / envelope.job_id
        submitted = client.submit(
            build_shot_job(str(job_file), envelope, tag, output_dir=str(out), chunk_size=1)
        )
        deadline = datetime.now(UTC) + timedelta(minutes=15)
        status = ""
        while datetime.now(UTC) < deadline:
            status = client.job_status(str(submitted["id"]))
            if status in {"completed", "failed", "canceled"}:
                break
            time.sleep(5)
        assert status == "completed", f"{tag.name}: {status}"
        assert len(list(out.glob("frame_*"))) == 2
