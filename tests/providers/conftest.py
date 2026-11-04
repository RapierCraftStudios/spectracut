# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fake ``codex`` and ``claude`` CLIs for provider unit tests.

Each fake is a small Python script written to ``tmp_path``. Its behaviour is set
by ``FAKE_MODE`` and it records its argv and the API-key env it saw to
``FAKE_RECORD`` (one JSON object per call).
"""

from __future__ import annotations

import json
import stat
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

#: A token-shaped secret the fakes leak on stdout/stderr; it must never surface.
FAKE_TOKEN = "sk-ant-oat01-" + "Zx9" * 20
FAKE_JWT = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ1c2VyLTEyMyJ9.c2lnbmF0dXJlLXRoYXQtaXMtbG9uZw"
FAKE_EMAIL = "someone@example.test"

_COMMON = r"""
import json, os, sys
argv = sys.argv[1:]
mode = os.environ.get("FAKE_MODE", "ok")
record = os.environ.get("FAKE_RECORD")
if record:
    with open(record, "a") as fh:
        fh.write(json.dumps({
            "argv": argv,
            "cwd": os.getcwd(),
            "keys": sorted(k for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY") if k in os.environ),
        }) + "\n")
TOKEN = __TOKEN__
JWT = __JWT__
"""

_CLAUDE = r"""
if argv[:2] == ["auth", "status"]:
    if mode == "signed-out":
        print(json.dumps({"loggedIn": False, "authMethod": "none"}))
        sys.exit(1)
    print(json.dumps({"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "max",
                      "email": __EMAIL__, "orgId": "org-1", "accessToken": TOKEN}))
    sys.exit(0)
if "-p" in argv:
    if mode == "expired":
        print(json.dumps({"type": "result", "is_error": True,
                          "result": "OAuth token has expired. Please run /login " + TOKEN}))
        sys.stderr.write("refresh failed for " + JWT + "\n")
        sys.exit(1)
    if mode == "empty":
        sys.exit(0)
    if mode == "is-error-ok-exit":
        print(json.dumps({"type": "result", "is_error": True, "result": "Not logged in"}))
        sys.exit(0)
    print(json.dumps({"type": "result", "is_error": False, "result": "",
                      "structured_output": {"score": 0.8, "notes": "clear and legible"},
                      "usage": {"input_tokens": 10, "cache_read_input_tokens": 3000}}))
    sys.exit(0)
sys.exit(2)
"""

_CODEX = r"""
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
    "53de0000000c4944415478da63f8cfc0000003010100c9fe92ef0000000049454e44ae426082"
)
if argv[:2] == ["login", "status"]:
    if mode == "signed-out":
        sys.stderr.write("Not logged in\n")
        sys.exit(1)
    sys.stderr.write("Logged in using ChatGPT\n")
    sys.exit(0)
if argv and argv[0] == "exec":
    if mode == "expired":
        sys.stderr.write("ERROR: 401 Unauthorized: token expired " + JWT + "\n")
        sys.exit(1)
    workdir = argv[argv.index("-C") + 1]
    if mode != "no-image":
        with open(os.path.join(workdir, "frame.png"), "wb") as fh:
            fh.write(PNG)
    print("DONE")
    sys.exit(0)
sys.exit(2)
"""


def _write_script(path: Path, body: str) -> Path:
    source = (
        f"#!{sys.executable}\n"
        + _COMMON.replace("__TOKEN__", repr(FAKE_TOKEN)).replace("__JWT__", repr(FAKE_JWT))
        + body.replace("__EMAIL__", repr(FAKE_EMAIL))
    )
    path.write_text(source)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """No real CLIs, keys or user config leak into a test."""
    for var in (
        "SPECTRACUT_CLAUDE_BIN",
        "SPECTRACUT_CODEX_BIN",
        "SPECTRACUT_PROVIDERS",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "FAKE_MODE",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("SPECTRACUT_CONFIG", raising=False)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    yield tmp_path


@pytest.fixture
def fake_bins(isolated_env: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    bin_dir = isolated_env / "fake-bin"
    bin_dir.mkdir()
    claude = _write_script(bin_dir / "claude", _CLAUDE)
    codex = _write_script(bin_dir / "codex", _CODEX)
    record = isolated_env / "calls.jsonl"
    monkeypatch.setenv("FAKE_RECORD", str(record))
    monkeypatch.setenv("SPECTRACUT_CLAUDE_BIN", str(claude))
    monkeypatch.setenv("SPECTRACUT_CODEX_BIN", str(codex))
    return {"claude": claude, "codex": codex, "record": record, "dir": bin_dir}


@pytest.fixture
def make_fake_claude(isolated_env: Path) -> Callable[[str], Path]:
    """Write the fake Claude CLI under another name (e.g. a wrapper)."""

    def make(name: str) -> Path:
        target = isolated_env / "wrappers"
        target.mkdir(exist_ok=True)
        return _write_script(target / name, _CLAUDE)

    return make


def read_calls(record: Path) -> list[dict[str, Any]]:
    if not record.exists():
        return []
    return [json.loads(line) for line in record.read_text().splitlines() if line]


def no_secret(text: str) -> bool:
    return FAKE_TOKEN not in text and FAKE_JWT not in text and "Zx9Zx9" not in text


@pytest.fixture
def api_keys(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    keys = {
        "OPENAI_API_KEY": "sk-proj-" + "q" * 40,
        "ANTHROPIC_API_KEY": "sk-ant-api03-" + "w" * 40,
    }
    for name, value in keys.items():
        monkeypatch.setenv(name, value)
    return keys


class Fakes:
    """Secrets the fake CLIs leak, plus helpers, exposed to tests as a fixture."""

    token = FAKE_TOKEN
    jwt = FAKE_JWT
    email = FAKE_EMAIL

    @staticmethod
    def no_secret(text: str) -> bool:
        return no_secret(text)

    @staticmethod
    def calls(record: Path) -> list[dict[str, Any]]:
        return read_calls(record)


@pytest.fixture
def fakes() -> Fakes:
    return Fakes()
