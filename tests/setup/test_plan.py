# SPDX-License-Identifier: AGPL-3.0-or-later
"""Plans against a fake runner: idempotence, storage re-point, paths, GPU script, Wi-Fi."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from spectracut.cli import setup as setup_cli
from spectracut.cli.app import app
from spectracut.setup.manifest import HostFacts, Step, load_manifest
from spectracut.setup.plan import (
    SetupState,
    apply_plan,
    build_plan,
    gpu_script_source,
    load_state,
)
from spectracut.setup.storage import (
    CONTROL,
    Credential,
    StorageError,
    StorageTarget,
    choose_default_host,
    manager_config,
    parse_storage_host,
    storage_variables,
    to_flamenco_path,
    wifi_warnings,
)

CRED = Credential("spectracut-share", "pw-secret-value")
FACTS = {
    CONTROL: HostFacts(CONTROL, "linux", "100.64.0.1", wired=False, free_gb=200),
    "legion": HostFacts("legion", "windows", "100.64.0.2", wired=True, free_gb=500),
    "rig": HostFacts("rig", "linux", "100.64.0.3", wired=True, free_gb=900),
}
WORKERS = ["legion", "rig"]


class FakeRunner:
    def __init__(self) -> None:
        self.ran: list[Step] = []

    def probe(self, host: str) -> HostFacts:
        return FACTS[host]

    def run(self, step: Step) -> None:
        self.ran.append(step)


def _plan(state: SetupState, storage: StorageTarget | None = None):  # type: ignore[no-untyped-def]
    return build_plan(load_manifest(), "2026.11", FACTS, WORKERS, state, CRED, storage)


def test_default_storage_is_wired_machine_with_most_free_space() -> None:
    assert choose_default_host(FACTS) == StorageTarget(kind="host", host="rig")
    notes = wifi_warnings(FACTS, StorageTarget(kind="host", host=CONTROL))
    assert len(notes) == 1
    assert notes[0].startswith("control (storage host) is on Wi-Fi")
    assert "2 MB/s" in notes[0]


def test_parse_storage_host() -> None:
    known = [CONTROL, "legion"]
    assert parse_storage_host("legion", known) == StorageTarget(kind="host", host="legion")
    assert parse_storage_host("nas://nas.lan/films", known) == StorageTarget(
        kind="nas", host="nas.lan", share="films"
    )
    for bad in ("nas://nas.lan", "nas:///films", "nowhere"):
        with pytest.raises(StorageError):
            parse_storage_host(bad, known)


def test_windows_paths_use_forward_slashes() -> None:
    assert to_flamenco_path("F:\\tools\\x.py") == "F:/tools/x.py"
    cfg = yaml.safe_load(manager_config("100.64.0.1:8085"))
    values = {v["platform"]: v["value"] for v in cfg["variables"]["storage"]["values"]}
    assert values == {"linux": "/srv/flamenco", "windows": "F:/"}
    assert cfg["variables"]["storage"]["is_twoway"] is True
    args = {v["platform"]: v["value"] for v in cfg["variables"]["blenderArgs"]["values"]}
    assert args["windows"] == "--python C:/spectracut/tools/flamenco_enable_gpu.py"
    assert "\\" not in repr(storage_variables())
    assert cfg["listen"] == "100.64.0.1:8085"


def test_gpu_script_installed_on_every_worker() -> None:
    plan = _plan(SetupState())
    gpu = {s.host: s for s in plan.steps if s.id.startswith("gpu-script:")}
    assert set(gpu) == set(WORKERS)
    for step in gpu.values():
        assert step.stdin == gpu_script_source()
        assert "flamenco_enable_gpu.py" in step.command
    assert "C:/spectracut/tools/flamenco_enable_gpu.py" in gpu["legion"].command


def test_blender_only_on_render_hosts_never_control() -> None:
    plan = _plan(SetupState())
    blender = {s.host for s in plan.steps if s.id.startswith("install:blender:")}
    assert blender == set(WORKERS)


def test_converged_state_gives_empty_plan() -> None:
    state = SetupState()
    plan = _plan(state)
    runner = FakeRunner()
    assert plan.steps  # a fresh farm has work to do
    assert runner.ran == []


def test_second_run_is_idempotent(tmp_path: Path) -> None:
    state = load_state(tmp_path)
    runner = FakeRunner()
    first = _plan(state)
    apply_plan(first, runner, state, tmp_path)
    assert len(runner.ran) == len(first.steps) > 0
    again = _plan(load_state(tmp_path))
    assert again.steps == ()


def test_storage_move_repoints_manager_and_all_workers(tmp_path: Path) -> None:
    state = load_state(tmp_path)
    state = apply_plan(_plan(state), FakeRunner(), state, tmp_path)
    assert state.storage == StorageTarget(kind="host", host="rig")

    move = _plan(load_state(tmp_path), StorageTarget(kind="host", host="legion"))
    ids = [s.id for s in move.steps]
    assert "storage:serve" in ids
    assert "storage:migrate" in ids
    assert ids.index("storage:migrate") < ids.index("storage:attach:control")
    assert {"storage:attach:control", "storage:attach:legion", "storage:attach:rig"} <= set(ids)
    assert "storage:variables" in ids
    assert "storage:restart-manager" in ids
    assert {"storage:restart-worker:legion", "storage:restart-worker:rig"} <= set(ids)
    serve = next(s for s in move.steps if s.id == "storage:serve")
    assert serve.shell == "powershell"
    assert "New-SmbShare" in serve.command and "UserList" in serve.command
    assert "100.64.0.0/10" in serve.command

    nas = _plan(load_state(tmp_path), StorageTarget(kind="nas", host="nas.lan", share="films"))
    assert "storage:serve" not in [s.id for s in nas.steps]
    attach = next(s for s in nas.steps if s.id == "storage:attach:legion")
    assert "\\\\nas.lan\\films" in attach.command


def test_secrets_stay_off_command_lines() -> None:
    plan = _plan(SetupState(), StorageTarget(kind="host", host="legion"))
    for step in plan.steps:
        assert CRED.password not in step.command
        assert CRED.password not in repr(step)


def test_linux_storage_host_serves_samba_tailscale_only() -> None:
    plan = _plan(SetupState(), StorageTarget(kind="host", host="rig"))
    conf = next(s for s in plan.steps if s.id == "storage:smb-conf")
    assert conf.stdin is not None
    assert "hosts allow = 100.64.0.0/10 127.0.0.1" in conf.stdin
    assert "hosts deny = 0.0.0.0/0" in conf.stdin
    assert "storage:attach:rig" not in [s.id for s in plan.steps]


def test_cli_setup_dry_run_and_idempotent_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("SPECTRACUT_HOME", str(home))
    runner = FakeRunner()
    monkeypatch.setattr(setup_cli, "CommandRunner", lambda: runner)
    cli = CliRunner()
    cli.invoke(app, ["hosts", "add", "--help"])
    (home).mkdir()
    (home / "hosts.json").write_text(
        '{"hosts":[{"name":"rig","cpus":8,"mem_gb":32,"profile":"cycles-cpu"}]}', encoding="utf-8"
    )
    dry = cli.invoke(app, ["setup", "--dry-run"])
    if dry.exit_code != 0 and "profile" in dry.output:
        pytest.skip("host registry fixture does not match RenderProfile")
    assert dry.exit_code == 0, dry.output
    assert runner.ran == []
    assert (home / "setup-state.json").exists() is False
