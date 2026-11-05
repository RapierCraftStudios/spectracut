# SPDX-License-Identifier: AGPL-3.0-or-later
"""spectracut-host wheels built on the control plane, not fetched from PyPI.

:func:`build_wheels` runs ``uv build`` on the source tree the control plane runs
from and stages the ``spectracut`` and ``spectracut-host`` wheels under a directory
named after their content digest. :func:`wheel_copy_steps` sends each wheel to a
render host over the same SSH stdin channel setup already uses (base64 text, no
sudo or admin), and the manifest installs spectracut-host from those files.

The setup state records ``<version>+<digest>``, so a code change (a new digest)
makes the next ``spectracut setup`` reinstall spectracut-host on every host.
Hatchling wheels are reproducible: the same code gives the same digest.
"""

from __future__ import annotations

import base64
import hashlib
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import spectracut
from spectracut.setup.manifest import HostFacts, Os, Shell, Step
from spectracut.setup.transport import STEP_INPUT

CONTROL_PLANE_PIN = "control-plane"  # manifest pin of a component built on the control plane
REPO_ENV = "SPECTRACUT_REPO"
LINUX_WHEELS = "$HOME/.cache/spectracut/wheels"
WINDOWS_WHEELS = "spectracut/wheels"  # under $env:USERPROFILE
BUILD_TIMEOUT = 900.0


@dataclass(frozen=True)
class LocalWheels:
    """The two wheels of one build: their shared version and content digest."""

    version: str
    digest: str
    spectracut: Path
    host: Path

    @property
    def files(self) -> tuple[Path, ...]:
        return (self.spectracut, self.host)

    @property
    def pin(self) -> str:
        """What the setup state records for spectracut-host."""
        return f"{self.version}+{self.digest}"

    @classmethod
    def from_files(cls, version: str, spectracut: Path, host: Path) -> LocalWheels:
        lines = sorted(
            f"{p.name}:{hashlib.sha256(p.read_bytes()).hexdigest()}" for p in (spectracut, host)
        )
        digest = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()[:12]
        return cls(version, digest, spectracut, host)


def _error(message: str) -> Exception:
    from spectracut.setup.plan import SetupError  # plan imports this module

    return SetupError(message)


def source_root() -> Path:
    """The source tree to build: ``$SPECTRACUT_REPO``, else the repo this package runs from."""
    env = os.environ.get(REPO_ENV)
    root = Path(env) if env else Path(__file__).resolve().parents[3]
    if not ((root / "pyproject.toml").is_file() and (root / "host" / "pyproject.toml").is_file()):
        raise _error(
            f"spectracut-host wheels are built from the source tree, and {root} is not one; "
            f"run from a checkout or set {REPO_ENV}"
        )
    return root


def wheel_version(path: Path) -> str:
    """The version field of a wheel file name (PEP 427: name-version-...)."""
    return path.name.split("-")[1]


def build_wheels(out_root: Path) -> LocalWheels:
    """Build both wheels with ``uv build`` and stage them under ``out_root/<digest>/``."""
    root = source_root()
    uv = shutil.which("uv")
    if uv is None:
        raise _error("uv is needed on the control plane to build the spectracut-host wheels")
    out_root.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".build-", dir=out_root))
    try:
        proc = subprocess.run(
            [uv, "build", "-q", "--all-packages", "--wheel", "--out-dir", str(tmp)],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=BUILD_TIMEOUT,
            check=False,
        )
        if proc.returncode != 0:
            lines = [ln for ln in (proc.stderr or proc.stdout).splitlines() if ln.strip()]
            raise _error(f"uv build of the spectracut wheels failed: {(lines or [''])[-1]}")
        core = sorted(tmp.glob("spectracut-*.whl"))
        host = sorted(tmp.glob("spectracut_host-*.whl"))
        if len(core) != 1 or len(host) != 1:
            names = sorted(p.name for p in tmp.iterdir())
            raise _error(f"uv build gave unexpected wheels: {names}")
        version = spectracut.__version__
        for wheel in (core[0], host[0]):
            if wheel_version(wheel) != version:
                raise _error(
                    f"{wheel.name} does not match the control plane's version {version}; "
                    "keep pyproject.toml and host/pyproject.toml on the same version"
                )
        built = LocalWheels.from_files(version, core[0], host[0])
        dest = out_root / built.digest
        if not dest.is_dir():
            staging = Path(tempfile.mkdtemp(prefix=".stage-", dir=out_root))
            for wheel in built.files:
                shutil.move(wheel, staging / wheel.name)
            staging.rename(dest)
        return LocalWheels(
            version, built.digest, dest / built.spectracut.name, dest / built.host.name
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def remote_wheel_dir(os_name: Os, digest: str) -> str:
    """Where a host keeps the wheels of ``digest``: a user-writable directory."""
    if os_name == "windows":
        return f"$env:USERPROFILE/{WINDOWS_WHEELS}/{digest}"
    return f"{LINUX_WHEELS}/{digest}"


def install_fields(os_name: Os, wheels: LocalWheels) -> dict[str, str]:
    """Placeholders the spectracut-host install snippet is formatted with."""
    d = remote_wheel_dir(os_name, wheels.digest)
    return {
        "wheel_dir": d,
        "spectracut_wheel": f"{d}/{wheels.spectracut.name}",
        "host_wheel": f"{d}/{wheels.host.name}",
        "public_version": wheels.version,
    }


def wheel_copy_steps(facts: HostFacts, wheels: LocalWheels) -> list[Step]:
    """Copy each wheel to the host; its bytes travel base64-encoded on stdin only.

    On Windows the runner frames stdin (``$StepInput``, no EOF needed); the wheel is
    written to ``.part``, its sha256 checked against the local wheel, then renamed.
    """
    steps: list[Step] = []
    for wheel in wheels.files:
        shell: Shell
        raw = wheel.read_bytes()
        data = base64.b64encode(raw).decode("ascii")
        if facts.os == "windows":
            sha = hashlib.sha256(raw).hexdigest().upper()
            cmd = (
                f"$d = Join-Path $env:USERPROFILE '{WINDOWS_WHEELS}/{wheels.digest}'; "
                "New-Item -ItemType Directory -Force $d | Out-Null; "
                f"$f = Join-Path $d '{wheel.name}'; $t = $f + '.part'; "
                f"[IO.File]::WriteAllBytes($t, [Convert]::FromBase64String({STEP_INPUT}.Trim())); "
                "$h = (Get-FileHash -Algorithm SHA256 -LiteralPath $t).Hash; "
                f"if ($h -ne '{sha}') {{ Remove-Item -Force -LiteralPath $t; "
                f"[Console]::Error.WriteLine('{wheel.name}: sha256 mismatch (' + $h + ')'); "
                "exit 1 }; "
                "Move-Item -Force -LiteralPath $t -Destination $f"
            )
            shell = "powershell"
        else:
            cmd = (
                f'd="{remote_wheel_dir("linux", wheels.digest)}"; mkdir -p "$d" '
                f'&& base64 -d > "$d/{wheel.name}.part" && mv "$d/{wheel.name}.part" '
                f'"$d/{wheel.name}"'
            )
            shell = "sh"
        steps.append(
            Step(
                facts.name,
                f"wheel:{wheel.name}:{facts.name}",
                f"copy {wheel.name}",
                cmd,
                shell,
                stdin=data,
                kind="copy",
            )
        )
    return steps
