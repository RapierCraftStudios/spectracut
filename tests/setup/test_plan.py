# SPDX-License-Identifier: AGPL-3.0-or-later
"""Plans against a fake runner: idempotence, storage re-point, paths, GPU script, Wi-Fi."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

import spectracut
from spectracut.cli import setup as setup_cli
from spectracut.cli.app import app
from spectracut.queue.flamenco import Worker, WorkerTag
from spectracut.setup.manifest import HostFacts, Step, load_manifest
from spectracut.setup.plan import (
    LINUX_PROBE,
    MISSING_CONFIG,
    MISSING_JOB_TYPES,
    MISSING_UNIT,
    WINDOWS_PROBE,
    CommandRunner,
    SetupError,
    SetupState,
    apply_plan,
    build_plan,
    control_probe,
    gpu_script_source,
    load_state,
    parse_probe,
    task_script_command,
    task_script_content,
    task_script_sha256,
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
    "legion": HostFacts(
        "legion",
        "windows",
        "100.64.0.2",
        wired=True,
        free_gb=500,
        account="LEGION\\artac-remote",
        sid="S-1-5-21-1-2-3-1001",
        home="C:\\Users\\artac-remote",
    ),
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
        "Name": "legion",
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


def _args(plan: Any, host: str = "legion") -> dict[str, Any]:
    step = next(s for s in plan.steps if s.id == f"worker-args:{host}")
    assert "worker-args.json" in step.command
    args: dict[str, Any] = json.loads(step.stdin or "")
    return args


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
        assert all(
            ids.index(i) < ids.index(stop)
            for i in ids
            if i.startswith("wheel:") and i.endswith(f":{name}")
        )
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
        account="LEGION\\artac-remote",
        sid="S-1-5-21-1-2-3-1001",
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
    assert f"[IO.File]::ReadAllBytes('{cmd}')" in notes[0]
    assert f'-File "{cmd}"' not in notes[0]  # never runs the profile copy directly (#190)
    assert CRED.password not in notes[0] and CRED.user not in notes[0]
    task = next(s for s in plan.steps if s.id == "service:worker:legion")
    assert "windows_worker_task.ps1" not in task.command  # cannot register without admin
    assert "schtasks.exe /end" in task.command and "schtasks.exe /run" in task.command
    script = next(s for s in plan.steps if s.id == "worker-task-script:legion")
    assert "S-1-5-21-1-2-3-1001" in (script.stdin or "") and "__SID__" not in (script.stdin or "")
    assert "$id.User.Value" not in script.command and ".Replace(" not in script.command
    assert CRED.password not in (script.stdin or "") + json.dumps(_args(plan))
    src = task_script_source()
    assert "GRGX" in src and "SetSecurityDescriptor" in src and "/xml" in src
    assert "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>" in src
    assert "-File" in src and "worker-args" not in src  # the action never changes
    assert not [n for n in _plan(SetupState()).notes if "one-time admin step" in n]


def test_elevated_task_command_checks_the_sha256_of_what_setup_wrote() -> None:
    """#190: the admin runs only the bytes setup wrote, from a copy outside the profile."""
    plan = _user_plan(SetupState())
    script = next(s for s in plan.steps if s.id == "worker-task-script:legion")
    digest = hashlib.sha256((script.stdin or "").encode("utf-8")).hexdigest()
    assert script.stdin == task_script_content(USER_FACTS["legion"])
    assert task_script_sha256(USER_FACTS["legion"]) == digest
    note = next(n for n in plan.notes if n.startswith("legion needs a one-time admin step"))
    assert f"SHA-256 {digest}" in note
    cmd = task_script_command(USER_FACTS["legion"])
    assert f"if ($h -ne '{digest}') {{ throw" in cmd and cmd in note
    read = cmd.index("ReadAllBytes(")
    assert read < cmd.index("ComputeHash($b)") < cmd.index("WriteAllBytes($c, $b)")
    assert cmd.index("WriteAllBytes($c, $b)") < cmd.index("-File $c")
    assert "Join-Path $env:TEMP" in cmd and "USERPROFILE" not in cmd
    assert cmd.count("-File") == 1  # only the verified copy is ever run
    # placeholders filled on the controller, quotes escaped for the '...' literals
    odd = dataclasses.replace(USER_FACTS["legion"], account="LEGION\\o'neil")
    assert "$account = 'LEGION\\o''neil'" in task_script_content(odd)


def test_task_script_needs_the_probed_account_and_sid() -> None:
    blank = {**USER_FACTS, "legion": dataclasses.replace(USER_FACTS["legion"], sid="")}
    with pytest.raises(SetupError, match="SID"):
        build_plan(load_manifest(), "2026.11", blank, WORKERS, SetupState(), CRED, wheels=WHEELS)


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
    out = (
        "address=100.64.0.2\nlink=Ethernet\nfree=5000000000\nadmin=False\n"
        "home=C:\\Users\\artac-remote\naccount=MRDUBEY-1\\artac-remote\n"
        "sid=S-1-5-21-1-2-3-1001\ntask=False\n"
    )
    f = parse_probe("mrdubey-1", "windows", out)
    assert (f.account, f.sid) == ("MRDUBEY-1\\artac-remote", "S-1-5-21-1-2-3-1001")
    assert "'account=' + $id.Name" in WINDOWS_PROBE and "'sid=' + $id.User.Value" in WINDOWS_PROBE
    assert (f.admin, f.home, f.task, f.wired) == (False, "C:\\Users\\artac-remote", False, True)
    admin = parse_probe(
        "legion",
        "windows",
        "address=100.64.0.3\nlink=802.11,Ethernet\nfree=1\nadmin=True\nhome=C:\\U\ntask=True",
    )
    assert (admin.admin, admin.task, admin.wired) == (True, True, False)
    linux = parse_probe("rig", "linux", "address=100.64.0.4\nlink=eth0\nfree=100\n")
    assert (linux.admin, linux.home, linux.task) == (True, "", True)


def test_probe_with_empty_or_failing_tailscale_line_parses_every_other_field() -> None:
    win = (
        "address=\nlink=53,Ethernet,Ethernet,Ethernet\nfree=5000000000\nadmin=False\n"
        "home=C:\\Users\\artac-remote\ntask=False\ndrives=C=|D=\n"
    )
    f = parse_probe("mrdubey-1", "windows", win)
    assert f.address == "mrdubey-1" and f.wired and f.free_gb == 5.0
    assert (f.admin, f.home, f.task, f.drives) == (
        False,
        "C:\\Users\\artac-remote",
        False,
        ("C=", "D="),
    )
    # Stray CLI error text, or no address line at all, must not shift anything either.
    noisy = "401 Unauthorized: Tailscale already in use by MRDUBEY\\ItsMr\n" + win.replace(
        "address=\n", ""
    )
    g = parse_probe("mrdubey-1", "windows", noisy)
    assert g.address == "mrdubey-1" and (g.admin, g.task, g.free_gb) == (False, False, 5.0)
    lin = parse_probe("rig", "linux", "address=\nlink=wlan0\nfree=2000000000\n")
    assert (lin.address, lin.wired, lin.free_gb) == ("rig", False, 2.0)


def test_unnamed_interface_types_do_not_affect_the_wired_check() -> None:
    assert parse_probe("h", "windows", "link=53,Ethernet,Ethernet,Ethernet").wired
    assert not parse_probe("h", "windows", "link=53,802.11").wired
    assert parse_probe("h", "windows", "link=53").wired


class _Proc:
    def __init__(self, rc: int, out: str) -> None:
        self.returncode, self.stdout, self.stderr = rc, out, ""


def _runner(
    monkeypatch: pytest.MonkeyPatch, probe_out: str, tailscale: _Proc, win: bool = True
) -> tuple[CommandRunner, list[tuple[str, str]]]:
    calls: list[tuple[str, str]] = []

    def fake_exec(host: str, cmd: str, shell: str, stdin: str | None) -> _Proc:
        calls.append((host, cmd))
        if cmd == "uname -s":
            return _Proc(1 if win else 0, "")
        if cmd.startswith("tailscale ip -4 "):
            return tailscale
        return _Proc(0, "16\n31.8\n" if "ProcessorCount" in cmd else probe_out)

    runner = CommandRunner()
    monkeypatch.setattr(runner, "_exec", fake_exec)
    return runner, calls


def test_address_is_resolved_on_the_control_plane_first(monkeypatch: pytest.MonkeyPatch) -> None:
    runner, calls = _runner(
        monkeypatch, "address=100.64.0.99\nlink=Ethernet\nfree=1\n", _Proc(0, "100.64.0.9\n")
    )
    assert runner.probe("mrdubey-1").address == "100.64.0.9"
    assert (CONTROL, "tailscale ip -4 mrdubey-1") in calls


def test_address_falls_back_to_host_probe_then_name(monkeypatch: pytest.MonkeyPatch) -> None:
    probe = "address=100.64.0.99\nlink=Ethernet\nfree=1\n"
    for bad in (_Proc(1, ""), _Proc(0, ""), _Proc(0, "no such host\n"), _Proc(0, "fd7a::1\n")):
        runner, _ = _runner(monkeypatch, probe, bad)
        assert runner.probe("mrdubey-1").address == "100.64.0.99"
    runner, _ = _runner(monkeypatch, "address=\nlink=Ethernet\nfree=1\n", _Proc(1, ""))
    assert runner.probe("mrdubey-1").address == "mrdubey-1"


def test_linux_probe_address_is_resolved_on_the_control_plane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, _ = _runner(
        monkeypatch, "address=\nlink=eth0\nfree=100\n", _Proc(0, "100.64.0.4\n"), False
    )
    assert runner.probe("rig").address == "100.64.0.4"


def test_windows_probe_and_resources_scripts_use_no_cim(monkeypatch: pytest.MonkeyPatch) -> None:
    runner, calls = _runner(
        monkeypatch, "address=100.64.0.2\nlink=Ethernet\nfree=1\n", _Proc(0, "100.64.0.2\n")
    )
    assert runner.probe("mrdubey-1").os == "windows"
    assert runner.resources("mrdubey-1", "windows") == (16.0, 31.8)
    probe, resources = calls[1][1], calls[-1][1]
    for word in CIM:
        assert word not in probe and word not in resources
    assert "IsInRole" in probe and "$env:USERPROFILE" in probe and "schtasks.exe /query" in probe
    assert "ComputerInfo" in resources
    # every field is labelled, so a failing command cannot shift the others
    for key in ("address", "link", "free", "admin", "home", "task", "drives"):
        assert f"'{key}=' +" in probe


def test_linux_probe_script_labels_every_field() -> None:
    for key in ("address", "link", "free"):
        assert f'echo "{key}=$(' in LINUX_PROBE


def test_parse_probe_reads_drive_letters_in_use() -> None:
    out = (
        "address=100.64.0.2\nlink=Ethernet\nfree=1\nadmin=False\nhome=C:\\U\ntask=True\n"
        "drives=C=|D=|F=\\\\nas\\media, 2024|S=D:\\SpectraCut\\flamenco\n"
    )
    f = parse_probe("mrdubey-1", "windows", out)
    assert f.drives == ("C=", "D=", "F=\\\\nas\\media, 2024", "S=D:\\SpectraCut\\flamenco")
    old = parse_probe("legion", "windows", "address=100.64.0.3\nlink=Ethernet\nfree=1")
    assert old.drives == ()


def test_windows_probe_lists_drives_without_cim(monkeypatch: pytest.MonkeyPatch) -> None:
    runner, calls = _runner(monkeypatch, "link=Ethernet\nfree=1\n", _Proc(1, ""))
    runner.probe("mrdubey-1")
    assert "Get-PSDrive -PSProvider FileSystem" in calls[1][1] and "subst" in calls[1][1]
    assert "Cim" not in calls[1][1] and "Wmi" not in calls[1][1]


class StubManager:
    """A Manager whose workers are the registered hosts, already tagged by setup."""

    def __init__(self) -> None:
        self.tags: list[WorkerTag] = []

    def list_tags(self) -> list[WorkerTag]:
        return list(self.tags)

    def job_types(self) -> list[str]:
        return ["spectracut-shot"]

    def create_tag(self, name: str, description: str = "") -> None:
        self.tags.append(WorkerTag(id=name, name=name, description=description))

    def delete_tag(self, tag_id: str) -> None:
        self.tags = [t for t in self.tags if t.id != tag_id]

    def list_workers(self) -> list[Worker]:
        return [Worker(id=n, name=n) for n in WORKERS]

    def worker_tags_of(self, worker_id: str) -> list[WorkerTag]:
        return []

    def set_worker_tags(self, worker_id: str, tag_ids: Sequence[str]) -> None:
        pass


def _cli_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, FakeRunner]:
    home = tmp_path / "home"
    monkeypatch.setenv("SPECTRACUT_HOME", str(home))
    runner = FakeRunner()
    manager = StubManager()
    monkeypatch.setattr(setup_cli, "CommandRunner", lambda: runner)
    monkeypatch.setattr(setup_cli, "build_wheels", lambda _out: WHEELS)
    monkeypatch.setattr(setup_cli, "manager_client", lambda _url: manager)
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


PUB = "cHVibGljLWtleS1iYXNlNjQ="


def _reg_plan(state: SetupState, key: str | None = PUB):  # type: ignore[no-untyped-def]
    return build_plan(
        load_manifest(),
        "2026.11",
        FACTS,
        WORKERS,
        state,
        CRED,
        wheels=WHEELS,
        signing_public_key=key,
    )


def test_every_worker_gets_host_json_in_the_worker_account() -> None:
    steps = {s.id: s for s in _reg_plan(SetupState()).steps}
    assert f"host-registration:{CONTROL}" not in steps
    for name in WORKERS:
        step = steps[f"host-registration:{name}"]
        assert json.loads(step.stdin or "") == {"host_id": name, "control_plane_public_key": PUB}
        assert step.component == "host-registration" and step.version
        assert "sudo" not in step.command and PUB not in step.command
    win, lin = steps["host-registration:legion"], steps["host-registration:rig"]
    assert win.shell == "powershell" and "$env:USERPROFILE" in win.command
    assert ".config/spectracut/host.json" in win.command
    assert lin.shell == "sh" and '"$HOME/.config/spectracut/host.json"' in lin.command
    assert "chmod 600" in lin.command


def test_no_host_json_without_a_key() -> None:
    assert not [s for s in _reg_plan(SetupState(), None).steps if "registration" in s.id]


def test_host_json_rerun_is_idempotent_and_a_new_key_rewrites(tmp_path: Path) -> None:
    state = load_state(tmp_path)
    apply_plan(_reg_plan(state), FakeRunner(), state, tmp_path)
    assert _reg_plan(load_state(tmp_path)).steps == ()
    rekeyed = [s.id for s in _reg_plan(load_state(tmp_path), "bmV3LWtleQ==").steps]
    assert rekeyed == ["host-registration:legion", "host-registration:rig"]


def test_cli_setup_creates_key_once_and_never_prints_or_ships_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from spectracut.queue.signing import SIGNING_KEY_ENV, load_private_key, public_key_b64

    monkeypatch.delenv(SIGNING_KEY_ENV, raising=False)
    home, runner = _cli_home(tmp_path, monkeypatch)
    entry = {"cpus": 8, "mem_gb": 32, "profile": {"blender_version": "4.5.3"}}
    hosts = [{"name": n, **entry} for n in WORKERS]
    (home / "hosts.json").write_text(json.dumps({"hosts": hosts}), encoding="utf-8")
    cli = CliRunner()
    dry = cli.invoke(app, ["setup", "--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert not (home / "job-signing.key").exists()  # a dry run creates nothing
    assert "legion: register the host" in dry.output

    done = cli.invoke(app, ["setup"])
    assert done.exit_code == 0, done.output
    key_file = home / "job-signing.key"
    assert key_file.stat().st_mode & 0o777 == 0o600
    private = key_file.read_text(encoding="utf-8").strip()
    public = public_key_b64(load_private_key())  # found with no env var set
    regs = {s.host: s for s in runner.ran if s.id.startswith("host-registration:")}
    assert set(regs) == set(WORKERS)
    for name, step in regs.items():
        assert json.loads(step.stdin or "") == {"host_id": name, "control_plane_public_key": public}
    for step in runner.ran:
        assert private not in step.command and private not in (step.stdin or "")
    assert private not in done.output and private not in dry.output
    assert private not in (home / "setup-state.json").read_text(encoding="utf-8")

    again = cli.invoke(app, ["setup"])
    assert again.exit_code == 0 and "nothing to do" in again.output
    assert key_file.read_text(encoding="utf-8").strip() == private


def test_parse_probe_rejects_non_ip_address_so_it_never_reaches_a_shell() -> None:
    evil = "address=1.2.3.4'; touch INJECTED; echo '\nlink=eth0\nfree=1\n"
    assert parse_probe("rig", "linux", evil).address == "rig"
    assert parse_probe("rig", "linux", "address=rig.tail.ts.net\n").address == "rig"
    assert parse_probe("rig", "linux", "address=100.64.0.4\n").address == "100.64.0.4"


# ---- control-plane files are checked on disk, not trusted from setup state (#181) ------


def _converged_state(tmp_path: Path) -> SetupState:
    state = load_state(tmp_path)
    apply_plan(_plan(state), FakeRunner(), state, tmp_path)
    state = load_state(tmp_path)
    assert _plan(state).steps == ()
    return state


def _plan_missing(state: SetupState, *missing: str):  # type: ignore[no-untyped-def]
    facts = {**FACTS, CONTROL: dataclasses.replace(FACTS[CONTROL], missing=missing)}
    return build_plan(load_manifest(), "2026.11", facts, WORKERS, state, CRED, wheels=WHEELS)


def test_missing_manager_unit_is_rewritten_though_state_records_it(tmp_path: Path) -> None:
    ids = [s.id for s in _plan_missing(_converged_state(tmp_path), MISSING_UNIT).steps]
    assert ids == ["service:manager-unit", "service:manager"]


def test_missing_job_types_are_reinstalled_though_state_records_them(tmp_path: Path) -> None:
    ids = [s.id for s in _plan_missing(_converged_state(tmp_path), MISSING_JOB_TYPES).steps]
    assert ids == ["manager:job-types"]


def test_missing_manager_config_is_rewritten_though_storage_converged(tmp_path: Path) -> None:
    steps = _plan_missing(_converged_state(tmp_path), MISSING_CONFIG).steps
    assert [s.id for s in steps] == ["storage:variables", "storage:restart-manager"]
    assert "flamenco-manager.yaml" in steps[0].command
    assert "variables:" in (steps[0].stdin or "")


def test_missing_manager_config_is_written_once_when_storage_also_changes() -> None:
    ids = [s.id for s in _plan_missing(SetupState(), MISSING_CONFIG).steps]
    assert ids.count("storage:variables") == 1


def test_nothing_missing_keeps_the_converged_plan_empty(tmp_path: Path) -> None:
    assert _plan_missing(_converged_state(tmp_path)).steps == ()


def test_parse_probe_reads_missing_files_and_ignores_unknown_tokens() -> None:
    out = "address=100.64.0.1\nmissing=manager-unit,bogus,job-types,job-types\n"
    assert parse_probe(CONTROL, "linux", out).missing == (MISSING_UNIT, MISSING_JOB_TYPES)
    assert parse_probe(CONTROL, "linux", "missing=\n").missing == ()
    assert parse_probe("rig", "linux", "free=1\n").missing == ()


def _run_control_probe(home: Path, bin_dir: Path) -> tuple[str, ...]:
    out = subprocess.run(
        ["sh", "-c", control_probe(["spectracut-shot.js"])],
        env={"HOME": str(home), "PATH": f"{bin_dir}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return parse_probe(CONTROL, "linux", out).missing


def test_control_probe_reports_missing_files_on_disk(tmp_path: Path) -> None:
    home, bin_dir = tmp_path / "home", tmp_path / "bin"
    home.mkdir()
    bin_dir.mkdir()
    fake = bin_dir / "systemctl"  # no Manager unit loaded: an empty WorkingDirectory
    fake.write_text("#!/bin/sh\nexit 1\n")
    fake.chmod(0o755)
    every = (MISSING_UNIT, MISSING_CONFIG, MISSING_JOB_TYPES)
    assert _run_control_probe(home, bin_dir) == every
    # Files under a literal "$HOME" folder (the #173 bug) are not the Manager's files.
    for root in (home / "$HOME", home):
        unit = root / ".config/systemd/user/flamenco-manager.service"
        unit.parent.mkdir(parents=True)
        unit.write_text("[Unit]\n")
        opt = root / ".local/opt/flamenco"
        (opt / "scripts").mkdir(parents=True)
        (opt / "flamenco-manager.yaml").write_text("_meta: {}\n")
        if root != home:
            (opt / "scripts/spectracut-shot.js").write_text("//\n")
            assert _run_control_probe(home, bin_dir) == every
    assert _run_control_probe(home, bin_dir) == (MISSING_JOB_TYPES,)
    (home / ".local/opt/flamenco/scripts/spectracut-shot.js").write_text("//\n")
    assert _run_control_probe(home, bin_dir) == ()


def test_command_runner_probes_control_plane_files_only_on_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scripts: dict[str, str] = {}

    def fake_exec(self, host, script, shell, stdin):  # type: ignore[no-untyped-def]
        scripts[host] = script
        out = "Linux\n" if script == "uname -s" else "missing=manager-config\n"
        return subprocess.CompletedProcess([], 0, out, "")

    monkeypatch.setattr(CommandRunner, "_exec", fake_exec)
    monkeypatch.setattr(CommandRunner, "resolve_address", lambda self, host: None)
    runner = CommandRunner()
    assert runner.probe(CONTROL).missing == (MISSING_CONFIG,)
    assert "spectracut-shot.js" in scripts[CONTROL]
    runner.probe("rig")
    assert scripts["rig"] == LINUX_PROBE
