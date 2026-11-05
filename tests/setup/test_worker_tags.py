# SPDX-License-Identifier: AGPL-3.0-or-later
"""Setup creates and assigns each registered host's Flamenco worker tags (#158)."""

from __future__ import annotations

import json
import tempfile
from collections.abc import Sequence
from pathlib import Path

import pytest
from typer.testing import CliRunner

import spectracut
from spectracut.cli import setup as setup_cli
from spectracut.cli.app import app
from spectracut.queue.flamenco import Worker, WorkerTag, select_worker_tag, worker_tags
from spectracut.queue.profile import RenderProfile
from spectracut.setup.manifest import HostFacts, Step, load_manifest
from spectracut.setup.plan import (
    WORKER_NAME_ENV,
    SetupError,
    SetupState,
    build_plan,
    worker_name,
    worker_script_source,
)
from spectracut.setup.storage import CONTROL, Credential
from spectracut.setup.wheels import LocalWheels
from spectracut.setup.worker_tags import (
    MANAGED_TAG_DESCRIPTION,
    desired_tags,
    sync_worker_tags,
)

LEGION = RenderProfile(
    blender_version="4.5.15", gpu_model="NVIDIA GeForce RTX 3070 Laptop GPU", vram_gb=8
)
MRDUBEY = RenderProfile(
    blender_version="4.5.15", gpu_model="NVIDIA GeForce RTX 4070 Ti", vram_gb=12
)


class Host:
    def __init__(self, name: str, profile: RenderProfile) -> None:
        self.name = name
        self.profile = profile


class FakeManager:
    """An in-memory Flamenco Manager: tags, workers and their memberships."""

    def __init__(self, workers: Sequence[str] = ("legion", "mrdubey-1")) -> None:
        self.tags: dict[str, WorkerTag] = {}
        self.workers = [Worker(id=f"w-{n}", name=n, status="awake") for n in workers]
        self.members: dict[str, list[str]] = {w.id: [] for w in self.workers}
        self.writes: list[tuple[str, object]] = []
        self.hidden_polls = 0  # list_workers() returns nobody this many times

    def add_tag(self, name: str, description: str = "") -> WorkerTag:
        tag = WorkerTag(id=f"t-{len(self.tags) + 1}-{name}", name=name, description=description)
        self.tags[tag.id] = tag
        return tag

    def list_tags(self) -> list[WorkerTag]:
        return list(self.tags.values())

    def create_tag(self, name: str, description: str = "") -> None:
        self.writes.append(("create", name))
        self.add_tag(name, description)

    def delete_tag(self, tag_id: str) -> None:
        self.writes.append(("delete", self.tags[tag_id].name))
        del self.tags[tag_id]
        for ids in self.members.values():
            if tag_id in ids:
                ids.remove(tag_id)

    def list_workers(self) -> list[Worker]:
        if self.hidden_polls:
            self.hidden_polls -= 1
            return []
        return list(self.workers)

    def worker_tags_of(self, worker_id: str) -> list[WorkerTag]:
        return [self.tags[i] for i in self.members[worker_id]]

    def set_worker_tags(self, worker_id: str, tag_ids: Sequence[str]) -> None:
        assert all(i in self.tags for i in tag_ids)
        self.writes.append(("set", worker_id))
        self.members[worker_id] = list(tag_ids)

    def names(self, worker: str) -> set[str]:
        return {t.name for t in self.worker_tags_of(f"w-{worker}")}


def _desired(legion: RenderProfile = LEGION) -> dict[str, list[str]]:
    return desired_tags([Host("legion", legion), Host("mrdubey-1", MRDUBEY)])


def test_desired_tags_are_what_spectracut_host_tags_prints() -> None:
    from spectracut_host.profile import worker_tags as host_worker_tags

    desired = _desired()
    assert desired["legion"] == ["3070laptopgpu", "vram-8gb", "eevee", "cycles", "workbench"]
    assert desired["mrdubey-1"] == ["4070ti", "vram-12gb", "eevee", "cycles", "workbench"]
    assert desired["legion"] == host_worker_tags(LEGION) == worker_tags(LEGION)


def test_sync_creates_and_assigns_tags_then_a_rerun_writes_nothing() -> None:
    manager = FakeManager()
    actions = sync_worker_tags(manager, _desired(), sleep=lambda _s: None)
    assert manager.names("legion") == set(_desired()["legion"])
    assert manager.names("mrdubey-1") == set(_desired()["mrdubey-1"])
    assert {t.description for t in manager.tags.values()} == {MANAGED_TAG_DESCRIPTION}
    assert len(manager.tags) == 7  # shared capability tags are created once
    assert "create worker tag 3070laptopgpu" in actions
    # A job requiring the legion GPU slug now finds a tag (the #50 failure).
    assert select_worker_tag("3070laptopgpu", manager.list_tags()).name == "3070laptopgpu"

    manager.writes.clear()
    assert sync_worker_tags(manager, _desired(), sleep=lambda _s: None) == []
    assert manager.writes == []


def test_sync_waits_for_the_worker_to_register() -> None:
    manager = FakeManager()
    manager.hidden_polls = 2
    slept: list[float] = []
    sync_worker_tags(manager, _desired(), sleep=slept.append, clock=lambda: 0.0)
    assert len(slept) == 2
    assert manager.names("legion")


def test_sync_times_out_naming_the_host_without_a_worker() -> None:
    manager = FakeManager(workers=("legion-rtx3070", "mrdubey-1"))  # legion not renamed yet
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    with pytest.raises(SetupError, match=r"legion \(worker name 'legion'\)"):
        sync_worker_tags(manager, _desired(), timeout=30, sleep=sleep, clock=lambda: now[0])
    assert manager.writes == []  # nothing is changed before every worker is there


def test_gpu_change_replaces_tags_and_prunes_the_stale_managed_tag() -> None:
    manager = FakeManager()
    sync_worker_tags(manager, _desired(), sleep=lambda _s: None)
    upgraded = RenderProfile(blender_version="4.5.15", gpu_model="RTX 4090", vram_gb=24)
    actions = sync_worker_tags(manager, _desired(upgraded), sleep=lambda _s: None)
    assert manager.names("legion") == {"4090", "vram-24gb", "eevee", "cycles", "workbench"}
    names = {t.name for t in manager.tags.values()}
    assert "3070laptopgpu" not in names and "vram-8gb" not in names
    assert "delete stale worker tag 3070laptopgpu" in actions
    assert manager.names("mrdubey-1") == set(_desired()["mrdubey-1"])


def test_capability_removed_from_desired_is_dropped_from_the_worker() -> None:
    manager = FakeManager()
    sync_worker_tags(manager, _desired(), sleep=lambda _s: None)
    fewer = {"legion": ["3070laptopgpu", "cycles"], "mrdubey-1": ["4070ti", "cycles"]}
    sync_worker_tags(manager, fewer, sleep=lambda _s: None)
    assert manager.names("legion") == {"3070laptopgpu", "cycles"}
    assert {t.name for t in manager.tags.values()} == {"3070laptopgpu", "4070ti", "cycles"}


def test_unmanaged_tags_and_unregistered_workers_are_left_alone() -> None:
    manager = FakeManager(workers=("legion", "mrdubey-1", "archlinux-cpu"))
    by_hand = manager.add_tag("night-shift", "added in the Manager UI")
    manager.members["w-legion"] = [by_hand.id]
    manager.members["w-archlinux-cpu"] = [by_hand.id]
    existing = manager.add_tag("4070TI")  # same tag spelled differently, unmanaged
    sync_worker_tags(manager, _desired(), sleep=lambda _s: None)
    assert "night-shift" in manager.names("legion")
    assert manager.members["w-archlinux-cpu"] == [by_hand.id]
    assert existing.id in manager.members["w-mrdubey-1"]  # reused, not duplicated
    assert not any(t.name == "4070ti" for t in manager.tags.values())
    sync_worker_tags(manager, {"legion": ["cycles"]}, sleep=lambda _s: None)
    assert by_hand.id in manager.tags and existing.id in manager.tags  # never pruned


def _no_sleep(seconds: float) -> None:
    pytest.fail(f"a dry run waited {seconds}s")


def test_dry_run_reads_only_and_reports() -> None:
    manager = FakeManager(workers=("mrdubey-1",))
    actions = sync_worker_tags(manager, _desired(), dry_run=True, sleep=_no_sleep)
    assert manager.writes == [] and manager.tags == {}
    assert "legion: no worker named 'legion' registered yet" in actions
    assert any(a.startswith("mrdubey-1: set worker mrdubey-1 tags to 4070ti") for a in actions)


# ---- setup names the worker after the registered host ------------------------------

CRED = Credential("spectracut-share", "pw")
FACTS = {
    CONTROL: HostFacts(CONTROL, "linux", "100.64.0.1", wired=True, free_gb=900),
    "legion": HostFacts("legion", "windows", "100.64.0.2", wired=True, free_gb=500),
    "rig": HostFacts("rig", "linux", "100.64.0.3", wired=True, free_gb=100),
}


def _wheels() -> LocalWheels:
    d = Path(tempfile.mkdtemp(prefix="sc-wheels-"))
    v = spectracut.__version__
    core, host = d / f"spectracut-{v}-py3-none-any.whl", d / f"spectracut_host-{v}-py3-none-any.whl"
    core.write_bytes(b"core")
    host.write_bytes(b"host")
    return LocalWheels.from_files(v, core, host)


WHEELS = _wheels()


def _plan(state: SetupState):  # type: ignore[no-untyped-def]
    return build_plan(
        load_manifest(), "2026.11", FACTS, ["legion", "rig"], state, CRED, wheels=WHEELS
    )


def test_setup_names_each_worker_after_its_registered_host() -> None:
    steps = {s.id: s for s in _plan(SetupState()).steps}
    unit = steps["service:worker-unit:rig"].stdin or ""
    assert f"Environment={WORKER_NAME_ENV}=rig\n" in unit
    assert worker_name("rig") == "rig"
    assert steps["service:worker:rig"].version.endswith("#name")  # old units are rewritten
    args = steps["worker-args:legion"].stdin or ""
    assert json.loads(args)["Name"] == "legion"
    src = worker_script_source()
    assert "$cfg.Name" in src and "$env:FLAMENCO_WORKER_NAME = $Name" in src


class FakeRunner:
    def __init__(self) -> None:
        self.ran: list[Step] = []

    def probe(self, host: str) -> HostFacts:
        return FACTS[host]

    def run(self, step: Step) -> str | None:
        self.ran.append(step)
        return None


def test_cli_setup_syncs_tags_even_when_hosts_are_converged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SPECTRACUT_HOME", str(home))
    runner = FakeRunner()
    manager = FakeManager(workers=("legion", "rig"))
    urls: list[str] = []

    def client(url: str) -> FakeManager:
        urls.append(url)
        return manager

    monkeypatch.setattr(setup_cli, "CommandRunner", lambda: runner)
    monkeypatch.setattr(setup_cli, "build_wheels", lambda _out: WHEELS)
    monkeypatch.setattr(setup_cli, "manager_client", client)
    hosts = [
        {"name": "legion", "cpus": 8, "mem_gb": 32, "profile": LEGION.model_dump()},
        {"name": "rig", "cpus": 8, "mem_gb": 32, "profile": MRDUBEY.model_dump()},
    ]
    (home / "hosts.json").write_text(json.dumps({"hosts": hosts}), encoding="utf-8")
    cli = CliRunner()

    dry = cli.invoke(app, ["setup", "--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert manager.writes == [] and runner.ran == []
    assert "legion: set worker legion tags to 3070laptopgpu" in dry.output

    done = cli.invoke(app, ["setup"])
    assert done.exit_code == 0, done.output
    assert urls[-1] == "http://100.64.0.1:8085"
    assert manager.names("legion") == set(worker_tags(LEGION))
    assert manager.names("rig") == set(worker_tags(MRDUBEY))

    # Converged hosts: the plan is empty, but tags lost in the Manager are put back.
    for tag in list(manager.tags):
        manager.delete_tag(tag)
    again = cli.invoke(app, ["setup"])
    assert again.exit_code == 0 and "nothing to do" in again.output
    assert manager.names("legion") == set(worker_tags(LEGION))


def test_cli_setup_fails_when_a_worker_never_registers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SPECTRACUT_HOME", str(home))
    monkeypatch.setattr(setup_cli, "CommandRunner", FakeRunner)
    monkeypatch.setattr(setup_cli, "build_wheels", lambda _out: WHEELS)
    monkeypatch.setattr(setup_cli, "manager_client", lambda _url: FakeManager(workers=()))
    monkeypatch.setattr("spectracut.setup.worker_tags.REGISTER_TIMEOUT", 0.0)
    hosts = [{"name": "rig", "cpus": 8, "mem_gb": 32, "profile": MRDUBEY.model_dump()}]
    (home / "hosts.json").write_text(json.dumps({"hosts": hosts}), encoding="utf-8")
    result = CliRunner().invoke(app, ["setup"])
    assert result.exit_code == 2
    assert "no Flamenco worker registered for rig" in result.output
