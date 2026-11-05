# SPDX-License-Identifier: AGPL-3.0-or-later
"""Setup installs the custom Flamenco job types and checks the Manager lists them (#160)."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from test_plan import (  # type: ignore[import-not-found]
    CRED,
    FACTS,
    WHEELS,
    WORKERS,
    FakeRunner,
    StubManager,
)
from typer.testing import CliRunner

from spectracut.cli import setup as setup_cli
from spectracut.cli.app import app
from spectracut.queue.flamenco import (
    JOB_TYPE,
    FlamencoError,
    job_type_names,
    job_type_script,
    job_type_sources,
)
from spectracut.setup.manifest import load_manifest
from spectracut.setup.plan import (
    JOBTYPES_PSEUDO,
    HostState,
    SetupError,
    SetupState,
    build_plan,
    job_types_step,
    job_types_version,
)
from spectracut.setup.storage import CONTROL

SYSTEMCTL = """#!/bin/sh
echo "$*" >> "$SYSTEMCTL_LOG"
if [ "$3" = "show" ] || [ "$2" = "show" ]; then printf '%s\\n' "$MANAGER_WD"; fi
"""


def _plan(state: SetupState):  # type: ignore[no-untyped-def]
    return build_plan(load_manifest(), "2026.11", FACTS, WORKERS, state, CRED, wheels=WHEELS)


def test_every_packaged_job_type_is_a_source() -> None:
    sources = job_type_sources()
    folder = job_type_script().parent
    assert set(sources) == {p.name for p in folder.glob("*.js")}
    assert sources[job_type_script().name] == job_type_script().read_text(encoding="utf-8")
    assert JOB_TYPE in job_type_names()


def test_plan_installs_job_types_on_the_control_plane_once() -> None:
    plan = _plan(SetupState())
    steps = [s for s in plan.steps if s.component == JOBTYPES_PSEUDO]
    assert len(steps) == 1
    step = steps[0]
    assert step.host == CONTROL
    assert step.version == job_types_version(job_type_sources())
    assert "sudo" not in step.command
    assert "WorkingDirectory" in step.command and "$HOME/.local/opt/flamenco" in step.command
    # After the Manager is set up, so the restart hits a running service.
    ids = [s.id for s in plan.steps]
    assert ids.index(step.id) > ids.index("service:manager")

    converged = SetupState(hosts={CONTROL: HostState(components={JOBTYPES_PSEUDO: step.version})})
    assert not [s for s in _plan(converged).steps if s.component == JOBTYPES_PSEUDO]


def test_a_changed_script_gives_a_new_version() -> None:
    sources = job_type_sources()
    changed = {**sources, JOB_TYPE + ".js": sources[JOB_TYPE + ".js"] + "\n// v2\n"}
    assert job_types_version(changed) != job_types_version(sources)
    stale = SetupState(hosts={CONTROL: HostState(components={JOBTYPES_PSEUDO: "old"})})
    assert [s.id for s in _plan(stale).steps if s.component == JOBTYPES_PSEUDO]


def _run(step_cmd: str, home: Path, wd: str) -> list[str]:
    bindir = home / "bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "systemctl"
    stub.write_text(SYSTEMCTL, encoding="utf-8")
    stub.chmod(0o755)
    log = home / "systemctl.log"
    log.write_text("", encoding="utf-8")
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "SYSTEMCTL_LOG": str(log),
        "MANAGER_WD": wd,
    }
    subprocess.run(["sh", "-c", step_cmd], env=env, check=True, capture_output=True)
    return log.read_text(encoding="utf-8").splitlines()


@pytest.mark.parametrize("hand_installed", [True, False])
def test_step_installs_files_and_restarts_only_when_changed(
    tmp_path: Path, hand_installed: bool
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    manager = tmp_path / "flamenco-3.9.3-linux-amd64"
    wd = str(manager) if hand_installed else ""
    folder = (manager if hand_installed else home / ".local/opt/flamenco") / "scripts"
    sources = job_type_sources()
    step = job_types_step(CONTROL, sources)

    calls = _run(step.command, home, wd)
    for name, content in sources.items():
        assert (folder / name).read_text(encoding="utf-8") == content
    assert "--user restart flamenco-manager" in calls
    assert sorted(p.name for p in folder.iterdir()) == sorted(sources)  # no temp files left

    # Same files again: nothing replaced, no restart (idempotent).
    calls = _run(step.command, home, wd)
    assert not [c for c in calls if "restart" in c]

    # A changed script is replaced and restarts the Manager.
    name = next(iter(sources))
    (folder / name).write_text("stale", encoding="utf-8")
    calls = _run(step.command, home, wd)
    assert (folder / name).read_text(encoding="utf-8") == sources[name]
    assert "--user restart flamenco-manager" in calls


class _Manager:
    def __init__(self, answers: list[list[str] | Exception]) -> None:
        self.answers = answers
        self.calls = 0

    def job_types(self) -> list[str]:
        answer = self.answers[min(self.calls, len(self.answers) - 1)]
        self.calls += 1
        if isinstance(answer, Exception):
            raise answer
        return answer


def _check(
    monkeypatch: pytest.MonkeyPatch, manager: _Manager, dry_run: bool = False
) -> list[float]:
    monkeypatch.setattr(setup_cli, "manager_client", lambda _url: manager)
    now = [0.0]
    slept: list[float] = []

    def sleep(s: float) -> None:
        slept.append(s)
        now[0] += s

    setup_cli._check_job_types(
        "http://m:8085", dry_run, timeout=10.0, sleep=sleep, clock=lambda: now[0]
    )
    return slept


def test_check_passes_when_listed_and_waits_through_a_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _check(monkeypatch, _Manager([["echo-sleep-test", JOB_TYPE]])) == []
    restarting = _Manager([FlamencoError("GET /api/v3/jobs/types failed: refused"), [JOB_TYPE]])
    assert _check(monkeypatch, restarting)
    assert restarting.calls == 2


def test_check_fails_clearly_when_the_manager_lacks_a_job_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(SetupError, match=f"does not list job type {JOB_TYPE}.*scripts/"):
        _check(monkeypatch, _Manager([["echo-sleep-test"]]))


def test_dry_run_check_only_warns(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    manager = _Manager([["echo-sleep-test"]])
    assert _check(monkeypatch, manager, dry_run=True) == []
    assert manager.calls == 1
    assert f"warning: Flamenco Manager does not list job type {JOB_TYPE}" in capsys.readouterr().out


class _NoJobTypes(StubManager):  # type: ignore[misc]
    def job_types(self) -> list[str]:
        return ["echo-sleep-test"]


def test_cli_setup_fails_when_the_manager_never_lists_the_job_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SPECTRACUT_HOME", str(home))
    monkeypatch.setattr(setup_cli, "CommandRunner", FakeRunner)
    monkeypatch.setattr(setup_cli, "build_wheels", lambda _out: WHEELS)
    monkeypatch.setattr(setup_cli, "manager_client", lambda _url: _NoJobTypes())
    monkeypatch.setattr(setup_cli, "JOB_TYPES_TIMEOUT", 0.0)
    (home / "hosts.json").write_text(json.dumps({"hosts": []}), encoding="utf-8")
    result = CliRunner().invoke(app, ["setup"])
    assert result.exit_code == 2
    assert f"Flamenco Manager does not list job type {JOB_TYPE}" in result.output
