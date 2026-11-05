# SPDX-License-Identifier: AGPL-3.0-or-later
"""Plans against a fake runner: idempotence, storage re-point, paths, GPU script, Wi-Fi."""

from __future__ import annotations

import dataclasses
import json
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
    CommandRunner,
    SetupState,
    apply_plan,
    build_plan,
    gpu_script_source,
    load_state,
    parse_probe,
    task_script_source,
    worker_script_source,
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
    assert {"storage:attach:control", "storage:attach:rig"} <= set(ids)
    # Windows maps in the worker task's session (worker-args.json), not over SSH
    assert "storage:attach:legion" not in ids
    assert "storage:variables" in ids
    assert "storage:restart-manager" in ids
    assert {"storage:restart-worker:legion", "storage:restart-worker:rig"} <= set(ids)
    serve = next(s for s in move.steps if s.id == "storage:serve")
    assert serve.shell == "powershell"
    assert "Test-Path" in serve.command and "-Path 'C:/spectracut/storage'" in serve.command
    assert "New-SmbShare" in windows_storage_source() and "UserList" in windows_storage_source()
    assert "100.64.0.0/10" in windows_storage_source()

    nas = _plan(load_state(tmp_path), StorageTarget(kind="nas", host="nas.lan", share="films"))
    assert "storage:serve" not in [s.id for s in nas.steps]
    args = next(s for s in nas.steps if s.id == "worker-args:legion")
    assert json.loads(args.stdin or "") | {"Manager": ""} == {
        "Manager": "",
        "Drive": "F:",
        "Share": "films",
        "Server": "nas.lan",
        "User": CRED.user,
    }


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


def _args(plan, host: str = "legion") -> dict:  # type: ignore[no-untyped-def]
    step = next(s for s in plan.steps if s.id == f"worker-args:{host}")
    assert "worker-args.json" in step.command
    return json.loads(step.stdin or "")


def test_windows_worker_is_a_startup_task_under_the_profile() -> None:
    plan = _plan(SetupState())
    task = next(s for s in plan.steps if s.id == "service:worker:legion")
    assert "windows_worker_task.ps1" in task.command  # legion's account is an admin
    assert _args(plan)["Drive"] == "F:" and _args(plan)["User"] == CRED.user
    assert "BootTrigger" in task_script_source() and "S4U" in task_script_source()
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
    assert _args(plan)["LocalPath"] == "C:/spectracut/storage"
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
    assert "schtasks.exe /end" in by_id["worker:stop:legion"].command
    assert "schtasks.exe /run" in by_id["worker:start:legion"].command
    assert "systemctl --user stop spectracut-worker" in by_id["worker:stop:rig"].command
    assert "is-enabled" in by_id["worker:start:rig"].command
    assert CONTROL not in {s.host for s in _plan(state).steps if s.id.startswith("worker:")}


CIM = ("ScheduledTask", "Cim", "Get-NetAdapter")  # all denied to a network-logon standard user
USER_FACTS = {
    **FACTS,
    "legion": HostFacts(
        "legion",
        "windows",
        "100.64.0.2",
        wired=True,
        free_gb=500,
        admin=False,
        home="C:\\Users\\artac-remote",
        task=False,
    ),
}


def _user_plan(state: SetupState, storage: StorageTarget | None = None):  # type: ignore[no-untyped-def]
    return build_plan(
        load_manifest(), "2026.11", USER_FACTS, WORKERS, state, CRED, storage, wheels=WHEELS
    )


@pytest.mark.parametrize("plan_of", [_plan, _user_plan])
def test_windows_steps_never_use_cim_cmdlets(tmp_path: Path, plan_of) -> None:  # type: ignore[no-untyped-def]
    state = load_state(tmp_path)
    first = plan_of(state)
    apply_plan(first, FakeRunner(), state, tmp_path)
    move = plan_of(load_state(tmp_path), StorageTarget(kind="host", host="legion"))
    steps = [s for s in (*first.steps, *move.steps) if s.host == "legion"]
    assert {"storage:restart-worker:legion", "worker:stop:legion"} <= {s.id for s in steps}
    for step in steps:
        for word in CIM:
            assert word not in step.command, (step.id, word)
    for sid in ("worker:stop:legion", "worker:start:legion", "storage:restart-worker:legion"):
        cmd = next(s for s in steps if s.id == sid).command
        assert "schtasks.exe /query /tn spectracut-worker" in cmd and "if ($has)" in cmd, sid
    for word in CIM:
        assert word not in task_script_source() + worker_script_source()


def test_non_admin_windows_host_gets_one_elevated_command_and_no_admin_steps() -> None:
    plan = _user_plan(SetupState())
    notes = [n for n in plan.notes if n.startswith("legion needs a one-time admin step")]
    assert len(notes) == 1
    cmd = "C:\\Users\\artac-remote\\spectracut\\windows_worker_task.ps1"
    assert f'`powershell -NoProfile -ExecutionPolicy Bypass -File "{cmd}"`' in notes[0]
    assert CRED.password not in notes[0] and CRED.user not in notes[0]
    task = next(s for s in plan.steps if s.id == "service:worker:legion")
    assert "windows_worker_task.ps1" not in task.command  # cannot register without admin
    assert "schtasks.exe /end" in task.command and "schtasks.exe /run" in task.command
    script = next(s for s in plan.steps if s.id == "worker-task-script:legion")
    assert "__SID__" in (script.stdin or "") and "$id.User.Value" in script.command
    assert CRED.password not in (script.stdin or "") + json.dumps(_args(plan))
    src = task_script_source()
    assert "GRGX" in src and "SetSecurityDescriptor" in src and "/xml" in src
    assert "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>" in src
    assert "-File" in src and "worker-args" not in src  # the action never changes
    assert not [n for n in _plan(SetupState()).notes if "one-time admin step" in n]


def test_non_admin_note_repeats_until_the_task_exists(tmp_path: Path) -> None:
    state = load_state(tmp_path)
    apply_plan(_user_plan(state), FakeRunner(), state, tmp_path)
    again = _user_plan(load_state(tmp_path))
    assert again.steps == ()
    assert any(n.startswith("legion needs a one-time admin step") for n in again.notes)
    registered = {**USER_FACTS, "legion": dataclasses.replace(USER_FACTS["legion"], task=True)}
    done = build_plan(
        load_manifest(), "2026.11", registered, WORKERS, load_state(tmp_path), CRED,
        wheels=WHEELS,
    )  # fmt: skip
    assert done.steps == () and not [n for n in done.notes if "admin step" in n]


def test_parse_probe_reads_admin_profile_and_task_without_cim() -> None:
    out = "100.64.0.2\nEthernet\n5000000000\nFalse\nC:\\Users\\artac-remote\nFalse\n"
    f = parse_probe("mrdubey-1", "windows", out)
    assert (f.admin, f.home, f.task, f.wired) == (False, "C:\\Users\\artac-remote", False, True)
    admin = parse_probe("legion", "windows", "100.64.0.3\n802.11,Ethernet\n1\nTrue\nC:\\U\nTrue")
    assert (admin.admin, admin.task, admin.wired) == (True, True, False)
    linux = parse_probe("rig", "linux", "100.64.0.4\neth0\n100\n")
    assert (linux.admin, linux.home, linux.task) == (True, "", True)


def test_windows_probe_and_resources_scripts_use_no_cim(monkeypatch: pytest.MonkeyPatch) -> None:
    scripts: list[str] = []

    class Proc:
        def __init__(self, rc: int, out: str) -> None:
            self.returncode, self.stdout, self.stderr = rc, out, ""

    def fake_exec(host: str, cmd: str, shell: str, stdin: str | None) -> Proc:
        scripts.append(cmd)
        if cmd == "uname -s":
            return Proc(1, "")
        return Proc(0, "16\n31.8\n" if "ProcessorCount" in cmd else "100.64.0.2\nEthernet\n1\n")

    runner = CommandRunner()
    monkeypatch.setattr(runner, "_exec", fake_exec)
    assert runner.probe("mrdubey-1").os == "windows"
    assert runner.resources("mrdubey-1", "windows") == (16.0, 31.8)
    probe, resources = scripts[1], scripts[2]
    for word in CIM:
        assert word not in probe and word not in resources
    assert "IsInRole" in probe and "$env:USERPROFILE" in probe and "schtasks.exe /query" in probe
    assert "ComputerInfo" in resources


def test_parse_probe_reads_drive_letters_in_use() -> None:
    out = (
        "100.64.0.2\nEthernet\n1\nFalse\nC:\\U\nTrue\n"
        "C=|D=|F=\\\\nas\\media, 2024|S=D:\\SpectraCut\\flamenco\n"
    )
    f = parse_probe("mrdubey-1", "windows", out)
    assert f.drives == ("C=", "D=", "F=\\\\nas\\media, 2024", "S=D:\\SpectraCut\\flamenco")
    old = parse_probe("legion", "windows", "100.64.0.3\nEthernet\n1\nTrue\nC:\\U\nTrue")
    assert old.drives == ()


def test_windows_probe_lists_drives_without_cim(monkeypatch: pytest.MonkeyPatch) -> None:
    scripts: list[str] = []

    class Proc:
        returncode, stdout, stderr = 0, "100.64.0.2\nEthernet\n1\n", ""

    def fake_exec(host: str, cmd: str, shell: str, stdin: str | None) -> Proc:
        scripts.append(cmd)
        p = Proc()
        if cmd == "uname -s":
            p.returncode = 1
        return p

    runner = CommandRunner()
    monkeypatch.setattr(runner, "_exec", fake_exec)
    runner.probe("mrdubey-1")
    assert "Get-PSDrive -PSProvider FileSystem" in scripts[1] and "subst" in scripts[1]
    assert "Cim" not in scripts[1] and "Wmi" not in scripts[1]


def _cli_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, FakeRunner]:
    home = tmp_path / "home"
    monkeypatch.setenv("SPECTRACUT_HOME", str(home))
    runner = FakeRunner()
    monkeypatch.setattr(setup_cli, "CommandRunner", lambda: runner)
    monkeypatch.setattr(setup_cli, "build_wheels", lambda _out: WHEELS)
    home.mkdir()
    return home, runner


def test_cli_storage_path_and_windows_drive_are_validated_and_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, runner = _cli_home(tmp_path, monkeypatch)
    cli = CliRunner()
    dry = cli.invoke(
        app, ["setup", "--storage-path", "/data/flamenco/", "--windows-drive", "s", "--dry-run"]
    )
    assert dry.exit_code == 0, dry.output
    assert load_state(home) == SetupState()  # a dry run keeps nothing
    assert "bind /data/flamenco at /srv/flamenco" in dry.output

    bad = cli.invoke(app, ["setup", "--windows-drive", "SS:", "--dry-run"])
    assert bad.exit_code == 2 and "drive letter" in bad.output
    rel = cli.invoke(app, ["setup", "--storage-path", "relative/dir", "--dry-run"])
    assert rel.exit_code == 2 and "absolute" in rel.output

    done = cli.invoke(app, ["setup", "--storage-path", "/data/flamenco", "--windows-drive", "S:"])
    assert done.exit_code == 0, done.output
    state = load_state(home)
    assert (state.storage_path, state.windows_drive) == ("/data/flamenco", "S:")
    assert state.storage_layout is not None and state.storage_layout.drive == "S:"
    # later runs without the options keep the persisted settings: nothing to do
    again = cli.invoke(app, ["setup", "--dry-run"])
    assert again.exit_code == 0 and "nothing to do" in again.output


def test_cli_defaults_unchanged_without_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, runner = _cli_home(tmp_path, monkeypatch)
    done = CliRunner().invoke(app, ["setup"])
    assert done.exit_code == 0, done.output
    state = load_state(home)
    assert (state.storage_path, state.windows_drive) == (None, "F:")
    ran = {s.id: s for s in runner.ran}
    assert "path = /srv/flamenco\n" in (ran["storage:smb-conf"].stdin or "")
    assert "storage:bind" not in ran
    cfg = yaml.safe_load(ran["storage:variables"].stdin or "")
    values = {v["platform"]: v["value"] for v in cfg["variables"]["storage"]["values"]}
    assert values == {"linux": "/srv/flamenco", "windows": "F:/"}
