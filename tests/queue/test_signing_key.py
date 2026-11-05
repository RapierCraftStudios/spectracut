# SPDX-License-Identifier: AGPL-3.0-or-later
"""The control plane's job-signing key file: created once, 0600, found without the env var."""

from __future__ import annotations

import base64
import stat
from pathlib import Path

import pytest

from spectracut.queue.signing import (
    SIGNING_KEY_ENV,
    SIGNING_KEY_FILE,
    ensure_signing_key,
    generate_keypair,
    load_private_key,
    public_key_b64,
    read_public_key,
    signing_key_path,
)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "home"
    monkeypatch.setenv("SPECTRACUT_HOME", str(root))
    monkeypatch.delenv(SIGNING_KEY_ENV, raising=False)
    return root


def test_key_created_once_with_mode_0600_under_the_home(home: Path) -> None:
    assert read_public_key(home) is None
    public = ensure_signing_key(home)
    path = home / SIGNING_KEY_FILE
    assert signing_key_path() == path  # default home is SPECTRACUT_HOME
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    first = path.read_bytes()
    assert len(base64.b64decode(first.strip())) == 32
    assert ensure_signing_key(home) == public  # never regenerated
    assert path.read_bytes() == first
    assert read_public_key(home) == public


def test_load_private_key_finds_the_file_with_no_env_var(home: Path) -> None:
    public = ensure_signing_key()
    assert public_key_b64(load_private_key()) == public


def test_env_and_explicit_value_still_take_precedence(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_signing_key(home)
    env_private, env_public = generate_keypair()
    monkeypatch.setenv(SIGNING_KEY_ENV, env_private)
    assert public_key_b64(load_private_key()) == env_public
    value_private, value_public = generate_keypair()
    assert public_key_b64(load_private_key(value_private)) == value_public


def test_missing_key_names_env_and_file_without_key_material(home: Path) -> None:
    with pytest.raises(ValueError, match=SIGNING_KEY_ENV) as err:
        load_private_key()
    assert SIGNING_KEY_FILE in str(err.value)


def test_bad_key_file_error_does_not_echo_the_key(home: Path) -> None:
    home.mkdir(parents=True)
    (home / SIGNING_KEY_FILE).write_text("bm90LWEta2V5\n", encoding="utf-8")
    with pytest.raises(ValueError) as err:
        load_private_key()
    assert "bm90LWEta2V5" not in str(err.value)
