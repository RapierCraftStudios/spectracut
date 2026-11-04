# SPDX-License-Identifier: AGPL-3.0-or-later
"""The manifest pins every component once; upgrade picks the next tested set; doctor reports."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from spectracut.setup import component_lines
from spectracut.setup.manifest import Manifest, load_manifest
from spectracut.setup.plan import HostState, SetupState, save_state

EXPECTED = {
    "blender", "flamenco-manager", "flamenco-worker", "samba", "ardour", "supercollider",
    "audio-plugins", "local-models", "polyhaven-assets", "ambientcg-assets", "addons",
    "uv", "spectracut-host",
}  # fmt: skip


def _two_sets() -> Manifest:
    base = load_manifest()
    nxt = {**base.sets[0].versions, "blender": "4.5.16"}
    raw = base.model_dump()
    raw["sets"] = [*raw["sets"], {"id": "2026.12", "versions": nxt}]
    return Manifest.model_validate(raw)


def test_manifest_pins_every_component() -> None:
    m = load_manifest()
    assert set(m.components) == EXPECTED
    for s in m.sets:
        assert set(s.versions) == EXPECTED
    assert m.sets[0].versions["blender"].startswith("4.5.")


def test_unpinned_component_is_rejected() -> None:
    raw = load_manifest().model_dump()
    del raw["sets"][0]["versions"]["ardour"]
    with pytest.raises(ValidationError, match="ardour"):
        Manifest.model_validate(raw)


def test_upgrade_moves_to_next_tested_set() -> None:
    m = _two_sets()
    assert m.next_tested_set(None).id == "2026.11"  # type: ignore[union-attr]
    nxt = m.next_tested_set("2026.11")
    assert nxt is not None
    assert nxt.id == "2026.12"
    assert nxt.versions["blender"] == "4.5.16"
    assert m.next_tested_set("2026.12") is None


def test_doctor_lists_version_and_health(tmp_path: Path) -> None:
    m = load_manifest()
    assert "blender 4.5.15: not installed" in component_lines(tmp_path, m)
    save_state(
        SetupState(
            set_id="2026.11",
            hosts={
                "legion": HostState(components={"blender": "4.5.15", "addons": "2026.10"}),
            },
        ),
        tmp_path,
    )
    lines = component_lines(tmp_path, m)
    assert "blender 4.5.15 on legion: healthy" in lines
    assert "addons 2026.10 on legion: drift (pinned 2026.11)" in lines
    assert "ardour 8.12: not installed" in lines


def test_every_download_is_checked_against_its_published_sha256() -> None:
    m = load_manifest()
    for name in ("blender", "uv", "flamenco-manager", "flamenco-worker"):
        for os_name, cmd in m.components[name].install.items():
            assert "sha256" in cmd.lower(), (name, os_name)
            assert ("sha256sum -c" in cmd) if os_name == "linux" else ("Get-FileHash" in cmd), name
