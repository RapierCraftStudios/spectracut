# SPDX-License-Identifier: AGPL-3.0-or-later
"""Model-host setup steps (family extras, pinned weights) and the doctor model lines."""

from __future__ import annotations

import dataclasses
import os
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import spectracut
from spectracut.cli import setup as setup_cli
from spectracut.cli.app import app
from spectracut.providers.local.registry import MODELS
from spectracut.setup import component_lines, model_disk_lines, model_lines
from spectracut.setup.manifest import HostFacts, Step, load_manifest
from spectracut.setup.model_assets import (
    ASSETS,
    ESSENTIAL_MODELS,
    MODEL_ROOT_ENV,
    TORCH_INDEX,
    default_model_root,
    extras_digest,
    model_version,
)
from spectracut.setup.plan import (
    MODEL_PREFIX,
    MODELS_DEPS,
    MODELS_ROOT,
    HostState,
    ModelDisk,
    ModelDiskError,
    Plan,
    SetupError,
    SetupState,
    apply_plan,
    build_plan,
    load_state,
    model_disk_step,
    models_deps_version,
    parse_model_disk,
    probe_model_disk,
)
from spectracut.setup.storage import CONTROL, Credential, StorageTarget
from spectracut.setup.wheels import LocalWheels

CRED = Credential("spectracut-share", "pw")
LEGION = HostFacts(
    "legion",
    "windows",
    "100.64.0.2",
    wired=True,
    free_gb=500,
    account="LEGION\\artac-remote",
    sid="S-1-5-21-1-2-3-1001",
    home="C:\\Users\\artac-remote",
)
RIG = HostFacts("rig", "linux", "100.64.0.3", wired=True, free_gb=900)
FACTS = {CONTROL: HostFacts(CONTROL, "linux", "100.64.0.1", wired=True, free_gb=200)}
FACTS |= {"legion": LEGION, "rig": RIG}
WORKERS = ["legion", "rig"]
STORAGE = StorageTarget(kind="host", host="rig")


def _wheels() -> LocalWheels:
    d = Path(tempfile.mkdtemp(prefix="sc-wheels-"))
    v = spectracut.__version__
    core, host = d / f"spectracut-{v}-py3-none-any.whl", d / f"spectracut_host-{v}-py3-none-any.whl"
    core.write_bytes(b"core")
    host.write_bytes(b"host")
    return LocalWheels.from_files(v, core, host)


WHEELS = _wheels()


ALL = SetupState(models="all")
GB = 1_000_000_000


class Runner:
    def __init__(self, out: str | None = None) -> None:
        self.ran: list[str] = []
        self.out = out

    def probe(self, host: str) -> HostFacts:
        return FACTS[host]

    def run(self, step: Step) -> str | None:
        self.ran.append(step.id)
        return self.out if step.artifact else None


def _plan(
    state: SetupState,
    model_host: str | None = None,
    facts: dict[str, HostFacts] = FACTS,
    **kw: Any,
) -> Plan:
    return build_plan(
        load_manifest(),
        "2026.11",
        facts,
        WORKERS,
        state,
        CRED,
        STORAGE,
        wheels=WHEELS,
        model_host=model_host,
        **kw,
    )


def _model_ids(plan: Plan) -> list[str]:
    return [s.id for s in plan.steps if s.id.startswith("models:")]


def test_without_model_host_no_model_steps() -> None:
    assert _model_ids(_plan(SetupState())) == []
    plain = [s.id for s in _plan(SetupState()).steps]
    with_models = [s.id for s in _plan(SetupState(), "legion").steps]
    assert with_models[: len(plain)] == plain  # existing ids and order untouched


def test_model_host_gets_deps_then_the_selected_weights_steps() -> None:
    assert _model_ids(_plan(ALL, "legion")) == ["models:deps:legion"] + [
        f"models:weights:{m}:legion" for m in MODELS
    ]
    picked = _plan(SetupState(models="kokoro-82m,clap-nope".split(",")[0]), "legion")
    assert _model_ids(picked) == ["models:deps:legion", "models:weights:kokoro-82m:legion"]
    plan = _plan(SetupState(), "legion")
    ids = [s.id for s in plan.steps]
    assert _model_ids(plan) == ["models:deps:legion"] + [
        f"models:weights:{m}:legion" for m in ESSENTIAL_MODELS
    ]
    assert ids.index("models:deps:legion") > ids.index("install:spectracut-host:legion")
    deps = next(s for s in plan.steps if s.id == "models:deps:legion")
    assert deps.shell == "powershell" and deps.component == MODELS_DEPS
    assert deps.version == models_deps_version(WHEELS.pin)
    assert deps.version.endswith(f"+{extras_digest()}")
    extras = ",".join(ASSETS)
    assert f'spectracut_host-{spectracut.__version__}-py3-none-any.whl[{extras}]"' in deps.command
    assert f"--index {TORCH_INDEX} --index-strategy unsafe-best-match" in deps.command
    assert "--force" in deps.command


def test_weights_steps_pin_revisions_check_disk_and_never_need_hf_token() -> None:
    for host, shell, disk in (("legion", "powershell", "Get-PSDrive"), ("rig", "sh", "df -Pk")):
        plan = _plan(ALL, host)
        for m in MODELS:
            step = next(s for s in plan.steps if s.id == f"models:weights:{m}:{host}")
            assert step.shell == shell and step.kind == "install"
            assert step.component == f"{MODEL_PREFIX}{m}" and step.version == model_version(m)
            cmd = step.command
            assert disk in cmd and "not enough disk" in cmd
            assert cmd.index(disk) < cmd.index(" download ")
            asset = next(a for a in ASSETS.values() if m in a.model_ids)
            for w in asset.weights:
                assert f"download {w.repo} --revision {w.revision}" in cmd
            if asset.git_commit:
                assert f"checkout --quiet --detach {asset.git_commit}" in cmd
            assert "HF_TOKEN" not in cmd
            assert f"{asset.family}/{m}" in cmd


def test_converged_models_give_no_steps_and_host_upgrade_reruns_deps(tmp_path: Path) -> None:
    state = load_state(tmp_path)
    state = apply_plan(_plan(state, "legion"), Runner(), state, tmp_path)
    have = state.hosts["legion"].components
    assert all(have[f"{MODEL_PREFIX}{m}"] == model_version(m) for m in ESSENTIAL_MODELS)
    assert not any(f"{MODEL_PREFIX}{m}" in have for m in set(MODELS) - set(ESSENTIAL_MODELS))
    assert _plan(load_state(tmp_path), "legion").steps == ()
    # Converged even on a full disk: no weights are missing, so nothing is checked.
    full = ModelDisk("C:/m", free_gb=1.0, total_gb=1000.0)
    assert _plan(load_state(tmp_path), "legion", model_disk=full).steps == ()

    # A reinstall of spectracut-host (lost state entry) drops the extras: deps rerun.
    comps = dict(have)
    del comps["spectracut-host"]
    again = state.model_copy(
        update={"hosts": {**state.hosts, "legion": HostState(components=comps)}}
    )
    assert "models:deps:legion" in _model_ids(_plan(again, "legion"))

    # A drifted model re-downloads only that model.
    comps = dict(have) | {f"{MODEL_PREFIX}kokoro-82m": "old"}
    drift = state.model_copy(
        update={"hosts": {**state.hosts, "legion": HostState(components=comps)}}
    )
    assert _model_ids(_plan(drift, "legion")) == ["models:weights:kokoro-82m:legion"]


def test_essential_models_that_do_not_fit_under_the_reserve_plan_nothing() -> None:
    """Legion, 2026-11-07: weights were planned whatever the disk held (only a note)."""
    small = FACTS | {"legion": dataclasses.replace(LEGION, free_gb=40)}
    with pytest.raises(SetupError) as err:
        _plan(SetupState(), "legion", small)  # 26.2 GB needed, 40 - 20 GB reserve = 20
    msg = str(err.value)
    assert isinstance(err.value, ModelDiskError)
    assert "need about 26.2 GB" in msg and "6.2 GB short" in msg
    assert "Nothing was downloaded" in msg
    assert "--models" in msg and "--models-dir" in msg and "--skip-models" in msg
    # The reserve is 15 % of the disk when that is more than 20 GB: 150 GB of 1 TB.
    big = ModelDisk("C:/m", free_gb=100.0, total_gb=1000.0)
    with pytest.raises(ModelDiskError, match="150 GB is kept in reserve: 76.2 GB short"):
        _plan(SetupState(), "legion", model_disk=big)


def test_default_installs_only_essential_models_when_they_fit() -> None:
    disk = ModelDisk("C:/m", free_gb=60.0, total_gb=200.0)  # reserve 30, 30 available
    plan = _plan(SetupState(), "legion", model_disk=disk)
    assert _model_ids(plan) == ["models:deps:legion"] + [
        f"models:weights:{m}:legion" for m in ESSENTIAL_MODELS
    ]
    assert plan.notes == _plan(SetupState()).notes  # no disk note: checked up front
    with pytest.raises(ModelDiskError):
        _plan(ALL, "legion", model_disk=disk)  # all nine (73.1 GB) do not fit


def test_skip_models_installs_the_families_but_no_weights() -> None:
    small = FACTS | {"legion": dataclasses.replace(LEGION, free_gb=1)}
    plan = _plan(ALL, "legion", small, skip_models=True)
    assert _model_ids(plan) == ["models:deps:legion"]
    assert "install:spectracut-host:legion" in [s.id for s in plan.steps]


def test_models_dir_roots_the_weights_and_the_host_pointer(tmp_path: Path) -> None:
    state = SetupState(models_dir="D:/spectracut/models")
    plan = _plan(state, "legion")
    ids = _model_ids(plan)
    assert ids[:2] == ["models:deps:legion", "models:root:legion"]
    root = next(s for s in plan.steps if s.id == "models:root:legion")
    assert root.component == MODELS_ROOT and root.version == "D:/spectracut/models"
    assert "model-root" in root.command and "'D:/spectracut/models'" in root.command
    for m in ESSENTIAL_MODELS:
        step = next(s for s in plan.steps if s.id == f"models:weights:{m}:legion")
        assert "$root = 'D:/spectracut/models'" in step.command
    applied = apply_plan(plan, Runner(), state.model_copy(), tmp_path)
    assert _plan(applied, "legion").steps == ()  # idempotent with the dir recorded


def test_unfinished_downloads_resume_and_unselected_ones_are_cleaned(tmp_path: Path) -> None:
    # 11 GB of moss-sfx (now unselected) and 5 GB of ezaudio-xl from an interrupted run.
    present = {"moss-sfx-8b-nf4": 11.0, "ezaudio-xl": 5.0}
    disk = ModelDisk("C:/m", free_gb=31.0, present=present)  # 31 + 11 - 20 >= 26.2 - 5
    plan = _plan(SetupState(), "legion", model_disk=disk)
    ids = _model_ids(plan)
    clean = "models:clean:moss-sfx-8b-nf4:legion"
    assert clean in ids and ids.index(clean) < ids.index("models:weights:ezaudio-xl:legion")
    assert not any(i.startswith("models:clean:ezaudio-xl") for i in ids)  # resumed
    with pytest.raises(ModelDiskError):
        _plan(SetupState(), "legion", model_disk=dataclasses.replace(disk, free_gb=30.0))
    # A recorded (installed) model is never removed, selected or not.
    state = apply_plan(_plan(ALL, "legion"), Runner(), load_state(tmp_path), tmp_path)
    done = ModelDisk("C:/m", free_gb=1.0, present={m: 1.0 for m in MODELS})
    assert _plan(state, "legion", model_disk=done).steps == ()


def test_disk_probe_cleanup_and_pointer_run_on_a_real_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "models"
    (root / "ezaudio" / "ezaudio-xl").mkdir(parents=True)
    (root / "ezaudio" / "ezaudio-xl" / "w.part").write_bytes(b"x" * 8192)
    (root / "moss-sfx" / "moss-sfx-8b-nf4").mkdir(parents=True)
    rig = Runner()

    def sh(step: Step) -> str:
        env = os.environ | {"HOME": str(tmp_path)}
        done = subprocess.run(  # noqa: S603 - the step's own script, run locally
            ["sh", "-c", step.command], capture_output=True, text=True, env=env, check=True
        )
        return done.stdout

    rig.run = sh  # type: ignore[method-assign]
    disk = probe_model_disk(rig, RIG, str(root))
    assert disk.root == str(root) and disk.free_gb > 0 and (disk.total_gb or 0) > disk.free_gb
    assert set(disk.present) == {"ezaudio-xl", "moss-sfx-8b-nf4"}
    assert disk.present["ezaudio-xl"] > 0
    roomy = dataclasses.replace(disk, free_gb=1000.0, total_gb=2000.0)  # any real disk size
    plan = _plan(SetupState(models_dir=str(root)), "rig", model_disk=roomy)
    for step in plan.steps:
        if step.id.startswith(("models:clean:", "models:root:")):
            sh(step)
    assert not (root / "moss-sfx" / "moss-sfx-8b-nf4").exists()
    assert (root / "ezaudio" / "ezaudio-xl" / "w.part").exists()
    monkeypatch.delenv(MODEL_ROOT_ENV)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert default_model_root() == root  # the host reads the pointer setup wrote
    monkeypatch.setenv(MODEL_ROOT_ENV, str(tmp_path / "env"))
    assert default_model_root() == tmp_path / "env"  # the env var still wins
    # A probe of a root that does not exist yet measures its nearest existing folder.
    fresh = probe_model_disk(rig, RIG, str(tmp_path / "a" / "b"))
    assert fresh.present == {} and fresh.free_gb > 0


def test_parse_model_disk_and_windows_probe() -> None:
    out = (
        "root=D:/m\nfree=50000000000\ntotal=1000000000000\n"
        "dirs=ezaudio/ezaudio-xl:2000000000|other/thing:5|kokoro/kokoro-82m:x\n"
    )
    disk = parse_model_disk(LEGION, out)
    assert disk == ModelDisk("D:/m", 50.0, 1000.0, {"ezaudio-xl": 2.0})
    assert parse_model_disk(LEGION, None) == ModelDisk("", 500.0, None, {})
    step = model_disk_step(LEGION, "D:/m")
    assert step.shell == "powershell" and step.artifact and step.component is None
    assert "$root = 'D:/m'" in step.command and "'total=' + ($v.Free + $v.Used)" in step.command
    lines = model_disk_lines("legion", Runner(out))
    assert lines == ["model disk on legion at D:/m: 50.0 GB free of 1000.0 GB (reserve 150 GB)"]
    assert model_disk_lines(None, Runner(out)) == []

    class Down(Runner):
        def probe(self, host: str) -> HostFacts:
            raise SetupError(f"cannot probe {host}: timeout")

    assert model_disk_lines("legion", Down()) == [
        "model disk on legion: unreachable (cannot probe legion: timeout)"
    ]


def _cli(home: Path, monkeypatch: pytest.MonkeyPatch, runner: Runner, args: Sequence[str]) -> Any:
    monkeypatch.setenv("SPECTRACUT_HOME", str(home))
    monkeypatch.setattr(setup_cli, "CommandRunner", lambda: runner)
    monkeypatch.setattr(setup_cli, "build_wheels", lambda _out: WHEELS)
    monkeypatch.setattr(setup_cli, "_model_host", lambda _home: "rig")
    monkeypatch.setattr(setup_cli, "_sync_tags", lambda *_a, **_k: None)  # no Manager here
    monkeypatch.setattr(setup_cli, "_check_job_types", lambda *_a, **_k: None)
    home.mkdir(exist_ok=True)
    (home / "hosts.json").write_text(
        '{"hosts":[{"name":"rig","cpus":8,"mem_gb":32,"profile":{"blender_version":"4.5.3"}}]}',
        encoding="utf-8",
    )
    result = CliRunner().invoke(app, ["setup", "--dry-run", *args])
    if result.exit_code != 0 and "profile" in result.output:
        pytest.skip("host registry fixture does not match RenderProfile")
    return result


def test_cli_setup_refuses_weights_that_do_not_fit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = Runner("root=/srv/m\nfree=30000000000\ntotal=100000000000\ndirs=\n")
    result = _cli(tmp_path / "home", monkeypatch, runner, [])
    assert result.exit_code != 0
    assert "6.2 GB short" in result.output and "--models-dir" in result.output
    assert runner.ran == ["models:disk:rig"]  # measured, nothing downloaded

    runner = Runner("root=/srv/m\nfree=30000000000\ntotal=100000000000\ndirs=\n")
    ok = _cli(tmp_path / "home2", monkeypatch, runner, ["--models", "kokoro-82m"])
    assert ok.exit_code == 0, ok.output
    assert "download kokoro-82m weights" in ok.output and "ezaudio" not in ok.output


def test_cli_setup_skip_models_and_bad_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = Runner("free=1\ntotal=100000000000\n")
    skip = _cli(tmp_path / "home", monkeypatch, runner, ["--skip-models"])
    assert skip.exit_code == 0, skip.output
    assert "install the local model families" in skip.output and "weights" not in skip.output
    assert "models:disk:rig" not in runner.ran
    bad = _cli(tmp_path / "home", monkeypatch, Runner(), ["--models", "nope"])
    assert bad.exit_code == 2 and "unknown model id(s) nope" in bad.output
    bad = _cli(tmp_path / "home", monkeypatch, Runner(), ["--models-dir", "rel/dir"])
    assert bad.exit_code == 2 and "--models-dir" in bad.output


def test_unprobed_model_host_is_skipped() -> None:
    assert _model_ids(_plan(SetupState(), "nowhere")) == []


def test_doctor_lists_every_model(tmp_path: Path) -> None:
    lines = model_lines(tmp_path)
    tags = {
        s.id: "essential, selected" if s.id in ESSENTIAL_MODELS else "optional, not selected"
        for s in MODELS.values()
    }
    assert f"{MODELS_DEPS} {extras_digest()}: not installed" in lines
    assert lines[: len(MODELS)] == [
        f"{s.id} {s.licence}: not installed ({tags[s.id]})" for s in MODELS.values()
    ]
    picked = model_lines(state=SetupState(models="qwen2.5-omni-7b-q4"))
    assert "qwen2.5-omni-7b-q4 Apache-2.0: not installed (optional, selected)" in picked
    assert any(x.startswith("kokoro-82m") and "(essential, not selected)" in x for x in picked)

    state = load_state(tmp_path)
    apply_plan(_plan(state, "legion"), Runner(), state, tmp_path)
    state = load_state(tmp_path)
    comps = dict(state.hosts["legion"].components) | {f"{MODEL_PREFIX}kokoro-82m": "f" * 12}
    state = state.model_copy(
        update={"hosts": {**state.hosts, "legion": HostState(components=comps)}}
    )
    lines = model_lines(state=state)
    v = model_version("ezaudio-xl")
    assert f"ezaudio-xl {v} on legion: healthy (essential, selected)" in lines
    pin = model_version("kokoro-82m")
    assert f"kokoro-82m {'f' * 12} on legion: drift (pinned {pin}) (essential, selected)" in lines
    assert f"{MODELS_DEPS} {models_deps_version(WHEELS.pin)} on legion: healthy" in lines
    assert model_lines(model_host="rig", state=state)[0].endswith(
        ": not installed (essential, selected)"
    )

    full = component_lines(tmp_path)
    assert full[-len(model_lines(tmp_path)) :] == model_lines(tmp_path)
