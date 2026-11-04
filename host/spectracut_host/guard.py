# SPDX-License-Identifier: AGPL-3.0-or-later
"""Execution guard: the only way a host starts Blender.

The guard refuses a job unless this host is registered, the job's Ed25519
signature verifies against the control plane's public key, it has not expired,
its recipe hash matches the recipe, the recipe targets this host's Blender
major.minor, the requested frames lie inside the signed range, and every step
names a tool in the bpy runtime's registry. Only then does it launch
``blender -b --factory-startup`` with the bpy runtime runner and a per-run
execution token, with secrets removed from Blender's environment.
"""

from __future__ import annotations

import ast
import json
import os
import re
import secrets
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from spectracut.queue.profile import RenderProfile
from spectracut.queue.signing import (
    JobEnvelope,
    JobSignatureError,
    SignedJob,
    load_public_key,
    verify_job,
)

HOST_CONFIG_ENV = "SPECTRACUT_HOST_CONFIG"
RUNTIME_DIR_ENV = "SPECTRACUT_BPY_RUNTIME"
TOKEN_ENV = "SPECTRACUT_EXECUTION_TOKEN"
_SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL", re.IGNORECASE)


class GuardRefused(Exception):
    """The guard refused to execute a job; the message says why."""


@dataclass(frozen=True)
class HostRegistration:
    """This host's identity and the control plane's job-signing public key."""

    host_id: str
    control_plane_public_key: str


def default_config_path() -> Path:
    return Path.home() / ".config" / "spectracut" / "host.json"


def load_registration(env: Mapping[str, str] | None = None) -> HostRegistration | None:
    """Load the host registration, or ``None`` if this host is not registered."""
    env = os.environ if env is None else env
    path = Path(env[HOST_CONFIG_ENV]) if env.get(HOST_CONFIG_ENV) else default_config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        host_id = data["host_id"]
        key = data["control_plane_public_key"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(host_id, str) or not host_id or not isinstance(key, str) or not key:
        return None
    return HostRegistration(host_id=host_id, control_plane_public_key=key)


def runtime_dir(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    if env.get(RUNTIME_DIR_ENV):
        return Path(env[RUNTIME_DIR_ENV])
    return Path(__file__).resolve().parent.parent / "bpy_runtime"


def runtime_tool_names(tools_py: Path) -> frozenset[str]:
    """Read the literal ``TOOL_NAMES`` from ``bpy_runtime/tools.py`` without importing it."""
    tree = ast.parse(tools_py.read_text(encoding="utf-8"))
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "TOOL_NAMES" for t in node.targets)
            and isinstance(node.value, ast.Call)
            and node.value.args
        ):
            names = ast.literal_eval(node.value.args[0])
            return frozenset(str(n) for n in names)
    raise ValueError(f"no TOOL_NAMES literal in {tools_py}")


def parse_frames(frames: str) -> tuple[int, int]:
    match = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+))?\s*", frames)
    if not match:
        raise GuardRefused(f"frames must look like 1-120, got {frames!r}")
    start = int(match.group(1))
    end = int(match.group(2)) if match.group(2) is not None else start
    if end < start:
        raise GuardRefused(f"frames end before start: {frames!r}")
    return start, end


def _major_minor(version: str) -> tuple[int, int] | None:
    match = re.match(r"(\d+)\.(\d+)", version)
    return (int(match.group(1)), int(match.group(2))) if match else None


def scrubbed_env(env: Mapping[str, str]) -> dict[str, str]:
    """Copy ``env`` without SpectraCut settings or anything that looks like a secret."""
    return {
        k: v
        for k, v in env.items()
        if not k.startswith("SPECTRACUT_") and not _SECRET_NAME.search(k)
    }


Launcher = Callable[..., subprocess.CompletedProcess[str]]


class ExecutionGuard:
    """Checks signed jobs and launches Blender with the bpy runtime."""

    def __init__(
        self,
        registration: HostRegistration | None,
        profile: RenderProfile,
        *,
        runtime: Path | None = None,
    ) -> None:
        self.registration = registration
        self.profile = profile
        self.runtime = runtime or runtime_dir()
        self.tool_names = runtime_tool_names(self.runtime / "tools.py")

    def check(
        self,
        signed: SignedJob,
        frames: str | None = None,
        now: datetime | None = None,
    ) -> tuple[JobEnvelope, tuple[int, int]]:
        """Return the verified envelope and frame range, or raise :class:`GuardRefused`."""
        if self.registration is None:
            raise GuardRefused(
                "host is not registered: no host config "
                f"(set {HOST_CONFIG_ENV} or create {default_config_path()})"
            )
        try:
            key = load_public_key(self.registration.control_plane_public_key)
        except ValueError as exc:
            raise GuardRefused(f"host registration is invalid: {exc}") from None
        try:
            envelope = verify_job(signed, key)
        except JobSignatureError as exc:
            raise GuardRefused(str(exc)) from None
        if envelope.is_expired(now):
            raise GuardRefused(f"job {envelope.job_id} expired at {envelope.expires_at}")
        if envelope.recipe.content_hash != envelope.recipe_hash:
            raise GuardRefused("recipe_hash does not match the recipe content")
        want = _major_minor(envelope.recipe.blender_version)
        have = _major_minor(self.profile.blender_version)
        if have is None or want != have:
            raise GuardRefused(
                f"recipe needs Blender {envelope.recipe.blender_version}, "
                f"host has {self.profile.blender_version}"
            )
        unknown = sorted({s.tool for s in envelope.recipe.steps} - self.tool_names)
        if unknown:
            raise GuardRefused(f"recipe uses tools the bpy runtime does not provide: {unknown}")
        start, end = (
            parse_frames(frames)
            if frames is not None
            else (envelope.frame_start, envelope.frame_end)
        )
        if start < envelope.frame_start or end > envelope.frame_end:
            raise GuardRefused(
                f"frames {start}-{end} are outside the signed range "
                f"{envelope.frame_start}-{envelope.frame_end}"
            )
        return envelope, (start, end)

    def launch(
        self,
        envelope: JobEnvelope,
        frames: tuple[int, int],
        output_dir: Path,
        *,
        blender_bin: str = "blender",
        env: Mapping[str, str] | None = None,
        launcher: Launcher = subprocess.run,
    ) -> subprocess.CompletedProcess[str]:
        """Write the run manifest and run Blender headless with the bpy runtime."""
        output_dir.mkdir(parents=True, exist_ok=True)
        token = secrets.token_urlsafe(32)
        manifest = {
            "token": token,
            "job_id": envelope.job_id,
            "blender_version": envelope.recipe.blender_version,
            "steps": [s.model_dump(mode="json") for s in envelope.recipe.steps],
            "frame_start": frames[0],
            "frame_end": frames[1],
            "output_dir": str(output_dir.resolve()),
        }
        manifest_path = output_dir / f".manifest-{frames[0]}-{frames[1]}.json"
        fd = os.open(manifest_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh)
        child_env = scrubbed_env(os.environ if env is None else env)
        child_env[TOKEN_ENV] = token
        cmd = [
            blender_bin,
            "-b",
            "--factory-startup",
            "--python-exit-code",
            "1",
            "--python",
            str(self.runtime / "runner.py"),
            "--",
            str(manifest_path.resolve()),
        ]
        try:
            return launcher(cmd, env=child_env, capture_output=True, text=True, check=False)
        finally:
            manifest_path.unlink(missing_ok=True)
