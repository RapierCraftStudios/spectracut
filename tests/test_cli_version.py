# SPDX-License-Identifier: AGPL-3.0-or-later
"""``spectracut --version`` prints the installed package version."""

from __future__ import annotations

from importlib.metadata import version

from typer.testing import CliRunner

import spectracut
from spectracut.cli.app import app

runner = CliRunner()


def test_version_flag_prints_installed_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == f"spectracut {version('spectracut')}"


def test_dunder_version_matches_metadata() -> None:
    assert spectracut.__version__ == version("spectracut")


def test_subcommands_still_registered() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "auth" in result.output and "bench" in result.output
