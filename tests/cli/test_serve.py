# SPDX-License-Identifier: AGPL-3.0-or-later
"""``spectracut serve`` binds to localhost only and needs no account (#35)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import uvicorn
from starlette.applications import Starlette
from typer.testing import CliRunner

from spectracut.cli.app import app

runner = CliRunner()


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    def fake_run(target: Any, **kwargs: Any) -> None:
        seen.append({"app": target, **kwargs})

    monkeypatch.setattr(uvicorn, "run", fake_run)
    return seen


def test_serve_defaults_to_loopback(calls: list[dict[str, Any]], tmp_path: Path) -> None:
    res = runner.invoke(app, ["serve", "--home", str(tmp_path)])
    assert res.exit_code == 0, res.output
    assert "http://127.0.0.1:7420/" in res.output
    assert len(calls) == 1
    assert calls[0]["host"] == "127.0.0.1" and calls[0]["port"] == 7420
    assert isinstance(calls[0]["app"], Starlette)


@pytest.mark.parametrize("host", ["localhost", "127.0.0.2", "::1", "[::1]"])
def test_serve_accepts_loopback_hosts(
    calls: list[dict[str, Any]], tmp_path: Path, host: str
) -> None:
    res = runner.invoke(app, ["serve", "--host", host, "--port", "8000", "--home", str(tmp_path)])
    assert res.exit_code == 0, res.output
    assert calls[0]["host"] == host.strip("[]") and calls[0]["port"] == 8000


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "::", "example.com"])
def test_serve_refuses_non_loopback_hosts(calls: list[dict[str, Any]], host: str) -> None:
    res = runner.invoke(app, ["serve", "--host", host])
    assert res.exit_code == 2
    assert "localhost only" in res.output
    assert calls == []


def test_serve_has_no_account_options() -> None:
    res = runner.invoke(app, ["serve", "--help"])
    assert res.exit_code == 0
    text = res.output.lower()
    for word in ("token", "password", "login", "account", "api-key"):
        assert f"--{word}" not in text
