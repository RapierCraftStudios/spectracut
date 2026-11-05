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


SYSTEM_TOOLS = {
    "curl", "tar", "sh", "docker", "uv", "blender", "smbd", "flamenco-manager", "flamenco-worker",
    "powershell", "unzip", "sudo", "test", "set", "if", "cd", "command", "pacman", "apt-get",
    "Invoke-WebRequest", "Expand-Archive", "$ErrorActionPreference", "$ProgressPreference",
}  # fmt: skip


def test_first_command_token_of_every_install_and_version_cmd_resolves() -> None:
    """The first token of each install/version_cmd is a shipped script or a whitelisted tool."""
    shipped: set[str] = set()
    for pyproject in (ROOT / "pyproject.toml", ROOT / "host" / "pyproject.toml"):
        shipped |= set(tomllib.loads(pyproject.read_text())["project"]["scripts"])
    allowed = {t.lower() for t in SYSTEM_TOOLS | shipped}
    unresolved = []
    for name, comp in load_manifest().components.items():
        for field in (comp.version_cmd, comp.install):
            for os_name, cmd in field.items():
                words = [w for w in cmd.split() if w != "&"]
                if words[0] == "(Join-Path":  # PowerShell call of a path: uv.exe tool install
                    words = ["uv"]
                tok = words[0].replace("\\", "/").rsplit("/", 1)[-1].removesuffix(".exe").lower()
                if tok not in allowed:
                    unresolved.append((name, os_name, tok))
    assert not unresolved, unresolved


WINDOWS_CHECKSUMS = ("blender", "uv", "flamenco-worker")


def _render_windows(name: str) -> str:
    m = load_manifest()
    return m.components[name].install["windows"].format(version=m.sets[0].versions[name])


@pytest.mark.parametrize("name", WINDOWS_CHECKSUMS)
def test_windows_install_decodes_byte_checksums_and_throws_on_mismatch(name: str) -> None:
    cmd = _render_windows(name)
    assert "{{" not in cmd and "}}" not in cmd
    assert "-is [byte[]]" in cmd and "[Text.Encoding]::UTF8.GetString" in cmd
    assert ").Content.Trim()" not in cmd and ").Content -split" not in cmd
    assert re.search(r"-ne \$want\) \{ throw '[^']*SHA-256 mismatch' \}", cmd)


def test_powershell_commands_silence_progress_and_report_the_real_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import base64
    import subprocess

    from spectracut.setup.manifest import Step
    from spectracut.setup.plan import CommandRunner, SetupError

    seen: list[list[str]] = []
    clixml = (
        '#< CLIXML\n<Objs Version="1.1.0.1"><Obj S="progress"><TN/></Obj>'
        '<S S="Error">Method invocation failed_x000D__x000A_</S></Objs>'
    )

    class FakePopen:
        pid = 0
        returncode = 1

        def __init__(self, argv: list[str], **_: object) -> None:
            seen.append(argv)

        def communicate(
            self, _input: str | None = None, timeout: float | None = None
        ) -> tuple[str, str]:
            return "", clixml

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    step = Step("legion", "s", "install uv", "Write-Output 1", "powershell")
    with pytest.raises(SetupError) as err:
        CommandRunner().run(step)
    enc = seen[0][-1].rsplit(" ", 1)[1]
    script = base64.b64decode(enc).decode("utf-16-le")
    assert script.startswith("$ProgressPreference = 'SilentlyContinue';")
    assert str(err.value).endswith("Method invocation failed")
    assert "CLIXML" not in str(err.value) and "<" not in str(err.value)


def test_doctor_judges_spectracut_host_by_the_control_plane_version(tmp_path: Path) -> None:
    import spectracut

    m = load_manifest()
    v = spectracut.__version__
    assert f"spectracut-host {v}: not installed" in component_lines(tmp_path, m)
    save_state(
        SetupState(
            set_id="2026.11",
            hosts={
                "legion": HostState(components={"spectracut-host": f"{v}+0123456789ab"}),
                "rig": HostState(components={"spectracut-host": "9.9.9+0123456789ab"}),
            },
        ),
        tmp_path,
    )
    lines = component_lines(tmp_path, m)
    assert f"spectracut-host {v}+0123456789ab on legion: healthy" in lines
    assert f"spectracut-host 9.9.9+0123456789ab on rig: drift (pinned {v})" in lines


def test_spectracut_host_installs_from_local_wheels_not_pypi() -> None:
    m = load_manifest()
    assert m.sets[0].versions["spectracut-host"] == "control-plane"
    comp = m.components["spectracut-host"]
    fields = {
        "wheel_dir": "W",
        "spectracut_wheel": "W/spectracut-1.2.3-py3-none-any.whl",
        "host_wheel": "W/spectracut_host-1.2.3-py3-none-any.whl",
        "public_version": "1.2.3",
    }
    for os_name, raw in comp.install.items():
        cmd = raw.format(version="1.2.3+abc", **fields)
        assert "{{" not in cmd and "}}" not in cmd, os_name
        assert '--find-links "W"' in cmd and "--reinstall" in cmd
        assert '--with "W/spectracut-1.2.3-py3-none-any.whl"' in cmd
        assert '"W/spectracut_host-1.2.3-py3-none-any.whl"' in cmd
        assert "spectracut-host==" not in cmd
        assert "--version" in cmd and "1.2.3" in cmd
        assert comp.version_cmd[os_name].endswith("--version")
    assert re.search(
        r"-ne '1\.2\.3'\) \{ throw ", comp.install["windows"].format(**fields, version="")
    )
