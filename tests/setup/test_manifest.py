# SPDX-License-Identifier: AGPL-3.0-or-later
"""The manifest pins every component once; upgrade picks the next tested set; doctor reports."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

from spectracut.setup import component_lines
from spectracut.setup.manifest import Manifest, load_manifest
from spectracut.setup.plan import HostState, SetupState, save_state

EXPECTED = {
    "blender", "flamenco-manager", "flamenco-worker", "samba", "audio-image",
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
    del raw["sets"][0]["versions"]["audio-image"]
    with pytest.raises(ValidationError, match="audio-image"):
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
                "legion": HostState(components={"blender": "4.5.15", "audio-image": "2026.10"}),
            },
        ),
        tmp_path,
    )
    lines = component_lines(tmp_path, m)
    assert "blender 4.5.15 on legion: healthy" in lines
    assert "audio-image 2026.10 on legion: drift (pinned 2026.11)" in lines
    assert "samba 4.21: not installed" in lines


def test_every_download_is_checked_against_its_published_sha256() -> None:
    m = load_manifest()
    for name in ("blender", "uv", "flamenco-manager", "flamenco-worker"):
        for os_name, cmd in m.components[name].install.items():
            assert "sha256" in cmd.lower(), (name, os_name)
            assert ("sha256sum -c" in cmd) if os_name == "linux" else ("Get-FileHash" in cmd), name


ROOT = Path(__file__).resolve().parents[2]


def test_every_spectracut_command_in_the_manifest_is_shipped() -> None:
    shipped: set[str] = set()
    for pyproject in (ROOT / "pyproject.toml", ROOT / "host" / "pyproject.toml"):
        shipped |= set(tomllib.loads(pyproject.read_text())["project"]["scripts"])
    text = (ROOT / "src/spectracut/setup/manifest.yaml").read_text()
    commands = {
        m.removesuffix(".exe")
        for m in re.findall(r"(?<![\w/.-])(spectracut-[a-z]+(?:\.exe)?)(?=[ \"'])", text)
    }
    pins = set(load_manifest().components)
    unresolved = commands - shipped - pins
    assert not unresolved, f"commands not shipped by any package: {sorted(unresolved)}"
    assert "spectracut-host" in shipped


def test_audio_tools_install_from_the_pinned_image() -> None:
    comp = load_manifest().components["audio-image"]
    assert "host/docker/audio.Dockerfile" in comp.install["linux"]
    assert (ROOT / "host/docker/audio.Dockerfile").is_file()
    assert "pacman" not in comp.install["linux"] and "apt-get" not in comp.install["linux"]


def test_audio_image_install_runs_docker_build_of_the_dockerfile(tmp_path: Path) -> None:
    """Executes the manifest's install command with a recording `docker` (no daemon needed)."""
    import os
    import stat
    import subprocess

    m = load_manifest()
    version = m.sets[0].versions["audio-image"]
    cmd = m.components["audio-image"].install["linux"].format(version=version)
    cmd = cmd.replace("${{", "${").replace("}}", "}") if "{{" in cmd else cmd
    log = tmp_path / "docker.log"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "docker"
    fake.write_text(f'#!/bin/sh\necho "$@" >> {log}\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "SPECTRACUT_REPO": str(ROOT)}
    subprocess.run(["sh", "-c", cmd], env=env, check=True)
    line = log.read_text()
    assert "build -f host/docker/audio.Dockerfile" in line
    assert f"spectracut-audio:{version}" in line and "spectracut-audio:current" in line
