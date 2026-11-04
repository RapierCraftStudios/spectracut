# SPDX-License-Identifier: AGPL-3.0-or-later
"""The execution guard refuses unregistered, unsigned or tampered execution."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from spectracut_host.guard import (
    ExecutionGuard,
    GuardRefused,
    HostRegistration,
    load_registration,
    runtime_tool_names,
    scrubbed_env,
)

from spectracut.queue.profile import RenderProfile
from spectracut.queue.signing import (
    JobEnvelope,
    SignedJob,
    generate_keypair,
    load_private_key,
    sign_job,
)
from spectracut.spec.recipe import Recipe

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "host" / "bpy_runtime"
PROFILE = RenderProfile(blender_version="4.5.3", gpu_model="NVIDIA GeForce RTX 4070 Ti")

SignJob = Callable[..., SignedJob]
MakeEnvelope = Callable[..., JobEnvelope]


@pytest.fixture
def guard(keypair: tuple[str, str]) -> ExecutionGuard:
    return ExecutionGuard(HostRegistration("legion", keypair[1]), PROFILE, runtime=RUNTIME)


def test_signed_job_is_accepted(guard: ExecutionGuard, sign: SignJob) -> None:
    envelope, frames = guard.check(sign())
    assert envelope.job_id == "job-1"
    assert frames == (1, 3)


def test_unregistered_host_refuses(keypair: tuple[str, str], sign: SignJob) -> None:
    guard = ExecutionGuard(None, PROFILE, runtime=RUNTIME)
    with pytest.raises(GuardRefused, match="not registered"):
        guard.check(sign())


def test_unsigned_job_refused(guard: ExecutionGuard, make_envelope: MakeEnvelope) -> None:
    with pytest.raises(GuardRefused, match="unsigned"):
        guard.check(SignedJob(envelope=make_envelope()))


def test_job_signed_by_another_key_refused(
    guard: ExecutionGuard, make_envelope: MakeEnvelope
) -> None:
    other, _ = generate_keypair()
    with pytest.raises(GuardRefused, match="invalid"):
        guard.check(sign_job(make_envelope(), load_private_key(other)))


def test_tampered_job_refused(
    guard: ExecutionGuard, sign: SignJob, make_recipe: Callable[..., Recipe]
) -> None:
    signed = sign()
    tampered_recipe = make_recipe([("scene.clear", {})])
    forged = signed.envelope.model_copy(
        update={"recipe": tampered_recipe, "recipe_hash": tampered_recipe.content_hash}
    )
    with pytest.raises(GuardRefused, match="invalid"):
        guard.check(SignedJob(envelope=forged, signature=signed.signature))
    widened = signed.envelope.model_copy(update={"frame_end": 999})
    with pytest.raises(GuardRefused, match="invalid"):
        guard.check(SignedJob(envelope=widened, signature=signed.signature))


def test_garbage_signature_refused(guard: ExecutionGuard, make_envelope: MakeEnvelope) -> None:
    with pytest.raises(GuardRefused, match="invalid"):
        guard.check(SignedJob(envelope=make_envelope(), signature="not base64!"))


def test_expired_job_refused(guard: ExecutionGuard, sign: SignJob) -> None:
    signed = sign(issued_at=datetime.now(UTC) - timedelta(hours=2))
    with pytest.raises(GuardRefused, match="expired"):
        guard.check(signed)


def test_recipe_hash_mismatch_refused(guard: ExecutionGuard, sign: SignJob) -> None:
    with pytest.raises(GuardRefused, match="recipe_hash"):
        guard.check(sign(recipe_hash="sha256:" + "0" * 64))


def test_blender_version_mismatch_refused(keypair: tuple[str, str], sign: SignJob) -> None:
    for version in ("4.2.1", "unknown"):
        profile = PROFILE.model_copy(update={"blender_version": version})
        guard = ExecutionGuard(HostRegistration("legion", keypair[1]), profile, runtime=RUNTIME)
        with pytest.raises(GuardRefused, match="Blender"):
            guard.check(sign())


def test_unknown_tool_refused(
    guard: ExecutionGuard, sign: SignJob, make_envelope: MakeEnvelope, make_recipe: Any
) -> None:
    recipe = make_recipe([("python.exec", {"code": "import os"})])
    with pytest.raises(GuardRefused, match="python.exec"):
        guard.check(sign(make_envelope(recipe)))


def test_frames_must_lie_in_signed_range(guard: ExecutionGuard, sign: SignJob) -> None:
    signed = sign(frames=(1, 10))
    assert guard.check(signed, "3-5")[1] == (3, 5)
    assert guard.check(signed, "7")[1] == (7, 7)
    for bad in ("0-4", "8-11", "5-3", "x"):
        with pytest.raises(GuardRefused):
            guard.check(signed, bad)


def test_bad_registration_key_refused(sign: SignJob) -> None:
    guard = ExecutionGuard(HostRegistration("legion", "nope"), PROFILE, runtime=RUNTIME)
    with pytest.raises(GuardRefused, match="registration is invalid"):
        guard.check(sign())


def test_load_registration(tmp_path: Path, registration: Path) -> None:
    reg = load_registration({"SPECTRACUT_HOST_CONFIG": str(registration)})
    assert reg is not None and reg.host_id == "legion"
    assert load_registration({"SPECTRACUT_HOST_CONFIG": str(tmp_path / "missing.json")}) is None
    broken = tmp_path / "broken.json"
    broken.write_text('{"host_id": ""}')
    assert load_registration({"SPECTRACUT_HOST_CONFIG": str(broken)}) is None


def test_launch_runs_blender_headless_with_runtime_and_scrubbed_env(
    guard: ExecutionGuard, sign: SignJob, tmp_path: Path
) -> None:
    calls: list[dict[str, Any]] = []

    def launcher(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        manifest = Path(cmd[-1])
        calls.append({"cmd": cmd, "env": kwargs["env"], "manifest": manifest.read_text()})
        assert oct(manifest.stat().st_mode & 0o777) == oct(0o600)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    envelope, frames = guard.check(sign())
    env = {
        "PATH": "/usr/bin",
        "SPECTRACUT_JOB_SIGNING_KEY": "private",
        "SPECTRACUT_HOST_KEY": "host-secret",
        "AWS_SECRET_ACCESS_KEY": "aws",
        "ANTHROPIC_API_KEY": "sk",
    }
    guard.launch(
        envelope, frames, tmp_path / "out", blender_bin="blender", env=env, launcher=launcher
    )
    (call,) = calls
    cmd = call["cmd"]
    assert cmd[:5] == ["blender", "-b", "--factory-startup", "--python-exit-code", "1"]
    assert cmd[5:7] == ["--python", str(RUNTIME / "runner.py")]
    assert cmd[7] == "--"
    assert "--python-expr" not in cmd
    assert set(call["env"]) == {"PATH", "SPECTRACUT_EXECUTION_TOKEN"}
    assert call["env"]["SPECTRACUT_EXECUTION_TOKEN"] in call["manifest"]
    assert "private" not in call["manifest"] and "host-secret" not in call["manifest"]
    assert not list((tmp_path / "out").glob(".manifest-*"))


def test_scrubbed_env_drops_secrets() -> None:
    env = {"HOME": "/h", "GITHUB_TOKEN": "t", "DB_PASSWORD": "p", "SPECTRACUT_X": "1"}
    assert scrubbed_env(env) == {"HOME": "/h"}


def test_guard_tool_list_matches_runtime_registry() -> None:
    # Read the runtime registry in a separate interpreter that never imports spectracut.
    code = (
        "import sys; sys.path.insert(0, sys.argv[1])\n"
        "from bpy_runtime.tools import TOOLS\n"
        "print('\\n'.join(sorted(TOOLS)))\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, str(RUNTIME.parent)],
        capture_output=True,
        text=True,
        check=True,
        env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"},
    )
    assert set(result.stdout.split()) == runtime_tool_names(RUNTIME / "tools.py")
