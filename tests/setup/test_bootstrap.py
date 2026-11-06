# SPDX-License-Identifier: AGPL-3.0-or-later
"""``hosts add`` on an unregistered host: probe, install, read the profile, register."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

import spectracut
from spectracut.cli import render as render_cli
from spectracut.cli.app import app
from spectracut.cli.render import load_hosts
from spectracut.queue.profile import RenderProfile
from spectracut.setup.manifest import HostFacts, Os, Step, load_manifest
from spectracut.setup.plan import (
    CommandRunner,
    SetupError,
    SetupState,
    build_plan,
    load_state,
)
from spectracut.setup.storage import CONTROL, Credential
from spectracut.setup.wheels import LocalWheels

PROFILE = RenderProfile(
    blender_version="4.2.3", gpu_model="NVIDIA GeForce RTX 4070 Ti", vram_gb=12, os="linux"
)


def _fake_wheels(payload: bytes = b"code") -> LocalWheels:
    """Two tiny stand-in wheel files; the same payload gives the same digest."""
    d = Path(tempfile.mkdtemp(prefix="sc-wheels-"))
    v = spectracut.__version__
    core, host = d / f"spectracut-{v}-py3-none-any.whl", d / f"spectracut_host-{v}-py3-none-any.whl"
    core.write_bytes(b"core:" + payload)
    host.write_bytes(b"host:" + payload)
    return LocalWheels.from_files(v, core, host)


WHEELS = _fake_wheels()


class FakeHost:
    def __init__(self, os: Os = "linux", fail: str | None = None, admin: bool = True) -> None:
        self.os = os
        self.fail = fail
        self.admin = admin
        self.ran: list[Step] = []

    def probe(self, host: str) -> HostFacts:
        if self.fail:
            raise SetupError(f"cannot reach {host} over SSH: {self.fail}")
        return HostFacts(
            host,
            self.os,
            "100.64.0.9",
            wired=True,
            free_gb=500,
            admin=self.admin,
            home="C:\\Users\\artac" if self.os == "windows" else "",
            account="RIG\\artac" if self.os == "windows" else "",
            sid="S-1-5-21-9-9-9-1001" if self.os == "windows" else "",
        )

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
    monkeypatch.setattr(render_cli, "build_wheels", lambda _out: WHEELS)


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
    installs = [ids.index(f"install:{c}:legion") for c in ("flamenco-worker", "spectracut-host")]
    assert ids.index("worker:stop:legion") < min(installs)
    assert max(ids.index(f"wheel:{w.name}:legion") for w in WHEELS.files) < ids.index(
        "worker:stop:legion"
    )
    assert max(installs) < ids.index("worker:start:legion") < ids.index("host:profile:legion")
    copies = [i for i in ids if i.startswith("wheel:")]
    assert copies == [
        i for w in WHEELS.files for i in (f"wheel:{w.name}:legion:0", f"wheel:{w.name}:legion")
    ]
    assert max(ids.index(c) for c in copies) < ids.index("install:spectracut-host:legion")
    (install,) = [s for s in fake.ran if s.id == "install:spectracut-host:legion"]
    assert install.version == WHEELS.pin
    assert load_state(home).hosts["legion"].components["spectracut-host"] == WHEELS.pin
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


def test_non_admin_windows_gpu_only_host_bootstraps_with_schtasks_only(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeHost(os="windows", admin=False)
    _use(monkeypatch, fake)
    res = CliRunner().invoke(
        app, ["hosts", "add", "mrdubey-1", "--role", "gpu-only", "--gpu", "RTX 4070 Ti:12"]
    )
    assert res.exit_code == 0, res.output
    by_id = {s.id: s for s in fake.ran}
    for sid in ("worker:stop:mrdubey-1", "worker:start:mrdubey-1"):
        cmd = by_id[sid].command
        assert "ScheduledTask" not in cmd and "Cim" not in cmd, sid
        assert "schtasks.exe /query /tn spectracut-worker" in cmd and "if ($has)" in cmd, sid
    assert all("Cim" not in s.command and "ScheduledTask" not in s.command for s in fake.ran)
    (slot,) = load_hosts(home)
    assert (slot.mem_gb, slot.build_lanes) == (4.0, False)
    assert [(g.name, g.vram_gb) for g in slot.gpus] == [("RTX 4070 Ti", 12)]


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
        Credential("u", "p"), wheels=WHEELS,
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

    def no_build(_out: Path) -> LocalWheels:
        raise AssertionError("--profile-file must not build wheels")

    monkeypatch.setattr(render_cli, "build_wheels", no_build)
    pf = tmp_path / "p.json"
    pf.write_text(json.dumps(PROFILE.model_dump()), encoding="utf-8")
    args = ["hosts", "add", "rig", "--cpus", "4", "--mem-gb", "8", "--profile-file", str(pf)]
    assert CliRunner().invoke(app, args).exit_code == 0
    assert [s.name for s in load_hosts(home)] == ["rig"]
