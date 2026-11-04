# SPDX-License-Identifier: AGPL-3.0-or-later
"""``spectracut auth status`` shows sign-in state and never prints tokens."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from spectracut.cli.app import app

runner = CliRunner()


def test_auth_status_signed_in(
    fake_bins: dict[str, Path], api_keys: dict[str, str], fakes: Any
) -> None:
    result = runner.invoke(app, ["auth", "status"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "ChatGPT (codex CLI): signed in via ChatGPT" in out
    assert "Claude (claude CLI" in out and "signed in via claude.ai, max" in out
    assert "OPENAI_API_KEY: set" in out and "ANTHROPIC_API_KEY: set" in out
    assert fakes.no_secret(out) and fakes.email not in out
    for value in api_keys.values():
        assert value not in out


def test_auth_status_signed_out(
    fake_bins: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_MODE", "signed-out")
    result = runner.invoke(app, ["auth", "status"])
    assert result.exit_code == 0, result.output
    assert "not signed in — run `codex login`" in result.output
    assert "not signed in — run `claude auth login`" in result.output
    assert "OPENAI_API_KEY: not set" in result.output


def test_auth_status_without_clis(isolated_env: Path) -> None:
    result = runner.invoke(app, ["auth", "status"])
    assert result.exit_code == 0, result.output
    assert "ChatGPT (codex CLI): not available" in result.output
    assert "Claude (claude CLI): not available" in result.output


def test_auth_status_with_unsupported_claude_bin(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPECTRACUT_CLAUDE_BIN", "claude-go")
    result = runner.invoke(app, ["auth", "status"])
    assert result.exit_code == 0, result.output
    assert "not supported" in result.output
