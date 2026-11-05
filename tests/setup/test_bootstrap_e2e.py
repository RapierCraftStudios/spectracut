# SPDX-License-Identifier: AGPL-3.0-or-later
"""Real-host check of ``hosts add`` bootstrap, run by the owner's M1 acceptance (#50).

Opt in with ``SPECTRACUT_E2E_HOSTS=legion,mrdubey-1`` (SSH aliases of unregistered render
hosts reachable from this machine); ``SPECTRACUT_E2E_ROLE`` and ``SPECTRACUT_E2E_GPU``
pick the role and ``--gpu`` (a non-admin Windows host needs the elevated command that
``spectracut setup`` prints run once first). Without it the test is skipped; the same path runs
against a fake runner in ``test_bootstrap.py``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

import spectracut
from spectracut.cli.app import app
from spectracut.cli.render import load_hosts

HOST_VERSION = {
    "linux": '"$HOME/.local/bin/spectracut-host" --version',
    "windows": "& (Join-Path $env:USERPROFILE '.local/bin/spectracut-host.exe') --version",
}

ALIASES = [a for a in os.environ.get("SPECTRACUT_E2E_HOSTS", "").split(",") if a]
# e.g. SPECTRACUT_E2E_ROLE=gpu-only SPECTRACUT_E2E_GPU="RTX 4070 Ti:12" for mrdubey-1 (#142)
ROLE = os.environ.get("SPECTRACUT_E2E_ROLE", "general")
GPU = ["--gpu", os.environ["SPECTRACUT_E2E_GPU"]] if os.environ.get("SPECTRACUT_E2E_GPU") else []

pytestmark = pytest.mark.skipif(not ALIASES, reason="SPECTRACUT_E2E_HOSTS not set")


def test_hosts_add_bootstraps_real_hosts_and_setup_has_nothing_left_to_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("SPECTRACUT_HOME", str(home))
    cli = CliRunner()
    for alias in ALIASES:
        res = cli.invoke(app, ["hosts", "add", alias, "--role", ROLE, *GPU])
        assert res.exit_code == 0, res.output
    assert [s.name for s in load_hosts(home)] == ALIASES
    from spectracut.setup.plan import CommandRunner

    runner = CommandRunner(timeout=120)
    for alias in ALIASES:
        os_name = runner.probe(alias).os
        shell = "powershell" if os_name == "windows" else "sh"
        proc = runner._exec(alias, HOST_VERSION[os_name], shell, None)
        assert proc.returncode == 0, proc.stderr
        # installed from the control plane's wheels: same version, no PyPI release needed
        assert proc.stdout.strip() == spectracut.__version__, alias
    dry = cli.invoke(app, ["setup", "--dry-run"])
    assert dry.exit_code == 0, dry.output
    for alias in ALIASES:
        assert f"{alias}: install spectracut-host" not in dry.output
        assert f"{alias}: install blender" not in dry.output
