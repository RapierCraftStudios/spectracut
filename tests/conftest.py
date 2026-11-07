# SPDX-License-Identifier: AGPL-3.0-or-later
"""Suite-wide fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from spectracut.setup.model_assets import MODEL_ROOT_ENV
from spectracut.spec.references import REFERENCES_STORE_ENV


@pytest.fixture(autouse=True)
def _private_model_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the host model root (and its ``models-serve`` lock) inside each test's tmp dir.

    Otherwise tests that run ``serve`` would share ``~/.local/share/spectracut/models``
    with every other test run on the machine and wait on each other's lock.
    """
    monkeypatch.setenv(MODEL_ROOT_ENV, str(tmp_path / "model-root"))


@pytest.fixture(autouse=True)
def _private_reference_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the owner's reference store at an empty tmp dir, as on CI (no keyframes).

    Tests that need keyframes write them into their own store.
    """
    monkeypatch.setenv(REFERENCES_STORE_ENV, str(tmp_path / "references-store"))


SYNTHETIC_GLB = b"glTF synthetic device kit model: "


@pytest.fixture(autouse=True)
def _synthetic_device_kit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#362: the device kit's real glbs are not committed, so in-lane tests re-pin each
    kit entry to synthetic bytes and serve them from a fake fetcher into a private
    cache: no test touches the network or the shared assets dir. The farm e2e
    (``SPECTRACUT_E2E=1``) keeps the real pins."""
    import dataclasses
    import hashlib
    import os
    from types import MappingProxyType

    if os.environ.get("SPECTRACUT_E2E") == "1":
        return
    from spectracut.studio.departments import shots
    from spectracut.templates import device_kit

    blobs: dict[str, bytes] = {}
    entries = {}
    for key, entry in device_kit.DEVICE_KIT.items():
        data = SYNTHETIC_GLB + entry.id.encode()
        url = f"https://example.invalid/{entry.id}.glb"
        blobs[url] = data
        entries[key] = dataclasses.replace(entry, url=url, sha256=hashlib.sha256(data).hexdigest())
    monkeypatch.setattr(device_kit, "DEVICE_KIT", MappingProxyType(entries))
    cache = tmp_path / "device-kit-cache"

    def ensure(entry: device_kit.DeviceAsset) -> Path:
        return device_kit.ensure_device_asset(entry, cache, fetch=blobs.__getitem__)

    monkeypatch.setattr(shots, "ensure_device_asset", ensure)
