# SPDX-License-Identifier: AGPL-3.0-or-later
"""spectracut-host wheels built on the control plane, not fetched from PyPI.

:func:`build_wheels` runs ``uv build`` on the source tree the control plane runs
from and stages the ``spectracut`` and ``spectracut-host`` wheels under a directory
named after their content digest. :func:`wheel_copy_steps` sends each wheel to a
render host over the same SSH stdin channel setup already uses (base64 text in
chunks, no sudo or admin), and the manifest installs spectracut-host from those files.

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
CHUNK_BYTES = 256 * 1024  # raw bytes per copy step; one stalled step costs one chunk


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


def wheel_chunks(raw: bytes) -> list[bytes]:
    """``raw`` split into :data:`CHUNK_BYTES` pieces (one empty piece for an empty file)."""
    return [raw[i : i + CHUNK_BYTES] for i in range(0, len(raw), CHUNK_BYTES)] or [b""]


def wheel_copy_steps(facts: HostFacts, wheels: LocalWheels) -> list[Step]:
    """Copy each wheel to the host in chunks; its bytes travel base64-encoded on stdin only.

    Each wheel is ``wheel:<file>:<host>:<n>`` chunk steps, then one ``wheel:<file>:<host>``
    step that checks the whole file's sha256 against the local wheel and renames the
    ``.part`` file into place. A chunk is written at its own offset, so a retry after a
    stall is idempotent, and a stall costs one chunk instead of the whole wheel. On
    Windows the runner frames stdin (``$StepInput``, no EOF needed). The copies only
    stage files under a digest-named directory, so setup runs them before it stops the
    worker.
    """
    steps: list[Step] = []
    windows = facts.os == "windows"
    shell: Shell = "powershell" if windows else "sh"
    for wheel in wheels.files:
        raw = wheel.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        base = f"wheel:{wheel.name}:{facts.name}"
        for n, chunk in enumerate(wheel_chunks(raw)):
            if windows:
                mode = "Create" if n == 0 else "OpenOrCreate"
                cmd = (
                    f"$d = Join-Path $env:USERPROFILE '{WINDOWS_WHEELS}/{wheels.digest}'; "
                    "New-Item -ItemType Directory -Force $d | Out-Null; "
                    f"$t = Join-Path $d '{wheel.name}.part'; "
                    f"$b = [Convert]::FromBase64String({STEP_INPUT}.Trim()); "
                    f"$s = [IO.File]::Open($t, '{mode}', 'Write', 'None'); "
                    f"try {{ $s.Seek({n * CHUNK_BYTES}, 'Begin') | Out-Null; "
                    "$s.Write($b, 0, $b.Length) } finally { $s.Dispose() }"
                )
            else:
                keep = "" if n == 0 else " conv=notrunc"
                cmd = (
                    f'd="{remote_wheel_dir("linux", wheels.digest)}"; mkdir -p "$d" '
                    f'&& base64 -d | dd of="$d/{wheel.name}.part" bs={CHUNK_BYTES} '
                    f"seek={n}{keep} iflag=fullblock status=none"
                )
            steps.append(
                Step(
                    facts.name,
                    f"{base}:{n}",
                    f"copy {wheel.name} part {n + 1}",
                    cmd,
                    shell,
                    stdin=base64.b64encode(chunk).decode("ascii"),
                    kind="copy",
                )
            )
        if windows:
            cmd = (
                f"$d = Join-Path $env:USERPROFILE '{WINDOWS_WHEELS}/{wheels.digest}'; "
                f"$f = Join-Path $d '{wheel.name}'; $t = $f + '.part'; "
                "$h = (Get-FileHash -Algorithm SHA256 -LiteralPath $t).Hash; "
                f"if ($h -ne '{sha.upper()}') {{ Remove-Item -Force -LiteralPath $t; "
                f"[Console]::Error.WriteLine('{wheel.name}: sha256 mismatch (' + $h + ')'); "
                "exit 1 }; "
                "Move-Item -Force -LiteralPath $t -Destination $f"
            )
        else:
            part = f'"$d/{wheel.name}.part"'
            cmd = (
                f'd="{remote_wheel_dir("linux", wheels.digest)}"; '
                f'h=$(sha256sum < {part} | cut -d" " -f1); '
                f'if [ "$h" != "{sha}" ]; then rm -f {part}; '
                f'echo "{wheel.name}: sha256 mismatch ($h)" >&2; exit 1; fi; '
                f'mv {part} "$d/{wheel.name}"'
            )
        steps.append(Step(facts.name, base, f"verify {wheel.name}", cmd, shell, kind="copy"))
    return steps
