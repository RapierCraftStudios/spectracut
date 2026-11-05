# SPDX-License-Identifier: AGPL-3.0-or-later
"""``spectracut-host``: run, ``--once --job <id>``, profile and tags.

Runs the installed console script as a subprocess, against a live local HTTP job
server for ``--once``, with a fake ``blender`` that runs the real bpy runtime
runner on a recording fake ``bpy``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from spectracut.queue.profile import RenderProfile, frame_cache_key
from spectracut.queue.signing import SignedJob

SignJob = Callable[..., SignedJob]

HOST_BIN = shutil.which("spectracut-host", path=str(Path(sys.executable).parent))


def host(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    assert HOST_BIN is not None, "spectracut-host console script is not installed (uv sync)"
    return subprocess.run(
        [HOST_BIN, *args], capture_output=True, text=True, env=env, check=False, timeout=120
    )


def records(path: str) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def write_job(tmp_path: Path, signed: SignedJob, name: str = "job-1.json") -> Path:
    path = tmp_path / "jobs" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text(signed.model_dump_json())
    return path


def test_run_renders_signed_job_and_reports_profile(
    tmp_path: Path, host_env: dict[str, str], sign: SignJob
) -> None:
    signed = sign()
    out = tmp_path / "out"
    result = host(
        host_env, "run", "--job-file", str(write_job(tmp_path, signed)), "--output", str(out)
    )
    assert result.returncode == 0, result.stderr
    assert sorted(p.name for p in out.glob("frame_*")) == [
        "frame_0001.png",
        "frame_0002.png",
        "frame_0003.png",
    ]
    report = json.loads((out / "result.json").read_text())
    profile = RenderProfile.model_validate(report["render_profile"])
    assert profile.blender_version == "4.5.3"
    assert profile.gpu_model == "NVIDIA GeForce RTX 4070 Ti"
    assert profile.gpu_driver == "560.94"
    assert report["frame_cache_key"] == frame_cache_key(signed.envelope.recipe_hash, profile)
    assert report["host_id"] == "legion"
    report_json = json.loads((out / "report.json").read_text())
    assert report["scene_graph_hash"] == report_json["scene_graph_hash"]
    assert len(report["scene_graph_hash"]) == 64
    (launch,) = records(host_env["FAKE_BLENDER_RECORD"])
    assert launch["argv"][:2] == ["-b", "--factory-startup"]
    assert "SPECTRACUT_HOST_CONFIG" not in launch["env"]


def test_run_chunk_writes_chunk_result(
    tmp_path: Path, host_env: dict[str, str], sign: SignJob
) -> None:
    job = write_job(tmp_path, sign(frames=(1, 10)))
    out = tmp_path / "out"
    result = host(host_env, "run", "--job-file", str(job), "--frames", "4-5", "--output", str(out))
    assert result.returncode == 0, result.stderr
    assert sorted(p.name for p in out.glob("frame_*")) == ["frame_0004.png", "frame_0005.png"]
    assert json.loads((out / "result-4-5.json").read_text())["frames"] == [4, 5]


def test_unsigned_job_is_refused_and_blender_never_starts(
    tmp_path: Path, host_env: dict[str, str], sign: SignJob
) -> None:
    unsigned = SignedJob(envelope=sign().envelope)
    out = tmp_path / "out"
    result = host(
        host_env, "run", "--job-file", str(write_job(tmp_path, unsigned)), "--output", str(out)
    )
    assert result.returncode == 2
    assert "unsigned" in result.stderr
    assert records(host_env["FAKE_BLENDER_RECORD"]) == []
    assert not out.exists()


def test_unregistered_host_is_refused(
    tmp_path: Path, host_env: dict[str, str], sign: SignJob
) -> None:
    env = {**host_env, "SPECTRACUT_HOST_CONFIG": str(tmp_path / "missing.json")}
    result = host(env, "run", "--job-file", str(write_job(tmp_path, sign())), "--output", "o")
    assert result.returncode == 2
    assert "not registered" in result.stderr
    assert records(host_env["FAKE_BLENDER_RECORD"]) == []


def test_malformed_job_file_is_refused(tmp_path: Path, host_env: dict[str, str]) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text('{"envelope": {"job_id": "x"}}')
    assert host(host_env, "run", "--job-file", str(bad), "--output", "o").returncode == 2
    missing = host(host_env, "run", "--job-file", str(tmp_path / "nope.json"), "--output", "o")
    assert missing.returncode == 4


class _JobServer(BaseHTTPRequestHandler):
    jobs: dict[str, bytes] = {}
    requests: list[tuple[str, str | None]] = []

    def log_message(self, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        _JobServer.requests.append((self.path, self.headers.get("X-SpectraCut-Host")))
        job_id = self.path.rsplit("/", 1)[-1]
        body = _JobServer.jobs.get(job_id) if self.path.startswith("/api/hosts/jobs/") else None
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def job_server() -> Iterator[str]:
    _JobServer.jobs, _JobServer.requests = {}, []
    server = HTTPServer(("127.0.0.1", 0), _JobServer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_once_pulls_one_job_over_http_runs_it_and_exits(
    tmp_path: Path, host_env: dict[str, str], sign: SignJob, job_server: str
) -> None:
    _JobServer.jobs["job-7"] = sign(job_id="job-7").model_dump_json().encode()
    _JobServer.jobs["job-8"] = sign(job_id="job-8").model_dump_json().encode()
    env = {**host_env, "SPECTRACUT_CONTROL_URL": job_server}
    out = tmp_path / "out"
    result = host(env, "--once", "--job", "job-7", "--output", str(out))
    assert result.returncode == 0, result.stderr
    assert _JobServer.requests == [("/api/hosts/jobs/job-7", "legion")]
    assert json.loads((out / "result.json").read_text())["job_id"] == "job-7"
    assert len(records(host_env["FAKE_BLENDER_RECORD"])) == 1


def test_once_from_shared_storage(tmp_path: Path, host_env: dict[str, str], sign: SignJob) -> None:
    jobs = write_job(tmp_path, sign(job_id="job-3"), "job-3.json").parent
    out = tmp_path / "out"
    result = host(
        host_env, "--once", "--job", "job-3", "--jobs-dir", str(jobs), "--output", str(out)
    )
    assert result.returncode == 0, result.stderr
    assert (out / "result.json").exists()


def test_once_refuses_wrong_job_and_reports_pull_errors(
    tmp_path: Path, host_env: dict[str, str], sign: SignJob, job_server: str
) -> None:
    _JobServer.jobs["job-1"] = sign(job_id="job-2").model_dump_json().encode()
    env = {**host_env, "SPECTRACUT_CONTROL_URL": job_server}
    swapped = host(env, "--once", "--job", "job-1", "--output", str(tmp_path / "o"))
    assert swapped.returncode == 2 and "asked for job job-1" in swapped.stderr
    missing = host(env, "--once", "--job", "job-404", "--output", str(tmp_path / "o"))
    assert missing.returncode == 4 and "HTTP 404" in missing.stderr
    traversal = host(env, "--once", "--job", "../etc", "--output", str(tmp_path / "o"))
    assert traversal.returncode == 4 and "invalid job id" in traversal.stderr
    assert records(host_env["FAKE_BLENDER_RECORD"]) == []


def test_once_usage(host_env: dict[str, str]) -> None:
    assert host(host_env, "--once").returncode == 1
    assert host(host_env, "--once", "--job", "j", "--output", "o").returncode == 1
    assert host(host_env).returncode == 1


def test_profile_and_tags(host_env: dict[str, str]) -> None:
    profile = host(host_env, "profile")
    assert profile.returncode == 0, profile.stderr
    data = json.loads(profile.stdout)
    assert data["blender_version"] == "4.5.3"
    assert data["gpu_model"] == "NVIDIA GeForce RTX 4070 Ti"
    assert data["gpu_driver"] == "560.94"
    tags = host(host_env, "tags", "--capability", "eevee")
    assert tags.stdout.split() == ["4070ti", "vram-12gb", "eevee"]


def test_version_flag_prints_the_installed_package_version() -> None:
    import os

    import spectracut_host

    import spectracut

    res = host(dict(os.environ), "--version")
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == spectracut_host.__version__ == spectracut.__version__


def test_run_assets_dir_flag_overrides_env(
    tmp_path: Path, host_env: dict[str, str], sign: SignJob, make_recipe: Any
) -> None:
    flag_dir, env_dir = tmp_path / "flag-assets", tmp_path / "env-assets"
    for d in (flag_dir, env_dir):
        (d / "devices").mkdir(parents=True)
        (d / "devices" / "phone.glb").write_bytes(b"glTF")
    recipe = make_recipe([("object.import_gltf", {"path": "devices/phone.glb"})])
    job = write_job(tmp_path, sign(make_envelope_for(recipe)))
    env = {**host_env, "SPECTRACUT_ASSETS_DIR": str(env_dir), "FAKE_BPY_RECORD": ""}
    env["FAKE_BPY_RECORD"] = str(tmp_path / "bpy.jsonl")
    out = tmp_path / "out"
    args = ["run", "--job-file", str(job), "--output", str(out)]
    result = host(env, *args, "--assets-dir", str(flag_dir))
    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "bpy.jsonl").read_text()
    assert str((flag_dir / "devices" / "phone.glb").resolve()) in calls
    assert str(env_dir) not in calls
    assert json.loads((out / "result.json").read_text())["scene_graph_hash"]
