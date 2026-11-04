# SPDX-License-Identifier: AGPL-3.0-or-later
"""The setup plan: manifest pins diffed against what each machine already has.

:func:`build_plan` emits only the steps a machine still needs, so a converged
farm gives an empty plan (setup is idempotent). :func:`apply_plan` runs steps
through a :class:`~spectracut.setup.manifest.StepRunner` and records each success in
``<home>/setup-state.json``, so an interrupted run resumes where it stopped.
"""

from __future__ import annotations

import base64
import hashlib
import shlex
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from spectracut.setup.manifest import HostFacts, Manifest, Os, Role, Step, StepRunner
from spectracut.setup.storage import (
    CONTROL,
    GPU_SCRIPT,
    LINUX_TOOLS,
    MANAGER_PORT,
    WINDOWS_TOOLS,
    Credential,
    StorageTarget,
    choose_default_host,
    plan_storage,
    wifi_warnings,
    write_file_step,
)
from spectracut.spec._base import StrictModel
from spectracut.studio.store import spectracut_home, write_atomic

STATE_NAME = "setup-state.json"
GPU_PSEUDO = "gpu-script"
SERVICE_PSEUDO = "worker-service"
MANAGER_SERVICE = "manager-service"


class HostState(StrictModel):
    components: dict[str, str] = {}


class SetupState(StrictModel):
    set_id: str | None = None
    hosts: dict[str, HostState] = {}
    storage: StorageTarget | None = None


@dataclass(frozen=True)
class Plan:
    set_id: str
    steps: tuple[Step, ...]
    storage: StorageTarget
    notes: tuple[str, ...] = ()


def state_path(home: Path | None = None) -> Path:
    return (Path(home) if home is not None else spectracut_home()) / STATE_NAME


def load_state(home: Path | None = None) -> SetupState:
    path = state_path(home)
    if not path.is_file():
        return SetupState()
    return SetupState.model_validate_json(path.read_bytes())


def save_state(state: SetupState, home: Path | None = None) -> None:
    write_atomic(state_path(home), state.model_dump_json(indent=2).encode("utf-8"))


def gpu_script_source() -> str:
    """``flamenco_enable_gpu.py``: packaged with the wheel, else from the repo's ``scripts/``."""
    here = Path(__file__).resolve()
    for candidate in (here.with_name(GPU_SCRIPT), here.parents[3] / "scripts" / GPU_SCRIPT):
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    raise FileNotFoundError(GPU_SCRIPT)


def _roles(name: str, storage_host: str | None) -> set[Role]:
    roles: set[Role] = {"control"} if name == CONTROL else {"host"}
    if name == storage_host:
        roles.add("storage")
    return roles


def _pins(manifest: Manifest, set_id: str) -> dict[str, str]:
    return dict(manifest.tested_set(set_id).versions)


def _service_steps(facts: HostFacts, manager_url: str, pin: str) -> list[Step]:
    """Run the Manager (control plane) or a Worker (render host) as a user service."""
    name = facts.name
    if name == CONTROL:
        unit = (
            "[Unit]\nDescription=Flamenco Manager\n[Service]\nWorkingDirectory=%h/.local/opt/flamenco\n"  # noqa: E501
            "ExecStart=%h/.local/opt/flamenco/flamenco-manager\nRestart=on-failure\n"
            "[Install]\nWantedBy=default.target\n"
        )
        return [
            write_file_step(
                name,
                "linux",
                "service:manager-unit",
                "write the Manager unit",
                "$HOME/.config/systemd/user/flamenco-manager.service",
                unit,
            ),
            Step(
                name,
                "service:manager",
                "enable the Flamenco Manager",
                "systemctl --user daemon-reload && systemctl --user enable --now flamenco-manager",
                component=MANAGER_SERVICE,
                version=pin,
            ),
        ]
    version = f"{pin}@{manager_url}"
    if facts.os == "windows":
        cmd = (
            "Register-ScheduledTask -TaskName spectracut-worker -Force "
            "-Action (New-ScheduledTaskAction -Execute 'C:/spectracut/flamenco-worker.exe' "
            f"-Argument '-manager {manager_url}' -WorkingDirectory 'C:/spectracut') "
            "-Trigger (New-ScheduledTaskTrigger -AtStartup) | Out-Null; "
            "Start-ScheduledTask -TaskName spectracut-worker"
        )
        return [
            Step(
                name,
                f"service:worker:{name}",
                "register the Worker startup task",
                cmd,
                "powershell",
                component=SERVICE_PSEUDO,
                version=version,
            )
        ]
    unit = (
        "[Unit]\nDescription=Flamenco Worker\n[Service]\nWorkingDirectory=%h/.local/opt/flamenco\n"
        f"ExecStart=%h/.local/opt/flamenco/flamenco-worker -manager {manager_url}\n"
        "Restart=on-failure\n[Install]\nWantedBy=default.target\n"
    )
    return [
        write_file_step(
            name,
            "linux",
            f"service:worker-unit:{name}",
            "write the Worker unit",
            "$HOME/.config/systemd/user/spectracut-worker.service",
            unit,
        ),
        Step(
            name,
            f"service:worker:{name}",
            "enable the Flamenco Worker",
            "systemctl --user daemon-reload && systemctl --user enable --now spectracut-worker",
            component=SERVICE_PSEUDO,
            version=version,
        ),
    ]


def build_plan(
    manifest: Manifest,
    set_id: str,
    facts: Mapping[str, HostFacts],
    workers: Sequence[str],
    state: SetupState,
    cred: Credential,
    storage: StorageTarget | None = None,
) -> Plan:
    """Steps still needed to bring every machine to ``set_id`` with storage on ``storage``.

    ``storage`` defaults to the target already in ``state``, else the wired
    machine with the most free space.
    """
    target = storage or state.storage or choose_default_host(facts)
    storage_host = target.host if target.kind == "host" else None
    pins = _pins(manifest, set_id)
    manager_url = f"http://{facts[CONTROL].address}:{MANAGER_PORT}"
    gpu_src = gpu_script_source()
    gpu_hash = hashlib.sha256(gpu_src.encode("utf-8")).hexdigest()[:12]
    steps: list[Step] = []
    for name in [CONTROL, *workers]:
        f = facts[name]
        have = state.hosts.get(name, HostState()).components
        roles = _roles(name, storage_host)
        for cname, comp in manifest.components.items():
            if not roles & set(comp.roles) or f.os not in comp.os:
                continue
            if have.get(cname) == pins[cname]:
                continue
            steps.append(
                Step(
                    name,
                    f"install:{cname}:{name}",
                    f"install {cname} {pins[cname]}",
                    comp.install[f.os].format(version=pins[cname]),
                    "powershell" if f.os == "windows" else "sh",
                    component=cname,
                    version=pins[cname],
                )
            )
        if name == CONTROL:
            svc = _service_steps(f, manager_url, pins["flamenco-manager"])
        elif name in workers:
            if have.get(GPU_PSEUDO) != gpu_hash:
                tools = WINDOWS_TOOLS if f.os == "windows" else LINUX_TOOLS
                step = write_file_step(
                    name,
                    f.os,
                    f"gpu-script:{name}",
                    "install the Cycles GPU and GPU-denoise startup script",
                    f"{tools}/{GPU_SCRIPT}",
                    gpu_src,
                )
                steps.append(
                    Step(**{**step.__dict__, "component": GPU_PSEUDO, "version": gpu_hash})
                )
            svc = _service_steps(f, manager_url, pins["flamenco-worker"])
        else:
            svc = []
        want = (
            {MANAGER_SERVICE: pins["flamenco-manager"]}
            if name == CONTROL
            else {SERVICE_PSEUDO: f"{pins['flamenco-worker']}@{manager_url}"}
        )
        if any(have.get(k) != v for k, v in want.items()):
            steps += svc
    steps += plan_storage(target, state.storage, facts, workers, cred)
    return Plan(set_id, tuple(steps), target, tuple(wifi_warnings(facts, target)))


def apply_plan(
    plan: Plan, runner: StepRunner, state: SetupState, home: Path | None = None
) -> SetupState:
    """Run the steps in order; record each success. A failure stops the run (state kept)."""
    for step in plan.steps:
        runner.run(step)
        if step.component and step.version:
            host = state.hosts.setdefault(step.host, HostState())
            host.components[step.component] = step.version
            save_state(state, home)
    state = state.model_copy(update={"set_id": plan.set_id, "storage": plan.storage})
    save_state(state, home)
    return state


class SetupError(RuntimeError):
    pass


class CommandRunner:
    """Runs steps on the control plane locally and on render hosts over SSH."""

    def __init__(self, timeout: float = 1800.0) -> None:
        self.timeout = timeout

    def _exec(
        self, host: str, cmd: str, shell: str, stdin: str | None
    ) -> subprocess.CompletedProcess[str]:
        if shell == "powershell":
            enc = base64.b64encode(cmd.encode("utf-16-le")).decode("ascii")
            remote = f"powershell -NoProfile -NonInteractive -EncodedCommand {enc}"
        else:
            remote = f"sh -c {shlex.quote(cmd)}"
        argv = ["sh", "-c", cmd] if host == CONTROL else ["ssh", host, remote]
        return subprocess.run(
            argv, input=stdin, capture_output=True, text=True, timeout=self.timeout, check=False
        )

    def run(self, step: Step) -> None:
        proc = self._exec(step.host, step.command, step.shell, step.stdin)
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout).strip().splitlines()[-1:] or [""]
            raise SetupError(f"{step.host}: {step.summary} failed ({proc.returncode}): {tail[0]}")

    def probe(self, host: str) -> HostFacts:
        os: Os = "linux"
        if host != CONTROL and self._exec(host, "uname -s", "sh", None).returncode != 0:
            os = "windows"
        if os == "windows":
            script = (
                "(tailscale ip -4)[0]; (Get-NetAdapter -Physical | Where-Object Status -eq Up "
                "| ForEach-Object PhysicalMediaType) -join ','; (Get-PSDrive C).Free"
            )
        else:
            script = (
                "tailscale ip -4 | head -1; ip route get 1.1.1.1 | awk '{for(i=1;i<NF;i++) "
                'if($i=="dev"){print $(i+1);exit}}\'; df -Pk "$HOME" | awk \'NR==2{print $4*1024}\''
            )
        proc = self._exec(host, script, "powershell" if os == "windows" else "sh", None)
        if proc.returncode != 0:
            raise SetupError(f"cannot probe {host}: {proc.stderr.strip()[-200:]}")
        return parse_probe(host, os, proc.stdout)


def parse_probe(name: str, os: Os, out: str) -> HostFacts:
    """Probe output: Tailscale address, network device or media type, free bytes."""
    lines = [ln.strip() for ln in out.splitlines()]
    lines += [""] * (3 - len(lines))
    address, link, free = lines[:3]
    wired = "802.11" not in link if os == "windows" else not link.startswith("wl")
    gb = float(free) / 1e9 if free.replace(".", "", 1).isdigit() else 0.0
    return HostFacts(name=name, os=os, address=address or name, wired=wired, free_gb=gb)
