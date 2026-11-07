# SPDX-License-Identifier: AGPL-3.0-or-later
"""Jobs route to the right card by worker tag; Flamenco submission payloads and client."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest
from spectracut_host.profile import worker_tags

from spectracut.queue.flamenco import (
    JOB_TYPE,
    FlamencoClient,
    FlamencoError,
    NoMatchingWorkerTag,
    WorkerTag,
    build_shot_job,
    job_type_script,
    select_worker_tag,
)
from spectracut.queue.profile import RenderProfile
from spectracut.queue.signing import JobEnvelope

TAGS = [
    WorkerTag(id="uuid-4070", name="4070ti"),
    WorkerTag(id="uuid-3090", name="3090"),
    WorkerTag(id="uuid-eevee", name="eevee"),
    WorkerTag(id="uuid-vram", name="vram-24gb"),
]


@pytest.mark.parametrize(
    ("requirement", "tag_id"),
    [
        ("4070ti", "uuid-4070"),
        ("4070 Ti", "uuid-4070"),
        ("3090", "uuid-3090"),
        ("eevee", "uuid-eevee"),
        ("EEVEE", "uuid-eevee"),
        ("vram-24gb", "uuid-vram"),
    ],
)
def test_routes_to_tag(requirement: str, tag_id: str) -> None:
    assert select_worker_tag(requirement, TAGS).id == tag_id


@pytest.mark.parametrize("requirement", ["4090", "", "cycles"])
def test_unknown_tag_is_an_error_not_untagged(requirement: str) -> None:
    with pytest.raises(NoMatchingWorkerTag):
        select_worker_tag(requirement, TAGS)


def test_host_tags_route_jobs_to_that_card() -> None:
    # Tags a host derives from its profile are the names jobs route by.
    legion = RenderProfile(
        blender_version="4.5.3", gpu_model="NVIDIA GeForce RTX 4070 Ti", vram_gb=12
    )
    other = RenderProfile(blender_version="4.5.3", gpu_model="NVIDIA GeForce RTX 3090", vram_gb=24)
    manager_tags = [
        WorkerTag(id=f"id-{name}", name=name)
        for name in sorted(set(worker_tags(legion)) | set(worker_tags(other)))
    ]
    assert select_worker_tag("4070ti", manager_tags).name in worker_tags(legion)
    assert select_worker_tag("4070ti", manager_tags).name not in worker_tags(other)
    assert select_worker_tag("3090", manager_tags).name in worker_tags(other)


def test_build_shot_job_carries_tag_and_settings(make_envelope: Callable[..., JobEnvelope]) -> None:
    envelope = make_envelope(frames=(1, 48))
    job = build_shot_job(
        "/shared/jobs/job-1.json", envelope, TAGS[0], output_dir="/shared/out", chunk_size=12
    )
    assert job["type"] == JOB_TYPE == "spectracut-shot"
    assert job["worker_tag"] == "uuid-4070"
    assert job["settings"] == {
        "job_file": "/shared/jobs/job-1.json",
        "frames": "1-48",
        "chunk_size": 12,
        "output_dir": "/shared/out",
    }
    assert job["metadata"]["spectracut.recipe_hash"] == envelope.recipe_hash


def test_build_shot_job_enforces_required_tag(make_envelope: Callable[..., JobEnvelope]) -> None:
    envelope = make_envelope(required_tag="eevee")
    build_shot_job("/j.json", envelope, TAGS[2], output_dir="/o")
    with pytest.raises(ValueError, match="requires worker tag"):
        build_shot_job("/j.json", envelope, TAGS[0], output_dir="/o")
    with pytest.raises(ValueError):
        build_shot_job("/j.json", make_envelope(), TAGS[0], output_dir="/o", chunk_size=0)


def test_job_type_script_ships_with_package() -> None:
    script = job_type_script()
    assert script.is_file()
    text = script.read_text()
    assert "function compileJob" in text
    assert "--python-expr" not in text


class _Manager(BaseHTTPRequestHandler):
    posted: list[Any] = []

    def log_message(self, *args: Any) -> None:
        pass

    def _send(self, code: int, body: Any) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        if self.path == "/api/v3/worker-mgt/tags":
            self._send(200, {"tags": [{"id": "uuid-4070", "name": "4070ti"}]})
        elif self.path == "/api/v3/jobs/j-1":
            self._send(200, {"id": "j-1", "status": "completed"})
        elif self.path == "/api/v3/jobs/j-1/tasks":
            self._send(200, {"tasks": [{"id": "t-1", "name": "render", "status": "soft-failed"}]})
        elif self.path == "/api/v3/tasks/t-1/logtail":
            raw = b"spectracut runner: refused: step 18 [args.invalid]\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        else:
            self._send(404, {"message": "no"})

    def do_POST(self) -> None:
        length = int(self.headers["Content-Length"])
        _Manager.posted.append(json.loads(self.rfile.read(length)))
        self._send(200, {"id": "j-1", "status": "queued"})


@pytest.fixture
def manager() -> Iterator[str]:
    _Manager.posted = []
    server = HTTPServer(("127.0.0.1", 0), _Manager)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_client_lists_tags_and_submits(
    manager: str, make_envelope: Callable[..., JobEnvelope]
) -> None:
    client = FlamencoClient(manager)
    tags = client.list_tags()
    assert tags == [WorkerTag(id="uuid-4070", name="4070ti")]
    job = build_shot_job(
        "/j.json", make_envelope(), select_worker_tag("4070ti", tags), output_dir="/o"
    )
    assert client.submit(job)["id"] == "j-1"
    assert _Manager.posted == [job]
    assert client.job_status("j-1") == "completed"


def test_client_lists_tasks_reads_the_log_tail_and_cancels(manager: str) -> None:
    client = FlamencoClient(manager)
    assert client.job_tasks("j-1") == [{"id": "t-1", "name": "render", "status": "soft-failed"}]
    assert client.task_log_tail("t-1") == "spectracut runner: refused: step 18 [args.invalid]\n"
    client.cancel_job("j-1", "soft-failed task t-1")
    assert _Manager.posted == [{"status": "cancel-requested", "reason": "soft-failed task t-1"}]
    with pytest.raises(FlamencoError, match="HTTP 404"):
        client.task_log_tail("missing")


def test_client_errors(manager: str) -> None:
    with pytest.raises(FlamencoError, match="HTTP 404"):
        FlamencoClient(manager).job_status("missing")
    with pytest.raises(FlamencoError):
        FlamencoClient("http://127.0.0.1:9", timeout=1).list_tags()


def test_build_shot_job_sets_optional_assets_dir(
    make_envelope: Callable[..., JobEnvelope],
) -> None:
    envelope = make_envelope()
    job = build_shot_job("/j.json", envelope, TAGS[0], output_dir="/o", assets_dir="/shared/assets")
    assert job["settings"]["assets_dir"] == "/shared/assets"
    assert (
        "assets_dir"
        not in build_shot_job("/j.json", envelope, TAGS[0], output_dir="/o")["settings"]
    )


class _Rejecting(BaseHTTPRequestHandler):
    body = b""

    def log_message(self, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        if self.path == "/api/v3/jobs/types":
            raw = json.dumps({"job_types": [{"name": "spectracut-shot", "label": "Shot"}]})
            self.send_response(200)
            data = raw.encode()
        else:
            self.send_response(400)
            data = _Rejecting.body
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_POST = do_GET


@pytest.fixture
def rejecting() -> Iterator[str]:
    server = HTTPServer(("127.0.0.1", 0), _Rejecting)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_client_error_includes_the_response_body(rejecting: str) -> None:
    client = FlamencoClient(rejecting)
    _Rejecting.body = b'{"message": "job type unknown", "type": "spectracut-shot"}'
    with pytest.raises(FlamencoError) as err:
        client.submit({"type": "spectracut-shot"})
    assert str(err.value).startswith("POST /api/v3/jobs failed: HTTP 400: ")
    assert "job type unknown" in str(err.value)

    _Rejecting.body = b"x" * 5000
    with pytest.raises(FlamencoError) as err:
        client.submit({})
    assert len(str(err.value)) < 600 and str(err.value).endswith("x…")

    _Rejecting.body = b""
    with pytest.raises(FlamencoError, match=r"failed: HTTP 400$"):
        client.submit({})


def test_client_lists_job_types(rejecting: str) -> None:
    assert FlamencoClient(rejecting).job_types() == ["spectracut-shot"]
