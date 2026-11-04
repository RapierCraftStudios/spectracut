# SPDX-License-Identifier: AGPL-3.0-or-later
"""Real-host check of ``hosts add`` bootstrap, run by the owner's M1 acceptance (#50).

Opt in with ``SPECTRACUT_E2E_HOSTS=legion,mrdubey-1`` (SSH aliases of unregistered render
hosts reachable from this machine). Without it the test is skipped; the same path runs
against a fake runner in ``test_bootstrap.py``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from spectracut.cli.app import app
from spectracut.cli.render import load_hosts

ALIASES = [a for a in os.environ.get("SPECTRACUT_E2E_HOSTS", "").split(",") if a]

pytestmark = pytest.mark.skipif(not ALIASES, reason="SPECTRACUT_E2E_HOSTS not set")


def test_hosts_add_bootstraps_real_hosts_and_setup_has_nothing_left_to_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("SPECTRACUT_HOME", str(home))
    cli = CliRunner()
    for alias in ALIASES:
        res = cli.invoke(app, ["hosts", "add", alias, "--role", "general"])
        assert res.exit_code == 0, res.output
    assert [s.name for s in load_hosts(home)] == ALIASES
    dry = cli.invoke(app, ["setup", "--dry-run"])
    assert dry.exit_code == 0, dry.output
    for alias in ALIASES:
        assert f"{alias}: install spectracut-host" not in dry.output
        assert f"{alias}: install blender" not in dry.output
