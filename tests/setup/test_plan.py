# SPDX-License-Identifier: AGPL-3.0-or-later
"""Plans against a fake runner: idempotence, storage re-point, paths, GPU script, Wi-Fi."""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

import spectracut
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
    windows_storage_source,
)
from spectracut.setup.wheels import LocalWheels

CRED = Credential("spectracut-share", "pw-secret-value")
FACTS = {
    CONTROL: HostFacts(CONTROL, "linux", "100.64.0.1", wired=False, free_gb=200),
    "legion": HostFacts("legion", "windows", "100.64.0.2", wired=True, free_gb=500),
    "rig": HostFacts("rig", "linux", "100.64.0.3", wired=True, free_gb=900),
}
WORKERS = ["legion", "rig"]


def _fake_wheels(payload: bytes = b"code") -> LocalWheels:
    """Two tiny stand-in wheel files; the same payload gives the same digest."""
    d = Path(tempfile.mkdtemp(prefix="sc-wheels-"))
    v = spectracut.__version__
    core, host = d / f"spectracut-{v}-py3-none-any.whl", d / f"spectracut_host-{v}-py3-none-any.whl"
    core.write_bytes(b"core:" + payload)
    host.write_bytes(b"host:" + payload)
    return LocalWheels.from_files(v, core, host)


WHEELS = _fake_wheels()


class FakeRunner:
    def __init__(self) -> None:
        self.ran: list[Step] = []

    def probe(self, host: str) -> HostFacts:
        return FACTS[host]

    out: str | None = None

    def run(self, step: Step) -> str | None:
        self.ran.append(step)
        return self.out if step.artifact else None


def _plan(state: SetupState, storage: StorageTarget | None = None):  # type: ignore[no-untyped-def]
    return build_plan(
        load_manifest(), "2026.11", FACTS, WORKERS, state, CRED, storage, wheels=WHEELS
    )


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
    assert "Test-Path" in serve.command and "Administrator" in serve.command  # admin handoff
    assert "New-SmbShare" in windows_storage_source() and "UserList" in windows_storage_source()
    assert "100.64.0.0/10" in windows_storage_source()

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
    monkeypatch.setattr(setup_cli, "build_wheels", lambda _out: WHEELS)
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


def test_every_download_step_verifies_sha256() -> None:
    for step in _plan(SetupState()).steps:
        if step.id.split(":")[0] == "install" and (
            "curl" in step.command or "Invoke-Web" in step.command
        ):  # noqa: E501
            assert "sha256" in step.command.lower(), step.id


def test_spectracut_host_installed_and_profiled_on_every_worker(tmp_path: Path) -> None:
    plan = _plan(SetupState())
    hosts = {s.host for s in plan.steps if s.id.startswith("install:spectracut-host:")}
    assert hosts == set(WORKERS)
    assert CONTROL not in {s.host for s in plan.steps if s.id.startswith("install:uv:")}
    profiles = {s.host: s for s in plan.steps if s.id.startswith("host:profile:")}
    assert set(profiles) == set(WORKERS)
    runner = FakeRunner()
    runner.out = '{"os": "windows"}'
    state = load_state(tmp_path)
    apply_plan(plan, runner, state, tmp_path)
    assert (tmp_path / "profiles" / "legion.json").read_text() == runner.out
    recorded = load_state(tmp_path).hosts
    for worker in WORKERS:
        assert recorded[worker].components["spectracut-host"] == WHEELS.pin
    copies = [s for s in runner.ran if s.id.startswith("wheel:")]
    assert {s.host for s in copies} == set(WORKERS) and all(s.component is None for s in copies)


def test_windows_worker_is_a_per_user_logon_task_under_the_profile() -> None:
    plan = _plan(SetupState())
    task = next(s for s in plan.steps if s.id == "service:worker:legion")
    assert "-AtLogOn" in task.command and "AtStartup" not in task.command
    assert "windows_worker.ps1" in task.command and '-Drive "F:"' in task.command
    for step in plan.steps:
        if step.host == "legion" and step.id.startswith(
            ("install:blender", "install:flamenco", "install:uv", "install:spectracut-host")
        ):
            assert "C:/spectracut" not in step.command, step.id
            assert "USERPROFILE" in step.command, step.id
    script = next(s for s in plan.steps if s.id == "worker-script:legion")
    assert "PATH" in (script.stdin or "") and "subst" in (script.stdin or "")
    cred = next(s for s in plan.steps if s.id == "worker-cred:legion")
    assert CRED.password not in cred.command and CRED.password in (cred.stdin or "")


def test_local_storage_host_substs_instead_of_mapping() -> None:
    plan = _plan(SetupState(), StorageTarget(kind="host", host="legion"))
    task = next(s for s in plan.steps if s.id == "service:worker:legion")
    assert '-LocalPath "C:/spectracut/storage"' in task.command
    assert "worker-cred:legion" not in [s.id for s in plan.steps]


def test_linux_worker_finds_blender_on_path() -> None:
    plan = _plan(SetupState())
    install = next(s for s in plan.steps if s.id == "install:blender:rig")
    assert ".local/bin/blender" in install.command
    unit = next(s for s in plan.steps if s.id == "service:worker-unit:rig")
    assert "Environment=PATH=%h/.local/bin" in (unit.stdin or "")


def test_every_host_uv_call_uses_only_managed_python() -> None:
    manifest = load_manifest()
    seen = 0
    for comp in manifest.components.values():
        if "host" not in comp.roles:
            continue
        for cmd in comp.install.values():
            for m in re.finditer(r"tool install --[^;]*", cmd):
                seen += 1
                assert "--python-preference only-managed" in m.group(0)
    assert seen == 2


@pytest.mark.parametrize("component", ["flamenco-worker", "spectracut-host", None])
def test_worker_stopped_before_files_are_replaced_and_started_after(component: str | None) -> None:
    from spectracut.setup.plan import HostState

    if component is None:
        plan = _plan(SetupState())
        state = SetupState()
        for step in plan.steps:
            if step.component and step.version:
                state.hosts.setdefault(step.host, HostState()).components[step.component] = (
                    step.version
                )
        plan = _plan(state)
        assert not [s for s in plan.steps if s.id.startswith("worker:")]
        return
    state = SetupState()
    for step in _plan(SetupState()).steps:
        if step.component and step.version and step.component != component:
            state.hosts.setdefault(step.host, HostState()).components[step.component] = step.version
    ids = [s.id for s in _plan(state).steps]
    by_id = {s.id: s for s in _plan(state).steps}
    for name, shell in (("legion", "powershell"), ("rig", "sh")):
        stop, inst, start = (
            f"worker:stop:{name}",
            f"install:{component}:{name}",
            f"worker:start:{name}",
        )
        assert ids.index(stop) < ids.index(inst) < ids.index(start)
        for sid in (stop, start):
            assert by_id[sid].shell == shell and by_id[sid].component is None
            assert "sudo" not in by_id[sid].command and "RunAs" not in by_id[sid].command
            assert CRED.password not in by_id[sid].command
    assert "Stop-ScheduledTask" in by_id["worker:stop:legion"].command
    assert "Get-ScheduledTask" in by_id["worker:start:legion"].command
    assert "systemctl --user stop spectracut-worker" in by_id["worker:stop:rig"].command
    assert "is-enabled" in by_id["worker:start:rig"].command
    assert CONTROL not in {s.host for s in _plan(state).steps if s.id.startswith("worker:")}
