# SPDX-License-Identifier: AGPL-3.0-or-later
"""Suite-wide fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from spectracut.setup.model_assets import MODEL_ROOT_ENV


@pytest.fixture(autouse=True)
def _private_model_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the host model root (and its ``models-serve`` lock) inside each test's tmp dir.

    Otherwise tests that run ``serve`` would share ``~/.local/share/spectracut/models``
    with every other test run on the machine and wait on each other's lock.
    """
    monkeypatch.setenv(MODEL_ROOT_ENV, str(tmp_path / "model-root"))
