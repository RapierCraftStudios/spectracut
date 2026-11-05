# SPDX-License-Identifier: AGPL-3.0-or-later
"""Windows storage host (admin-script handoff) and the gpu-only host role."""

from __future__ import annotations

import tempfile
from pathlib import Path

import spectracut
from spectracut.cli.render import HostEntry
from spectracut.queue.profile import RenderProfile
from spectracut.setup.manifest import HostFacts, Step, load_manifest
from spectracut.setup.plan import Plan, SetupState, build_plan
from spectracut.setup.storage import (
    CONTROL,
    Credential,
    StorageTarget,
    storage_notes,
    windows_storage_source,
)
from spectracut.setup.wheels import LocalWheels

CRED = Credential("spectracut-share", "pw-secret-value")
FACTS = {
    CONTROL: HostFacts(CONTROL, "linux", "100.64.0.1", wired=True, free_gb=200),
    "legion": HostFacts("legion", "windows", "100.64.0.2", wired=True, free_gb=500),
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


def _plan() -> Plan:
    return build_plan(
        load_manifest(), "2026.11", FACTS, ["legion", "rig"], SetupState(), CRED, TARGET,
        wheels=WHEELS,
    )  # fmt: skip


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
