# SPDX-License-Identifier: AGPL-3.0-or-later
"""spectracut-host from wheels built on the control plane (#132), not from PyPI.

Builds real wheels with ``uv build`` (the repo, and a tiny workspace whose code a
test changes), plans against them, and checks the copy steps. The opt-in
``SPECTRACUT_E2E_LOCAL_WHEELS=1`` test really installs spectracut-host from the
built wheels into a throwaway home through the setup runner.
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

import spectracut
from spectracut.setup.manifest import HostFacts, load_manifest
from spectracut.setup.plan import (
    CommandRunner,
    HostState,
    SetupError,
    SetupState,
    build_plan,
)
from spectracut.setup.storage import CONTROL, Credential, StorageTarget
from spectracut.setup.wheels import (
    REPO_ENV,
    LocalWheels,
    build_wheels,
    remote_wheel_dir,
    wheel_copy_steps,
)

FACTS = {
    CONTROL: HostFacts(CONTROL, "linux", "100.64.0.1", wired=True, free_gb=200),
    "legion": HostFacts("legion", "windows", "100.64.0.2", wired=True, free_gb=500),
    "rig": HostFacts("rig", "linux", "100.64.0.3", wired=True, free_gb=900),
}
WORKERS = ["legion", "rig"]
CRED = Credential("spectracut-share", "pw-secret-value")
NAS = StorageTarget(kind="nas", host="nas", share="flamenco")


def _workspace(root: Path, host_version: str | None = None) -> Path:
    """A minimal uv workspace shaped like the repo: spectracut plus host/spectracut-host."""
    v = spectracut.__version__
    build = '[build-system]\nrequires = ["hatchling>=1.27"]\nbuild-backend = "hatchling.build"\n'
    (root / "src" / "spectracut").mkdir(parents=True)
    (root / "src" / "spectracut" / "__init__.py").write_text("X = 1\n")
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "spectracut"\nversion = "{v}"\n{build}'
        '[tool.hatch.build.targets.wheel]\npackages = ["src/spectracut"]\n'
        '[tool.uv.workspace]\nmembers = ["host"]\n'
    )
    (root / "host" / "spectracut_host").mkdir(parents=True)
    (root / "host" / "spectracut_host" / "__init__.py").write_text("")
    (root / "host" / "bpy_runtime").mkdir()
    (root / "host" / "bpy_runtime" / "__init__.py").write_text("")
    (root / "host" / "bpy_runtime" / "tools.py").write_text('TOOL_NAMES = frozenset(["a"])\n')
    (root / "host" / "pyproject.toml").write_text(
        f'[project]\nname = "spectracut-host"\nversion = "{host_version or v}"\n{build}'
        '[tool.hatch.build.targets.wheel]\npackages = ["spectracut_host", "bpy_runtime"]\n'
    )
    return root


def _plan(state: SetupState, wheels: LocalWheels):  # type: ignore[no-untyped-def]
    return build_plan(load_manifest(), "2026.11", FACTS, WORKERS, state, CRED, NAS, wheels=wheels)


def _converged(wheels: LocalWheels) -> SetupState:
    steps = _plan(SetupState(), wheels).steps
    state = SetupState(set_id="2026.11", storage=NAS)
    for s in steps:
        if s.component and s.version:
            state.hosts.setdefault(s.host, HostState()).components[s.component] = s.version
    return state


def test_repo_wheels_match_the_control_plane_version_and_rebuild_identically(
    tmp_path: Path,
) -> None:
    first = build_wheels(tmp_path / "wheels")
    assert first.version == spectracut.__version__
    assert first.spectracut.name.startswith(f"spectracut-{first.version}-")
    assert first.host.name.startswith(f"spectracut_host-{first.version}-")
    assert first.spectracut.parent == tmp_path / "wheels" / first.digest
    again = build_wheels(tmp_path / "wheels")
    assert again == first  # same code, same digest, same staged files
    with zipfile.ZipFile(first.host) as whl:
        assert "bpy_runtime/tools.py" in whl.namelist()  # #161: hosts need the runtime


def test_changing_only_the_bpy_runtime_reinstalls_spectracut_host_on_every_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace(tmp_path / "src-tree")
    monkeypatch.setenv(REPO_ENV, str(root))
    before = build_wheels(tmp_path / "wheels")
    with zipfile.ZipFile(before.host) as whl:
        assert "bpy_runtime/tools.py" in whl.namelist()
    state = _converged(before)
    assert not [s.id for s in _plan(state, before).steps if "spectracut-host" in s.id]

    tools = root / "host" / "bpy_runtime" / "tools.py"
    tools.write_text('TOOL_NAMES = frozenset(["a", "b"])\n')
    after = build_wheels(tmp_path / "wheels")
    assert after.pin != before.pin and after.version == before.version
    ids = [s.id for s in _plan(state, after).steps]
    for host in WORKERS:
        assert f"install:spectracut-host:{host}" in ids
        assert f"wheel:{after.host.name}:{host}" in ids
    assert "install:spectracut-host:control" not in ids


def test_changing_the_code_changes_the_digest_and_reinstalls_on_every_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _workspace(tmp_path / "src-tree")
    monkeypatch.setenv(REPO_ENV, str(root))
    before = build_wheels(tmp_path / "wheels")
    state = _converged(before)
    assert state.hosts["rig"].components["spectracut-host"] == before.pin
    assert not [s.id for s in _plan(state, before).steps if "spectracut-host" in s.id]

    (root / "src" / "spectracut" / "__init__.py").write_text("X = 2\n")
    after = build_wheels(tmp_path / "wheels")
    assert after.version == before.version and after.digest != before.digest
    ids = [s.id for s in _plan(state, after).steps]
    for host in WORKERS:
        assert f"install:spectracut-host:{host}" in ids
        assert f"host:profile:{host}" in ids  # the profile is re-read after a reinstall
        assert f"wheel:{after.host.name}:{host}" in ids
    assert "install:spectracut-host:control" not in ids


def test_wheels_whose_version_differs_from_the_control_plane_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(REPO_ENV, str(_workspace(tmp_path / "src-tree", host_version="9.9.9")))
    with pytest.raises(SetupError, match="does not match the control plane's version"):
        build_wheels(tmp_path / "wheels")


def test_no_source_tree_is_a_clear_setup_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(REPO_ENV, str(tmp_path))
    with pytest.raises(SetupError, match=REPO_ENV):
        build_wheels(tmp_path / "wheels")


@pytest.fixture
def fake(tmp_path: Path) -> LocalWheels:
    v = spectracut.__version__
    core = tmp_path / f"spectracut-{v}-py3-none-any.whl"
    host = tmp_path / f"spectracut_host-{v}-py3-none-any.whl"
    core.write_bytes(bytes(range(256)) * 4)
    host.write_bytes(b"PK\x03\x04 host wheel \x00\xff")
    return LocalWheels.from_files(v, core, host)


def test_copy_steps_carry_wheel_bytes_on_stdin_only(fake: LocalWheels) -> None:
    for name in WORKERS:
        steps = wheel_copy_steps(FACTS[name], fake)
        assert [s.id for s in steps] == [f"wheel:{w.name}:{name}" for w in fake.files]
        for step, wheel in zip(steps, fake.files, strict=True):
            assert step.stdin is not None
            assert base64.b64decode(step.stdin) == wheel.read_bytes()
            assert step.stdin not in step.command
            assert remote_wheel_dir(FACTS[name].os, fake.digest).split("/")[-1] in step.command


def test_windows_steps_need_no_admin_and_linux_steps_no_sudo(fake: LocalWheels) -> None:
    steps = [
        s for s in _plan(SetupState(), fake).steps if "spectracut-host" in s.id or "wheel:" in s.id
    ]
    win = [s for s in steps if s.host == "legion"]
    linux = [s for s in steps if s.host == "rig"]
    assert win and linux
    for s in win:
        assert s.shell == "powershell"
        assert "$env:USERPROFILE" in s.command
        assert not any(w in s.command for w in ("RunAs", "-Verb", "Administrator", "C:/Program"))
    for s in linux:
        assert s.shell == "sh" and "sudo" not in s.command
        assert "$HOME/" in s.command


def test_install_step_uses_the_copied_wheels_and_checks_the_version(fake: LocalWheels) -> None:
    by_id = {s.id: s for s in _plan(SetupState(), fake).steps}
    for name in WORKERS:
        os_name = FACTS[name].os
        d = remote_wheel_dir(os_name, fake.digest)
        step = by_id[f"install:spectracut-host:{name}"]
        assert step.version == fake.pin == f"{spectracut.__version__}+{fake.digest}"
        assert f'--find-links "{d}"' in step.command
        assert f'--with "{d}/{fake.spectracut.name}"' in step.command
        assert f'"{d}/{fake.host.name}"' in step.command
        assert "spectracut-host==" not in step.command
        assert f"'{spectracut.__version__}'" in step.command or (
            f'"{spectracut.__version__}"' in step.command
        )
        ids = list(by_id)
        copies = [ids.index(f"wheel:{w.name}:{name}") for w in fake.files]
        assert max(copies) < ids.index(step.id)


@pytest.mark.skipif(
    os.environ.get("SPECTRACUT_E2E_LOCAL_WHEELS") != "1" or shutil.which("uv") is None,
    reason="set SPECTRACUT_E2E_LOCAL_WHEELS=1 for a real uv tool install",
)
def test_real_install_from_built_wheels_reports_the_control_plane_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uv = shutil.which("uv")
    assert uv is not None
    keep = {
        k: subprocess.run([uv, *a], capture_output=True, text=True, check=True).stdout.strip()
        for k, a in (
            ("UV_CACHE_DIR", ["cache", "dir"]),
            ("UV_PYTHON_INSTALL_DIR", ["python", "dir"]),
        )
    }
    wheels = build_wheels(tmp_path / "control" / "wheels")
    home = tmp_path / "host-home"
    (home / ".local" / "bin").mkdir(parents=True)
    (home / ".local" / "bin" / "uv").symlink_to(uv)
    for k, v in keep.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("HOME", str(home))
    for k in ("UV_TOOL_DIR", "UV_TOOL_BIN_DIR", "VIRTUAL_ENV", "XDG_BIN_HOME", "XDG_DATA_HOME"):
        monkeypatch.delenv(k, raising=False)
    facts = HostFacts(CONTROL, "linux", "127.0.0.1", wired=True, free_gb=10)
    steps = [s for s in _plan(SetupState(), wheels).steps if s.host == "rig"]
    steps = [s for s in steps if s.id.startswith(("wheel:", "install:spectracut-host:"))]
    runner = CommandRunner(timeout=900)
    for step in steps:  # the real steps, run locally instead of over SSH
        runner.run(step.__class__(**{**step.__dict__, "host": facts.name}))
    staged = home / ".cache" / "spectracut" / "wheels" / wheels.digest
    assert sorted(p.name for p in staged.iterdir()) == sorted(w.name for w in wheels.files)
    out = subprocess.run(
        [str(home / ".local" / "bin" / "spectracut-host"), "--version"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == spectracut.__version__
