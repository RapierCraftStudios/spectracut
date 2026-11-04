# SPDX-License-Identifier: AGPL-3.0-or-later
"""``hosts add`` on an unregistered host: probe, install, read the profile, register."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from spectracut.cli import render as render_cli
from spectracut.cli.app import app
from spectracut.cli.render import load_hosts
from spectracut.queue.profile import RenderProfile
from spectracut.setup.manifest import HostFacts, Step, load_manifest
from spectracut.setup.plan import (
    CommandRunner,
    SetupError,
    SetupState,
    build_plan,
    load_state,
)
from spectracut.setup.storage import CONTROL, Credential

PROFILE = RenderProfile(
    blender_version="4.2.3", gpu_model="NVIDIA GeForce RTX 4070 Ti", vram_gb=12, os="linux"
)


class FakeHost:
    def __init__(self, os: str = "linux", fail: str | None = None) -> None:
        self.os = os
        self.fail = fail
        self.ran: list[Step] = []

    def probe(self, host: str) -> HostFacts:
        if self.fail:
            raise SetupError(f"cannot reach {host} over SSH: {self.fail}")
        return HostFacts(host, self.os, "100.64.0.9", wired=True, free_gb=500)  # type: ignore[arg-type]

    def run(self, step: Step) -> str | None:
        self.ran.append(step)
        return PROFILE.model_dump_json() if step.artifact else None

    def resources(self, host: str, os: str) -> tuple[float, float]:
        return 16.0, 64.0


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SPECTRACUT_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


def _use(monkeypatch: pytest.MonkeyPatch, fake: FakeHost) -> None:
    monkeypatch.setattr(render_cli, "CommandRunner", lambda: fake)


def test_general_host_is_installed_profiled_and_registered_under_its_alias(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeHost()
    _use(monkeypatch, fake)
    res = CliRunner().invoke(app, ["hosts", "add", "legion", "--role", "general"])
    assert res.exit_code == 0, res.output
    ids = [s.id for s in fake.ran]
    assert "install:spectracut-host:legion" in ids
    assert "install:blender:legion" in ids
    assert ids[-1] == "host:profile:legion"
    (slot,) = load_hosts(home)
    assert (slot.name, slot.cpus, slot.mem_gb, slot.build_lanes) == ("legion", 16, 64, True)
    assert slot.profile == PROFILE
    assert slot.gpus[0].name == "NVIDIA GeForce RTX 4070 Ti"


def test_gpu_only_host_gets_4gb_cap_and_no_build_lanes(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use(monkeypatch, FakeHost())
    res = CliRunner().invoke(app, ["hosts", "add", "mrdubey-1", "--role", "gpu-only"])
    assert res.exit_code == 0, res.output
    (slot,) = load_hosts(home)
    assert slot.name == "mrdubey-1"
    assert slot.mem_gb == 4.0 and slot.build_lanes is False


def test_unreachable_host_fails_with_alias_and_error_and_registers_nothing(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use(monkeypatch, FakeHost(fail="Connection timed out"))
    res = CliRunner().invoke(app, ["hosts", "add", "ghost", "--role", "general"])
    assert res.exit_code == 2
    assert "ghost" in res.output and "Connection timed out" in res.output
    assert not (home / "hosts.json").exists()


def test_command_runner_probe_reports_alias_and_ssh_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Proc:
        returncode = 255
        stdout = ""
        stderr = "ssh: connect to host ghost port 22: Connection refused"

    runner = CommandRunner()
    monkeypatch.setattr(runner, "_exec", lambda *a, **k: Proc())
    with pytest.raises(SetupError, match=r"ghost.*Connection refused"):
        runner.probe("ghost")


def test_setup_after_bootstrap_has_no_host_install_steps(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use(monkeypatch, FakeHost())
    assert CliRunner().invoke(app, ["hosts", "add", "legion", "--role", "general"]).exit_code == 0
    state = load_state(home)
    facts = {
        CONTROL: HostFacts(CONTROL, "linux", "100.64.0.1", wired=True, free_gb=200),
        "legion": HostFacts("legion", "linux", "100.64.0.9", wired=True, free_gb=500),
    }
    manifest = load_manifest()
    plan = build_plan(
        manifest, state.set_id or manifest.sets[0].id, facts, ["legion"], state,
        Credential("u", "p"),
    )  # fmt: skip
    ids = [s.id for s in plan.steps if s.host == "legion"]
    # Blender, the worker and spectracut-host are in place; storage and the service are setup's.
    assert not [i for i in ids if i.startswith(("install:blender", "install:spectracut-host"))]
    assert not [i for i in ids if i.startswith(("install:flamenco", "install:uv", "gpu-script"))]
    assert not [i for i in ids if i.startswith("host:")]
    assert "service:worker:legion" in ids
    assert load_state(home) != SetupState()


def test_explicit_profile_file_path_needs_no_ssh(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use(monkeypatch, FakeHost(fail="must not be called"))
    pf = tmp_path / "p.json"
    pf.write_text(json.dumps(PROFILE.model_dump()), encoding="utf-8")
    args = ["hosts", "add", "rig", "--cpus", "4", "--mem-gb", "8", "--profile-file", str(pf)]
    assert CliRunner().invoke(app, args).exit_code == 0
    assert [s.name for s in load_hosts(home)] == ["rig"]
