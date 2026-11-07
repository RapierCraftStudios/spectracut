# SPDX-License-Identifier: AGPL-3.0-or-later
"""Model-host setup steps (family extras, pinned weights) and the doctor model lines."""

from __future__ import annotations

import dataclasses
import tempfile
from pathlib import Path

import spectracut
from spectracut.providers.local.registry import MODELS
from spectracut.setup import component_lines, model_lines
from spectracut.setup.manifest import HostFacts, load_manifest
from spectracut.setup.model_assets import ASSETS, TORCH_INDEX, extras_digest, model_version
from spectracut.setup.plan import (
    MODEL_PREFIX,
    MODELS_DEPS,
    HostState,
    Plan,
    SetupState,
    apply_plan,
    build_plan,
    load_state,
    models_deps_version,
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


class Runner:
    def __init__(self) -> None:
        self.ran: list[str] = []

    def probe(self, host: str) -> HostFacts:
        return FACTS[host]

    def run(self, step: object) -> str | None:
        self.ran.append(step.id)  # type: ignore[attr-defined]
        return None


def _plan(
    state: SetupState, model_host: str | None = None, facts: dict[str, HostFacts] = FACTS
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
    )


def _model_ids(plan: Plan) -> list[str]:
    return [s.id for s in plan.steps if s.id.startswith("models:")]


def test_without_model_host_no_model_steps() -> None:
    assert _model_ids(_plan(SetupState())) == []
    plain = [s.id for s in _plan(SetupState()).steps]
    with_models = [s.id for s in _plan(SetupState(), "legion").steps]
    assert with_models[: len(plain)] == plain  # existing ids and order untouched


def test_model_host_gets_deps_then_every_weights_step() -> None:
    plan = _plan(SetupState(), "legion")
    ids = [s.id for s in plan.steps]
    assert _model_ids(plan) == ["models:deps:legion"] + [
        f"models:weights:{m}:legion" for m in MODELS
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
        plan = _plan(SetupState(), host)
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
    assert all(have[f"{MODEL_PREFIX}{m}"] == model_version(m) for m in MODELS)
    assert _plan(load_state(tmp_path), "legion").steps == ()

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


def test_low_disk_adds_a_note() -> None:
    small = FACTS | {"legion": dataclasses.replace(LEGION, free_gb=10)}
    notes = _plan(SetupState(), "legion", small).notes
    assert any("legion has 10 GB free" in n for n in notes)
    assert not any("model weights" in n for n in _plan(SetupState(), "legion").notes)


def test_unprobed_model_host_is_skipped() -> None:
    assert _model_ids(_plan(SetupState(), "nowhere")) == []


def test_doctor_lists_every_model(tmp_path: Path) -> None:
    lines = model_lines(tmp_path)
    for spec in MODELS.values():
        assert f"{spec.id} {spec.licence}: not installed" in lines
    assert f"{MODELS_DEPS} {extras_digest()}: not installed" in lines
    assert lines[: len(MODELS)] == [f"{s.id} {s.licence}: not installed" for s in MODELS.values()]

    state = load_state(tmp_path)
    apply_plan(_plan(state, "legion"), Runner(), state, tmp_path)
    state = load_state(tmp_path)
    comps = dict(state.hosts["legion"].components) | {f"{MODEL_PREFIX}kokoro-82m": "f" * 12}
    state = state.model_copy(
        update={"hosts": {**state.hosts, "legion": HostState(components=comps)}}
    )
    lines = model_lines(state=state)
    v = model_version("ezaudio-xl")
    assert f"ezaudio-xl {v} on legion: healthy" in lines
    assert f"kokoro-82m {'f' * 12} on legion: drift (pinned {model_version('kokoro-82m')})" in lines
    assert f"{MODELS_DEPS} {models_deps_version(WHEELS.pin)} on legion: healthy" in lines
    assert model_lines(model_host="rig", state=state)[0].endswith(": not installed")

    full = component_lines(tmp_path)
    assert full[-len(model_lines(tmp_path)) :] == model_lines(tmp_path)
