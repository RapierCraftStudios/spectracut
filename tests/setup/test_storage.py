# SPDX-License-Identifier: AGPL-3.0-or-later
"""Windows storage host (admin-script handoff) and the gpu-only host role."""

from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

import spectracut
from spectracut.cli.render import HostEntry
from spectracut.queue.profile import RenderProfile
from spectracut.setup.manifest import HostFacts, Step, load_manifest
from spectracut.setup.plan import Plan, SetupState, apply_plan, build_plan, worker_script_source
from spectracut.setup.storage import (
    CONTROL,
    Credential,
    StorageError,
    StorageTarget,
    normalize_drive,
    normalize_storage_path,
    storage_notes,
    verified_admin_command,
    windows_storage_sha256,
    windows_storage_source,
    write_file_step,
)
from spectracut.setup.wheels import LocalWheels

CRED = Credential("spectracut-share", "pw-secret-value")
FACTS = {
    CONTROL: HostFacts(CONTROL, "linux", "100.64.0.1", wired=True, free_gb=200),
    # a standard (non-admin) SSH account: the storage share needs the one-time admin handoff
    "legion": HostFacts(
        "legion",
        "windows",
        "100.64.0.2",
        wired=True,
        free_gb=500,
        admin=False,
        account="LEGION\\artac-remote",
        sid="S-1-5-21-1-2-3-1001",
        home="C:\\Users\\artac-remote",
    ),
    "rig": HostFacts("rig", "linux", "100.64.0.3", wired=True, free_gb=900),
}
TARGET = StorageTarget(kind="host", host="legion")


def _fake_wheels(payload: bytes = b"code") -> LocalWheels:
    """Two tiny stand-in wheel files; the same payload gives the same digest."""
    d = Path(tempfile.mkdtemp(prefix="sc-wheels-"))
    v = spectracut.__version__
    core, host = d / f"spectracut-{v}-py3-none-any.whl", d / f"spectracut_host-{v}-py3-none-any.whl"
    core.write_bytes(b"core:" + payload)
    host.write_bytes(b"host:" + payload)
    return LocalWheels.from_files(v, core, host)


WHEELS = _fake_wheels()


def _plan(
    state: SetupState | None = None,
    facts: dict[str, HostFacts] | None = None,
    target: StorageTarget = TARGET,
) -> Plan:
    return build_plan(
        load_manifest(), "2026.11", facts or FACTS, ["legion", "rig"], state or SetupState(),
        CRED, target, wheels=WHEELS,
    )  # fmt: skip


D_PATH = "D:/SpectraCut/flamenco"
CUSTOM = SetupState(storage_path=D_PATH, windows_drive="S:")


def _step(plan: Plan, step_id: str) -> Step:
    return next(s for s in plan.steps if s.id == step_id)


def test_windows_storage_host_writes_admin_script_then_gates_then_mounts_and_maps() -> None:
    plan = _plan()
    ids = [s.id for s in plan.steps]
    gate = ids.index("storage:serve")
    assert ids.index("storage:admin-script") < ids.index("storage:admin-password") < gate
    # mount on the control plane (CIFS) and map on the other worker come after the gate
    assert ids.index("storage:attach:" + CONTROL) > gate
    assert ids.index("storage:attach:rig") > gate
    script = next(s for s in plan.steps if s.id == "storage:admin-script")
    assert script.stdin == windows_storage_source()
    assert "-RemoteAddress 100.64.0.0/10" in windows_storage_source()
    assert "RunAsAdministrator" in windows_storage_source()
    gate_step = plan.steps[gate]
    assert "exit 1" in gate_step.command and "Administrator" in gate_step.command
    assert any("one-time admin step" in n for n in plan.notes)


def test_windows_storage_password_stays_off_command_lines() -> None:
    for step in _plan().steps:
        assert CRED.password not in step.command
    pw = next(s for s in _plan().steps if s.id == "storage:admin-password")
    assert pw.stdin == CRED.password + "\n"
    assert "icacls" in pw.command and "S-1-5-32-544" in pw.command


def test_linux_storage_host_needs_no_admin_note() -> None:
    assert storage_notes(StorageTarget(kind="host", host="rig"), FACTS) == []


PROFILE = RenderProfile(blender_version="4.5.3", gpu_model="RTX 4070 Ti", vram_gb=12)


def test_gpu_only_role_caps_ram_at_4gb_and_drops_build_lanes() -> None:
    entry = HostEntry(name="main-pc", cpus=16, mem_gb=64, profile=PROFILE, role="gpu-only")
    slot = entry.slot()
    assert slot.mem_gb == 4
    assert slot.build_lanes is False
    capped = HostEntry(name="x", cpus=4, mem_gb=64, profile=PROFILE, role="gpu-only", ram_cap_gb=2)
    assert capped.slot().mem_gb == 2
    doc = (Path(__file__).parents[2] / "docs" / "setup" / "render-hosts.md").read_text("utf-8")
    assert "4 GB" in doc and "--ram-cap-gb" in doc and "gpu-only" in doc


def test_windows_worker_restart_uses_schtasks_and_tolerates_a_missing_task() -> None:
    restart = next(s for s in _plan().steps if s.id == "storage:restart-worker:legion")
    assert restart.shell == "powershell"
    assert "ScheduledTask" not in restart.command and "Cim" not in restart.command
    assert "schtasks.exe /query /tn spectracut-worker" in restart.command
    assert "if ($has) { schtasks.exe /end" in restart.command
    assert "if ($has) { schtasks.exe /run" in restart.command
    rig = next(s for s in _plan().steps if s.id == "storage:restart-worker:rig")
    assert rig.command == "systemctl --user restart spectracut-worker"


def test_general_host_keeps_all_memory_and_build_lanes() -> None:
    slot = HostEntry(name="rig", cpus=8, mem_gb=32, profile=PROFILE).slot()
    assert slot.mem_gb == 32
    assert slot.build_lanes is True


class _SimulatedWindowsHost:
    """Stand-in storage host: the share exists only once an admin ran the script."""

    def __init__(self) -> None:
        self.script: str | None = None
        self.password: str | None = None
        self.share_ready = False
        self.ran: list[str] = []

    def admin_runs_script(self) -> None:
        assert self.script is not None and self.password is not None
        self.share_ready = True
        self.password = None  # the script deletes the password file

    def probe(self, host: str) -> HostFacts:
        return FACTS[host]

    def run(self, step: Step) -> str | None:
        from spectracut.setup.plan import SetupError

        self.ran.append(step.id)
        if step.id == "storage:admin-script":
            self.script = step.stdin
        elif step.id == "storage:admin-password":
            self.password = step.stdin
        elif step.id == "storage:serve" and not self.share_ready:
            raise SetupError("run once as Administrator, then re-run spectracut setup")
        return ""


def test_windows_storage_flow_blocks_until_admin_ran_then_mounts_and_maps(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import pytest

    from spectracut.setup.plan import SetupError, apply_plan

    host = _SimulatedWindowsHost()
    with pytest.raises(SetupError, match="Administrator"):
        apply_plan(_plan(), host, SetupState(), tmp_path)
    assert host.script == windows_storage_source()
    assert not any(i.startswith("storage:attach") for i in host.ran)

    host.admin_runs_script()
    host.ran.clear()
    state = apply_plan(_plan(), host, SetupState(), tmp_path)
    assert "storage:attach:" + CONTROL in host.ran and "storage:attach:rig" in host.ran
    assert state.storage == TARGET


def test_drive_and_path_options_are_normalized() -> None:
    assert normalize_drive("s") == normalize_drive("S:") == "S:"
    for bad in ("SS", "1:", "", "S:\\"):
        with pytest.raises(StorageError):
            normalize_drive(bad)
    assert normalize_storage_path("d:\\SpectraCut\\flamenco\\") == D_PATH
    assert normalize_storage_path("/data/flamenco/") == "/data/flamenco"
    for bad in ("relative", "D:/x'; rm", "/a/$(b)", ""):
        with pytest.raises(StorageError):
            normalize_storage_path(bad)


def test_windows_storage_path_reaches_admin_script_gate_and_worker_subst() -> None:
    plan = _plan(CUSTOM)
    gate = _step(plan, "storage:serve")
    assert f"Test-Path '{D_PATH}/.spectracut-share'" in gate.command
    # the printed command sits in a '...' literal, so its own quotes are doubled
    assert f"-Path ''{D_PATH}''" in gate.command and "C:/spectracut" not in gate.command
    # storage host: subst of the local folder in the worker task's session, no SMB loopback
    own = json.loads(_step(plan, "worker-args:legion").stdin or "")
    assert own["LocalPath"] == D_PATH and own["Drive"] == "S:" and "Server" not in own
    assert "storage:attach:legion" not in [s.id for s in plan.steps]
    assert "worker-cred:legion" not in [s.id for s in plan.steps]
    worker = worker_script_source()
    assert "subst $Drive" in worker and "New-PSDrive" in worker and "'F:'" not in worker
    # the two-way variable uses the drive
    variables = yaml.safe_load(_step(plan, "storage:variables").stdin or "")
    win = [v for v in variables["variables"]["storage"]["values"] if v["platform"] == "windows"]
    assert win == [{"platform": "windows", "value": "S:/"}]


def test_other_windows_hosts_map_the_unc_share_in_the_task_session() -> None:
    facts = dict(
        FACTS,
        mrdubey=HostFacts(
            "mrdubey",
            "windows",
            "100.64.0.9",
            True,
            200,
            account="MRDUBEY\\artac",
            sid="S-1-5-21-4-5-6-1001",
            home="C:\\Users\\artac",
        ),
    )
    plan = build_plan(
        load_manifest(), "2026.11", facts, ["legion", "mrdubey", "rig"], CUSTOM, CRED, TARGET,
        wheels=WHEELS,
    )  # fmt: skip
    other = json.loads(_step(plan, "worker-args:mrdubey").stdin or "")
    assert other == {
        "Manager": other["Manager"],
        "Drive": "S:",
        "Share": "flamenco",
        "Server": "100.64.0.2",
        "User": CRED.user,
        "Name": "mrdubey",
    }
    assert _step(plan, "worker-cred:mrdubey").stdin == CRED.password + "\n"
    assert not [s for s in plan.steps if "New-SmbMapping" in s.command]


def test_drive_letter_in_use_on_any_windows_host_fails_the_plan() -> None:
    taken = dict(FACTS, legion=replace(FACTS["legion"], drives=("C=", "S=")))
    with pytest.raises(StorageError, match="S: is already used on legion"):
        _plan(CUSTOM, taken)
    other = dict(
        FACTS,
        mrdubey=HostFacts(
            "mrdubey",
            "windows",
            "100.64.0.9",
            True,
            1,
            account="MRDUBEY\\artac",
            sid="S-1-5-21-4-5-6-1001",
            home="C:\\Users\\artac",
        ),
    )
    other["mrdubey"] = replace(other["mrdubey"], drives=("S=\\\\nas\\other",))
    with pytest.raises(StorageError, match="mrdubey"):
        build_plan(
            load_manifest(), "2026.11", other, ["legion", "mrdubey"], CUSTOM, CRED, TARGET,
            wheels=WHEELS,
        )  # fmt: skip
    # already the share (UNC) or, on the storage host, its own subst folder: fine
    ok = dict(FACTS, legion=replace(FACTS["legion"], drives=("S=D:\\SpectraCut\\flamenco",)))
    assert _plan(CUSTOM, ok).steps
    other["mrdubey"] = replace(other["mrdubey"], drives=("S=\\\\100.64.0.2\\flamenco",))
    assert build_plan(
        load_manifest(), "2026.11", other, ["legion", "mrdubey"], CUSTOM, CRED, TARGET,
        wheels=WHEELS,
    ).steps  # fmt: skip
    # the default F: is checked too
    with pytest.raises(StorageError, match="F: is already used"):
        _plan(None, dict(FACTS, legion=replace(FACTS["legion"], drives=("F=",))))


def test_storage_path_must_match_the_storage_host_os() -> None:
    with pytest.raises(StorageError, match="Windows path"):
        _plan(SetupState(storage_path="/data/flamenco"))
    with pytest.raises(StorageError, match="Linux path"):
        _plan(CUSTOM, target=StorageTarget(kind="host", host="rig"))


def test_linux_storage_path_is_served_and_bound_at_srv_flamenco() -> None:
    plan = _plan(
        SetupState(storage_path="/data/flamenco"), target=StorageTarget(kind="host", host="rig")
    )
    assert "path = /data/flamenco\n" in (_step(plan, "storage:smb-conf").stdin or "")
    assert "sudo chown spectracut-share /data/flamenco" in _step(plan, "storage:serve").command
    bind = _step(plan, "storage:bind")
    assert bind.host == "rig" and "mount --bind /data/flamenco /srv/flamenco" in bind.command
    default = _plan(target=StorageTarget(kind="host", host="rig"))
    assert "path = /srv/flamenco\n" in (_step(default, "storage:smb-conf").stdin or "")
    assert "storage:bind" not in [s.id for s in default.steps]


def test_admin_ssh_account_runs_the_storage_script_itself_password_via_file_only() -> None:
    admin = dict(FACTS, legion=replace(FACTS["legion"], admin=True))
    plan = _plan(CUSTOM, admin)
    ids = [s.id for s in plan.steps]
    assert ids.index("storage:admin-script") < ids.index("storage:admin-password")
    assert ids.index("storage:admin-password") < ids.index("storage:serve")
    serve = _step(plan, "storage:serve")
    assert "powershell.exe -NoProfile -ExecutionPolicy Bypass -File $s -PasswordFile $f" in (
        serve.command
    )
    assert f"-Path '{D_PATH}'" in serve.command and serve.stdin is None
    assert "run once as Administrator" not in serve.command
    assert not any(
        "one-time admin step" in n and "storage" in n for n in storage_notes(TARGET, admin)
    )
    for step in plan.steps:
        assert CRED.password not in step.command
    assert _step(plan, "storage:admin-password").stdin == CRED.password + "\n"


def test_admin_script_reports_or_adopts_an_existing_share() -> None:
    src = windows_storage_source()
    check = src.index("$existing = Get-SmbShare -Name $Share")
    assert check < src.index("New-LocalUser") and check < src.index("New-Item -ItemType Directory")
    assert "(Get-NormPath $existing.Path) -ne (Get-NormPath $Path)" in src
    assert "throw (\"SMB share '$Share' already exists at" in src
    assert "Grant-SmbShareAccess -Name $Share -AccountName $User" in src  # adopt on a match
    assert src.index("throw (") < src.index(".spectracut-share")  # no marker on mismatch


def test_path_or_drive_change_replans_storage(tmp_path: Path) -> None:
    host = _SimulatedWindowsHost()
    host.share_ready = True  # an admin already ran the script
    state = apply_plan(_plan(), host, SetupState(), tmp_path)
    assert _plan(state).steps == ()
    moved = apply_plan(
        _plan(state.model_copy(update={"windows_drive": "S:"})), host, state, tmp_path
    )
    assert moved.storage_layout is not None and moved.storage_layout.drive == "S:"
    replan = _plan(moved.model_copy(update={"storage_path": D_PATH}))
    assert "storage:serve" in [s.id for s in replan.steps]
    assert "storage:restart-worker:legion" in [s.id for s in replan.steps]


def test_mount_commands_quote_a_hostile_server_as_one_argument() -> None:
    import shlex

    from spectracut.setup.storage import _attach_steps, _migrate_steps

    evil = "1.2.3.4'; touch INJECTED; echo '"
    cmds = [
        s.command
        for s in (
            *_attach_steps("rig", evil, "flamenco", CRED),
            *_migrate_steps(evil, "flamenco", CRED),
        )
        if "cifs" in s.command
    ]
    assert len(cmds) == 2
    for cmd in cmds:
        assert f"//{evil}/flamenco" in shlex.split(cmd)
        assert "touch" not in [t for t in shlex.split(cmd) if t != f"//{evil}/flamenco"]


def _run_sh(cmd: str, stdin: str, home: Path, cwd: Path) -> None:
    import subprocess

    env = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
    subprocess.run(["sh", "-c", cmd], input=stdin, text=True, cwd=cwd, env=env, check=True)


def test_write_file_step_expands_home_and_writes_without_sudo(tmp_path: Path) -> None:
    home, cwd = tmp_path / "home", tmp_path / "cwd"
    home.mkdir()
    cwd.mkdir()
    rel = ".config/systemd/user/flamenco-manager.service"
    step = write_file_step("control", "linux", "x", "x", f"$HOME/{rel}", "body\n")
    assert "sudo" not in step.command
    _run_sh(step.command, "body\n", home, cwd)
    target = home / rel
    assert target.read_text() == "body\n"
    assert oct(target.stat().st_mode & 0o777) == "0o644"
    assert list(cwd.iterdir()) == []  # no literal "$HOME" tree


def test_write_file_step_absolute_path_writes_atomically_under_umask() -> None:
    step = write_file_step("rig", "linux", "x", "x", "/etc/x.conf", "c", mode="600")
    assert step.command == (
        "d=$(dirname '/etc/x.conf'); sudo mkdir -p \"$d\" && sudo sh -c "
        '\'umask 077; cat > "$1.tmp" && chmod "$2" "$1.tmp" && mv -f "$1.tmp" "$1"\' '
        "sh '/etc/x.conf' '600'"
    )


def _run_with_stubs(cmd: str, stdin: str, tmp_path: Path) -> list[str]:
    """Run ``cmd`` under umask 022 with a pass-through sudo and a chmod that logs the mode."""
    import subprocess

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "chmod.log"
    (bin_dir / "sudo").write_text('#!/bin/sh\nexec "$@"\n')
    (bin_dir / "chmod").write_text(f'#!/bin/sh\nstat -c "%a" "$2" >> {log}\nexec /bin/chmod "$@"\n')
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    env = {"HOME": str(tmp_path / "home"), "PATH": f"{bin_dir}:/usr/bin:/bin"}
    subprocess.run(["sh", "-c", "umask 022; " + cmd], input=stdin, text=True, env=env, check=True)
    return log.read_text().split()


def test_write_file_step_sudo_secret_never_group_or_world_readable(tmp_path: Path) -> None:
    target = tmp_path / "etc" / "x.cred"
    step = write_file_step("rig", "linux", "x", "x", str(target), "secret\n", mode="600")
    before = _run_with_stubs(step.command, "secret\n", tmp_path)
    assert before == ["600"]  # mode already restricted when chmod runs
    assert target.read_text() == "secret\n"
    assert oct(target.stat().st_mode & 0o777) == "0o600"
    assert [p.name for p in target.parent.iterdir()] == ["x.cred"]


def test_write_file_step_sudo_rewrites_loose_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "x.cred"
    target.write_text("old")
    target.chmod(0o644)
    step = write_file_step("rig", "linux", "x", "x", str(target), "new", mode="600")
    _run_with_stubs(step.command, "new", tmp_path)
    assert target.read_text() == "new"
    assert oct(target.stat().st_mode & 0o777) == "0o600"


def test_write_file_step_home_branch_honours_mode_600(tmp_path: Path) -> None:
    home, cwd = tmp_path / "home", tmp_path / "cwd"
    home.mkdir()
    cwd.mkdir()
    step = write_file_step("c", "linux", "x", "x", "$HOME/s/k", "v", mode="600")
    _run_sh(step.command, "v", home, cwd)
    assert oct((home / "s" / "k").stat().st_mode & 0o777) == "0o600"
    assert [p.name for p in (home / "s").iterdir()] == ["k"]


@pytest.mark.parametrize("bad", ["", "9", "60000", "u+r", "600; rm"])
def test_write_file_step_rejects_bad_mode(bad: str) -> None:
    with pytest.raises(ValueError):
        write_file_step("rig", "linux", "x", "x", "/etc/x", "c", mode=bad)


@pytest.mark.parametrize("bad", ['$HOME/a"b', "$HOME/a`b", "$HOME/a$b", "$HOME/", "$X/a", "/a'b"])
def test_write_file_step_rejects_unsafe_paths(bad: str) -> None:
    with pytest.raises(ValueError):
        write_file_step("control", "linux", "x", "x", bad, "c")


def test_no_single_quoted_dollar_path_in_linux_write_steps() -> None:
    import re

    plan = _plan()
    writes = [s for s in plan.steps if s.kind == "write" and s.shell == "sh"]
    ids = {s.id for s in writes}
    assert {"service:manager-unit", "service:worker-unit:rig", "storage:variables"} <= ids
    for s in writes:
        quoted = re.findall(r"'([^']*)'", s.command)  # sequential pairs: real quoted spans
        # the sudo writer's sh -c script legitimately single-quotes its "$1"/"$2" arguments
        quoted = [q for q in quoted if not q.startswith("umask 077;")]
        assert not any("$" in q for q in quoted), s.id


def test_non_admin_storage_gate_runs_only_a_hash_checked_copy() -> None:
    """#190: the printed elevated command verifies windows_storage.ps1 and runs a TEMP copy."""
    plan = _plan(CUSTOM)
    script = _step(plan, "storage:admin-script")
    digest = hashlib.sha256((script.stdin or "").encode("utf-8")).hexdigest()
    assert windows_storage_sha256() == digest
    gate = _step(plan, "storage:serve").command
    assert f"if ($h -ne ''{digest}'')" in gate  # doubled: inside the printed '...' literal
    assert "Join-Path $env:TEMP" in gate and "-File $c" in gate
    assert "-File ' + $s" not in gate and "-File $s" not in gate
    assert ".Replace('__SCRIPT__', $s" in gate and ".Replace('__PW__', $f" in gate
    note = next(n for n in storage_notes(TARGET, FACTS) if "one-time admin step" in n)
    assert f"SHA-256 {digest}" in note
    for step in plan.steps:
        assert CRED.password not in step.command


def test_verified_admin_command_quotes_the_path_and_passes_args() -> None:
    cmd = verified_admin_command("C:\\Users\\o'neil\\x.ps1", "ab" * 32, "-User 'u'")
    assert "ReadAllBytes('C:\\Users\\o''neil\\x.ps1')" in cmd
    assert "-File $c -User 'u' }" in cmd and "finally { Remove-Item -Force $c }" in cmd
    assert "$LASTEXITCODE -ne 0" in cmd
