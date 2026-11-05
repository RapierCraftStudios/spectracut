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
import ipaddress
import json
import re
import shlex
import signal
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import partial
from os import killpg
from pathlib import Path
from typing import Protocol

from spectracut.queue.profile import RenderProfile
from spectracut.setup.manifest import (
    HostFacts,
    Manifest,
    Os,
    Role,
    Shell,
    Step,
    StepKind,
    StepRunner,
)
from spectracut.setup.storage import (
    CONTROL,
    GPU_SCRIPT,
    LINUX_TOOLS,
    MANAGER_PORT,
    WINDOWS_DRIVE,
    WINDOWS_TOOLS,
    WORKER_TASK,
    Credential,
    StorageLayout,
    StorageTarget,
    choose_default_host,
    normalize_drive,
    plan_storage,
    storage_folder,
    storage_notes,
    wifi_warnings,
    windows_task_restart,
    windows_task_start,
    windows_task_stop,
    write_file_step,
)
from spectracut.setup.transport import STEP_INPUT, frame_input, windows_input_preamble
from spectracut.setup.wheels import (
    CONTROL_PLANE_PIN,
    LocalWheels,
    install_fields,
    wheel_copy_steps,
)
from spectracut.spec._base import StrictModel
from spectracut.studio.store import spectracut_home, write_atomic

STATE_NAME = "setup-state.json"
GPU_PSEUDO = "gpu-script"
SERVICE_PSEUDO = "worker-service"
MANAGER_SERVICE = "manager-service"
HOST_PSEUDO = "host-profile"
HOST_REG_PSEUDO = "host-registration"
HOST_REG_REL = ".config/spectracut/host.json"
WORKER_SCRIPT = "windows_worker.ps1"
TASK_SCRIPT = "windows_worker_task.ps1"
WORKER_ARGS = "worker-args.json"
PROFILE_DIR = "profiles"
LINUX_BIN = "$HOME/.local/bin"
# Components whose files the running worker holds open: replaced only with the worker stopped.
WORKER_FILES = ("flamenco-worker", "spectracut-host")
SSH_ERROR = 255

# No CIM or WMI cmdlets: a network-logon standard user is denied WMI. Admin comes from the
# SSH token, the NICs from .NET. Every field prints its own ``key=value`` line even when its
# command fails (the Tailscale CLI is denied to a non-interactive account).
WINDOWS_PROBE = (
    "$a = ''; try { $a = [string](tailscale ip -4 2>$null | Select-Object -First 1) } "
    "catch {}; 'address=' + $a; "
    "'link=' + ((@([System.Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces() "
    "| Where-Object { $_.OperationalStatus -eq 'Up' -and "
    "$_.NetworkInterfaceType -notin 'Loopback', 'Tunnel' }) | ForEach-Object { "
    "if ($_.NetworkInterfaceType -eq 'Wireless80211') { '802.11' } "
    "else { [string]$_.NetworkInterfaceType } }) -join ','); 'free=' + (Get-PSDrive C).Free; "
    "'admin=' + ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]"
    "::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator); "
    "'home=' + $env:USERPROFILE; "
    f"schtasks.exe /query /tn {WORKER_TASK} *> $null; "
    "'task=' + ($LASTEXITCODE -eq 0); "
    # Drive letters in use: letter=UNC (network), letter=folder (subst), letter=.
    "$sub = @{}; subst | ForEach-Object { if ($_ -match '^(\\w):\\\\: => (.*)$') "
    "{ $sub[$matches[1]] = $matches[2] } }; 'drives=' + ((@(Get-PSDrive -PSProvider FileSystem) "
    "| ForEach-Object { $r = if ($sub[$_.Name]) { $sub[$_.Name] } "
    "else { [string]$_.DisplayRoot }; $_.Name + '=' + $r }) -join '|')"
)
LINUX_PROBE = (
    'echo "address=$(tailscale ip -4 2>/dev/null | head -1)"; '
    "echo \"link=$(ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<NF;i++) "
    'if($i=="dev"){print $(i+1);exit}}\')"; '
    'echo "free=$(df -Pk "$HOME" | awk \'NR==2{print $4*1024}\')"'
)


class HostState(StrictModel):
    components: dict[str, str] = {}


class SetupState(StrictModel):
    set_id: str | None = None
    hosts: dict[str, HostState] = {}
    storage: StorageTarget | None = None
    # Requested by ``setup --storage-path/--windows-drive`` (None: the per-OS default folder).
    storage_path: str | None = None
    windows_drive: str = WINDOWS_DRIVE
    # The layout last applied with ``storage``; None on older states means the defaults.
    storage_layout: StorageLayout | None = None

    def layout(self) -> StorageLayout:
        return StorageLayout(path=self.storage_path, drive=normalize_drive(self.windows_drive))


@dataclass(frozen=True)
class Plan:
    set_id: str
    steps: tuple[Step, ...]
    storage: StorageTarget
    notes: tuple[str, ...] = ()
    layout: StorageLayout = field(default_factory=StorageLayout)


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


def worker_script_source() -> str:
    """``windows_worker.ps1``: the script the ``spectracut-worker`` task runs on Windows."""
    return Path(__file__).with_name(WORKER_SCRIPT).read_text(encoding="utf-8")


def task_script_source() -> str:
    """``windows_worker_task.ps1``: the one-time elevated script that registers the task."""
    return Path(__file__).with_name(TASK_SCRIPT).read_text(encoding="utf-8")


def task_script_command(home: str) -> str:
    """The one command the owner runs in an elevated PowerShell (no secrets in it)."""
    path = f"{home or '<the SSH account profile>'}\\spectracut\\{TASK_SCRIPT}"
    return f'powershell -NoProfile -ExecutionPolicy Bypass -File "{path}"'


def worker_task_notes(facts: Mapping[str, HostFacts], hosts: Sequence[str]) -> list[str]:
    """One note per non-admin Windows worker: the elevated command that registers its task."""
    return [
        f"{h} needs a one-time admin step: its SSH account is not an Administrator, so after "
        f"this setup run open an elevated PowerShell on {h} and run "
        f"`{task_script_command(facts[h].home)}`; it registers the {WORKER_TASK} task to run "
        "at startup as that account and lets the account start and stop it, so later setups "
        "need no admin"
        for h in hosts
    ]


def profile_write_step(host: str, step_id: str, summary: str, rel: str, content: str) -> Step:
    """Write ``content`` (framed stdin) to ``rel`` under the Windows account's profile."""
    cmd = (
        f"$p = Join-Path $env:USERPROFILE '{rel}'; "
        "New-Item -ItemType Directory -Force (Split-Path $p) | Out-Null; "
        f"[IO.File]::WriteAllText($p, {STEP_INPUT})"
    )
    return Step(host, step_id, summary, cmd, "powershell", stdin=content, kind="write")


def _roles(name: str, storage_host: str | None) -> set[Role]:
    roles: set[Role] = {"control"} if name == CONTROL else {"host"}
    if name == storage_host:
        roles.add("storage")
    return roles


def _pins(manifest: Manifest, set_id: str, wheels: LocalWheels) -> dict[str, str]:
    """The set's pins; a component built on the control plane is pinned to its wheels."""
    pins = dict(manifest.tested_set(set_id).versions)
    return {c: wheels.pin if v == CONTROL_PLANE_PIN else v for c, v in pins.items()}


def _worker_args(
    manager_url: str,
    target: StorageTarget,
    facts: Mapping[str, HostFacts],
    name: str,
    user: str,
    layout: StorageLayout | None = None,
) -> str:
    """``worker-args.json`` for ``windows_worker.ps1``: Manager, drive, share and its account.

    Kept out of the task's action so the task is registered once (by an admin) and later
    setups change the worker's settings without admin.
    """
    layout = layout or StorageLayout()
    server = facts[target.host].address if target.kind == "host" else target.host
    args = {"Manager": manager_url, "Drive": normalize_drive(layout.drive), "Share": target.share}
    if target.kind == "host" and target.host == name:
        args["LocalPath"] = storage_folder(target, facts, layout)
    else:
        args |= {"Server": server, "User": user}
    return json.dumps(args, sort_keys=True)


def _service_steps(
    facts: HostFacts,
    manager_url: str,
    pin: str,
    worker_args: str = "",
    script_hash: str = "",
) -> list[Step]:
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
    if facts.os == "windows":
        return _windows_service_steps(facts, manager_url, pin, worker_args, script_hash)
    version = f"{pin}@{manager_url}#path"
    unit = (
        "[Unit]\nDescription=Flamenco Worker\n[Service]\nWorkingDirectory=%h/.local/opt/flamenco\n"
        "Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin\n"
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


def _windows_service_steps(
    facts: HostFacts, manager_url: str, pin: str, worker_args: str, script_hash: str
) -> list[Step]:
    """Worker settings and the task script under the profile, then register or restart the task.

    Only an Administrator can register a task that runs at startup as this account; on
    a standard account setup writes the files, the owner runs the task script once
    elevated (see :func:`worker_task_notes`), and setup restarts it with schtasks.exe.
    """
    name = facts.name
    write_task = (
        f"$p = Join-Path $env:USERPROFILE 'spectracut/{TASK_SCRIPT}'; "
        "New-Item -ItemType Directory -Force (Split-Path $p) | Out-Null; "
        "$id = [Security.Principal.WindowsIdentity]::GetCurrent(); "
        f"$t = {STEP_INPUT}.Replace('__ACCOUNT__', $id.Name)"
        ".Replace('__SID__', $id.User.Value).Replace('__HOME__', $env:USERPROFILE); "
        "[IO.File]::WriteAllText($p, $t)"
    )
    if facts.admin:
        cmd = (
            f"powershell.exe -NoProfile -ExecutionPolicy Bypass -File "
            f"(Join-Path $env:USERPROFILE 'spectracut/{TASK_SCRIPT}'); exit $LASTEXITCODE"
        )
        summary = "register the Worker startup task"
    else:
        cmd = windows_task_restart()
        summary = "restart the Worker task (registered by the one-time admin step)"
    return [
        profile_write_step(
            name,
            f"worker-args:{name}",
            "write the Worker settings",
            f"spectracut/{WORKER_ARGS}",
            worker_args,
        ),
        Step(
            name,
            f"worker-task-script:{name}",
            "install the one-time Worker task script",
            write_task,
            "powershell",
            stdin=task_script_source(),
            kind="write",
        ),
        Step(
            name,
            f"service:worker:{name}",
            summary,
            cmd,
            "powershell",
            component=SERVICE_PSEUDO,
            version=f"{pin}@{manager_url}#{script_hash}",
        ),
    ]


def _profile_step(facts: HostFacts, version: str) -> Step:
    """Run ``spectracut-host profile`` on the host; the JSON is what ``hosts add`` takes."""
    if facts.os == "windows":
        cmd = (
            "$b = Get-ChildItem (Join-Path $env:USERPROFILE 'spectracut') -Directory "
            "-Filter 'blender-*-windows-x64' | Sort-Object Name | Select-Object -Last 1; "
            "$env:PATH = $b.FullName + ';' + $env:PATH; "
            "& (Join-Path $env:USERPROFILE '.local/bin/spectracut-host.exe') profile"
        )
        shell: Shell = "powershell"
    else:
        cmd = f'PATH="{LINUX_BIN}:$PATH" "{LINUX_BIN}/spectracut-host" profile'
        shell = "sh"
    return Step(
        facts.name,
        f"host:profile:{facts.name}",
        "read the render profile (for `spectracut hosts add`)",
        cmd,
        shell,
        component=HOST_PSEUDO,
        version=version,
        artifact=f"{facts.name}.json",
    )


def _windows_worker_files(
    name: str, script: str, target: StorageTarget, cred: Credential, host: str
) -> list[Step]:
    """The logon script and, when this host maps a remote share, its credential (stdin only)."""
    files = [
        profile_write_step(
            name,
            f"worker-script:{name}",
            "install the Worker logon script",
            f"spectracut/{WORKER_SCRIPT}",
            script,
        )  # fmt: skip
    ]
    if not (target.kind == "host" and target.host == host):
        files.append(
            profile_write_step(
                name,
                f"worker-cred:{name}",
                "store the share credential for the logon task",
                "spectracut/smb.pw",
                cred.password + "\n",
            )  # fmt: skip
        )
    return files


def _worker_stop_start(f: HostFacts) -> tuple[Step, Step]:
    """Stop the worker (task or unit) before its files are replaced, start it again after.

    Both tolerate a host with no worker registered yet (a first install) and need no
    admin or sudo. They carry no component, so setup state never records them.
    """
    if f.os == "windows":
        stop = (
            f"{windows_task_stop()}; "
            "$root = Join-Path $env:USERPROFILE ''; "
            "$p = @(Get-Process -Name flamenco-worker, spectracut-host -ErrorAction "
            "SilentlyContinue | Where-Object { $_.Path -and $_.Path.StartsWith($root) }); "
            "$p | Stop-Process -Force -ErrorAction SilentlyContinue; "
            "$p | ForEach-Object { $_.WaitForExit(15000) | Out-Null }; $true | Out-Null"
        )
        start = windows_task_start()
        shell: Shell = "powershell"
    else:
        stop = f"systemctl --user stop {WORKER_TASK} 2>/dev/null || true"
        start = (
            f"if systemctl --user is-enabled --quiet {WORKER_TASK} 2>/dev/null; "
            f"then systemctl --user start {WORKER_TASK}; fi"
        )
        shell = "sh"
    return (
        Step(
            f.name,
            f"worker:stop:{f.name}",
            "stop the Flamenco Worker for the upgrade",
            stop,
            shell,
            kind="task",
        ),
        Step(
            f.name,
            f"worker:start:{f.name}",
            "start the Flamenco Worker again",
            start,
            shell,
            kind="task",
        ),
    )


def _install_steps(
    manifest: Manifest,
    pins: Mapping[str, str],
    f: HostFacts,
    have: Mapping[str, str],
    roles: set[Role],
    wheels: LocalWheels,
) -> list[Step]:
    """Install steps for every component this machine's roles need and does not yet have.

    A component built on the control plane (pinned to ``wheels.pin``) first gets its
    wheels copied to the host.
    """
    steps: list[Step] = []
    fields = install_fields(f.os, wheels)
    stop_at: int | None = None
    for cname, comp in manifest.components.items():
        if not roles & set(comp.roles) or f.os not in comp.os:
            continue
        if have.get(cname) == pins[cname]:
            continue
        if stop_at is None and cname in WORKER_FILES and "host" in roles:
            stop_at = len(steps)
        if pins[cname] == wheels.pin:
            steps += wheel_copy_steps(f, wheels)
        steps.append(
            Step(
                f.name,
                f"install:{cname}:{f.name}",
                f"install {cname} {pins[cname]}",
                comp.install[f.os].format(**fields, version=pins[cname]),
                "powershell" if f.os == "windows" else "sh",
                component=cname,
                version=pins[cname],
                kind="install",
            )
        )
    if stop_at is not None:
        stop, start = _worker_stop_start(f)
        steps.insert(stop_at, stop)
        steps.append(start)
    return steps


def _worker_prep_steps(
    f: HostFacts, have: Mapping[str, str], pins: Mapping[str, str], gpu_src: str, gpu_hash: str
) -> list[Step]:
    """The GPU startup script and the render profile read for a render host."""
    steps: list[Step] = []
    if have.get(GPU_PSEUDO) != gpu_hash:
        tools = WINDOWS_TOOLS if f.os == "windows" else LINUX_TOOLS
        step = write_file_step(
            f.name,
            f.os,
            f"gpu-script:{f.name}",
            "install the Cycles GPU and GPU-denoise startup script",
            f"{tools}/{GPU_SCRIPT}",
            gpu_src,
        )
        steps.append(Step(**{**step.__dict__, "component": GPU_PSEUDO, "version": gpu_hash}))
    profile = f"{pins['spectracut-host']}+{pins['blender']}"
    if have.get(HOST_PSEUDO) != profile:
        steps.append(_profile_step(f, profile))
    return steps


def host_registration(name: str, public_key: str) -> str:
    """``host.json`` for a worker: its registered name and the control plane's public key."""
    return json.dumps({"control_plane_public_key": public_key, "host_id": name}, sort_keys=True)


def _registration_step(f: HostFacts, public_key: str) -> Step:
    """Write ``host.json`` in the account the Worker runs as (the SSH account); no sudo.

    The execution guard reads it from that account's home on every job, so no restart.
    Only the public key is written; the private key never leaves the control plane.
    """
    content = host_registration(f.name, public_key)
    version = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
    summary = "register the host (host.json with the control plane's public key)"
    if f.os == "windows":
        step = profile_write_step(
            f.name, f"host-registration:{f.name}", summary, HOST_REG_REL, content
        )
    else:
        cmd = (
            f'p="$HOME/{HOST_REG_REL}"; mkdir -p "$(dirname "$p")" && '
            '(umask 077 && cat > "$p.tmp") && chmod 600 "$p.tmp" && mv -f "$p.tmp" "$p"'
        )
        step = Step(f.name, f"host-registration:{f.name}", summary, cmd, "sh", stdin=content)
    return replace(step, component=HOST_REG_PSEUDO, version=version)


def build_plan(
    manifest: Manifest,
    set_id: str,
    facts: Mapping[str, HostFacts],
    workers: Sequence[str],
    state: SetupState,
    cred: Credential,
    storage: StorageTarget | None = None,
    *,
    wheels: LocalWheels,
    signing_public_key: str | None = None,
) -> Plan:
    """Steps still needed to bring every machine to ``set_id`` with storage on ``storage``.

    ``storage`` defaults to the target already in ``state``, else the wired
    machine with the most free space. With ``signing_public_key`` every worker gets
    a ``host.json`` registering it under its registered name with that key.
    """
    target = storage or state.storage or choose_default_host(facts)
    layout = state.layout()
    storage_host = target.host if target.kind == "host" else None
    pins = _pins(manifest, set_id, wheels)
    manager_url = f"http://{facts[CONTROL].address}:{MANAGER_PORT}"
    gpu_src = gpu_script_source()
    gpu_hash = hashlib.sha256(gpu_src.encode("utf-8")).hexdigest()[:12]
    worker_src = worker_script_source() + task_script_source()
    steps: list[Step] = []
    need_task: list[str] = []
    for name in [CONTROL, *workers]:
        f = facts[name]
        have = state.hosts.get(name, HostState()).components
        roles = _roles(name, storage_host)
        steps += _install_steps(manifest, pins, f, have, roles, wheels)
        if name == CONTROL:
            svc = _service_steps(f, manager_url, pins["flamenco-manager"])
        elif name in workers:
            steps += _worker_prep_steps(f, have, pins, gpu_src, gpu_hash)
            if signing_public_key is not None:
                reg = _registration_step(f, signing_public_key)
                if have.get(HOST_REG_PSEUDO) != reg.version:
                    steps.append(reg)
            args = _worker_args(manager_url, target, facts, name, cred.user, layout)
            digest = hashlib.sha256((worker_src + args).encode("utf-8")).hexdigest()[:12]
            svc = _service_steps(f, manager_url, pins["flamenco-worker"], args, digest)
            if f.os == "windows":
                svc = _windows_worker_files(name, worker_src, target, cred, name) + svc
        else:
            svc = []
        want = (
            {MANAGER_SERVICE: pins["flamenco-manager"]}
            if name == CONTROL
            else {SERVICE_PSEUDO: svc[-1].version}
            if svc
            else {}
        )
        if any(have.get(k) != v for k, v in want.items()):
            steps += svc
            if name in workers and f.os == "windows" and not f.admin:
                need_task.append(name)
        elif name in workers and f.os == "windows" and not f.admin and not f.task:
            need_task.append(name)
    steps += plan_storage(target, state.storage, facts, workers, cred, layout, state.storage_layout)
    return Plan(
        set_id,
        tuple(steps),
        target,
        tuple(
            storage_notes(target, facts)
            + wifi_warnings(facts, target)
            + worker_task_notes(facts, need_task)
        ),
        layout,
    )


def _run_steps(
    steps: Sequence[Step],
    runner: StepRunner,
    state: SetupState,
    home: Path | None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    for step in steps:
        out = run_with_retry(partial(runner.run, step), step.kind, sleep)
        if step.artifact and out:
            write_atomic(
                (Path(home) if home is not None else spectracut_home())
                / PROFILE_DIR
                / step.artifact,
                out.encode("utf-8"),
            )
        if step.component and step.version:
            host = state.hosts.setdefault(step.host, HostState())
            host.components[step.component] = step.version
            save_state(state, home)


def apply_plan(
    plan: Plan,
    runner: StepRunner,
    state: SetupState,
    home: Path | None = None,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> SetupState:
    """Run the steps in order; record each success. A failure stops the run (state kept).

    Idempotent steps (:data:`RETRY_KINDS`) are retried with backoff before failing.
    """
    _run_steps(plan.steps, runner, state, home, sleep)
    state = state.model_copy(
        update={"set_id": plan.set_id, "storage": plan.storage, "storage_layout": plan.layout}
    )
    save_state(state, home)
    return state


class BootstrapRunner(StepRunner, Protocol):
    def resources(self, host: str, os: Os) -> tuple[float, float]: ...


@dataclass(frozen=True)
class Bootstrapped:
    """What bootstrapping an unregistered host learned: its profile and capacity."""

    profile: RenderProfile
    cpus: float
    mem_gb: float


def bootstrap_host(
    manifest: Manifest,
    alias: str,
    runner: BootstrapRunner,
    home: Path | None = None,
    *,
    wheels: LocalWheels,
    sleep: Callable[[float], None] = time.sleep,
) -> Bootstrapped:
    """Probe ``alias`` over SSH, run the host install steps and read its render profile.

    Components are recorded in the setup state, so a later ``spectracut setup`` finds
    the host converged. Storage and the worker service are left to setup.
    """
    facts = runner.probe(alias)
    state = load_state(home)
    set_id = state.set_id or manifest.sets[0].id
    pins = _pins(manifest, set_id, wheels)
    have = state.hosts.get(alias, HostState()).components
    gpu_src = gpu_script_source()
    gpu_hash = hashlib.sha256(gpu_src.encode("utf-8")).hexdigest()[:12]
    steps = _install_steps(manifest, pins, facts, have, {"host"}, wheels)
    steps += _worker_prep_steps(facts, have, pins, gpu_src, gpu_hash)
    profile_step = _profile_step(facts, f"{pins['spectracut-host']}+{pins['blender']}")
    if not any(s.id == profile_step.id for s in steps):
        steps.append(profile_step)
    _run_steps(steps, runner, state, home, sleep)
    path = (Path(home) if home is not None else spectracut_home()) / PROFILE_DIR
    try:
        profile = RenderProfile.model_validate_json((path / f"{alias}.json").read_bytes())
    except (OSError, ValueError) as exc:
        raise SetupError(f"{alias}: cannot read the render profile: {exc}") from exc
    cpus, mem_gb = runner.resources(alias, facts.os)
    return Bootstrapped(profile, cpus, mem_gb)


class SetupError(RuntimeError):
    pass


class StepTimeout(SetupError):
    """A step outlived its timeout; its ssh client (process group) was killed."""


# Seconds per step kind. Copies and writes are small; installs download and unpack.
STEP_TIMEOUTS: dict[StepKind, float] = {
    "copy": 120.0,
    "write": 120.0,
    "probe": 120.0,
    "task": 120.0,
    "install": 1800.0,
    "run": 600.0,
}
# Idempotent kinds: safe to run again after a timeout or a failure.
RETRY_KINDS: frozenset[StepKind] = frozenset({"copy", "write", "probe", "task"})
RETRIES = 3
BACKOFF = 2.0


def run_with_retry[T](
    fn: Callable[[], T],
    kind: StepKind,
    sleep: Callable[[float], None] = time.sleep,
    retries: int = RETRIES,
) -> T:
    """``fn()``; for a :data:`RETRY_KINDS` kind, up to ``retries`` retries (2, 4, 8 s apart).

    Only :class:`SetupError` (including :class:`StepTimeout`) is retried; the last
    one is raised. Other kinds run once.
    """
    attempts = retries + 1 if kind in RETRY_KINDS else 1
    for attempt in range(attempts):
        try:
            return fn()
        except SetupError:
            if attempt == attempts - 1:
                raise
            sleep(BACKOFF * 2**attempt)
    raise AssertionError("unreachable")  # pragma: no cover


_CLIXML_OBJS = re.compile(r"<Objs\b.*?</Objs>", re.DOTALL)
_CLIXML_ERR = re.compile(r'<S S="[Ee]rror">(.*?)</S>', re.DOTALL)


def _error_lines(text: str) -> list[str]:
    """Non-empty lines of ``text`` with PowerShell CLIXML records reduced to their errors."""

    def _errors(m: re.Match[str]) -> str:
        found = _CLIXML_ERR.findall(m.group(0))
        return "".join(found).replace("_x000D__x000A_", "\n").replace("_x000A_", "\n")

    text = _CLIXML_OBJS.sub(_errors, text.replace("#< CLIXML", ""))
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


class CommandRunner:
    """Runs steps on the control plane locally and on render hosts over SSH."""

    def __init__(self, timeout: float | None = None) -> None:
        # When set, overrides every kind's timeout (STEP_TIMEOUTS).
        self.timeout = timeout

    def timeout_for(self, kind: StepKind) -> float:
        return self.timeout if self.timeout is not None else STEP_TIMEOUTS[kind]

    def _exec(
        self, host: str, cmd: str, shell: str, stdin: str | None
    ) -> subprocess.CompletedProcess[str]:
        """A read-only call (probe, resources, address): the probe kind's timeout."""
        return self._spawn(host, cmd, shell, stdin, self.timeout_for("probe"), None)

    def _spawn(
        self,
        host: str,
        cmd: str,
        shell: str,
        stdin: str | None,
        limit: float,
        summary: str | None,
    ) -> subprocess.CompletedProcess[str]:
        """Run ``cmd``; past ``limit`` s kill its process group and raise :class:`StepTimeout`.

        A PowerShell step's stdin is framed (:mod:`spectracut.setup.transport`) and read
        into ``$StepInput`` by exact length, since Windows OpenSSH can lose stdin EOF.
        """
        if shell == "powershell":
            if stdin is not None:
                cmd = windows_input_preamble() + cmd
                stdin = frame_input(stdin)
            cmd = f"$ProgressPreference = 'SilentlyContinue'; {cmd}"
            enc = base64.b64encode(cmd.encode("utf-16-le")).decode("ascii")
            remote = f"powershell -NoProfile -NonInteractive -EncodedCommand {enc}"
        else:
            remote = f"sh -c {shlex.quote(cmd)}"
        argv = ["sh", "-c", cmd] if host == CONTROL else ["ssh", host, remote]
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE if stdin is not None else None,  # as before: inherited
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            out, err = proc.communicate(stdin, timeout=limit)
        except subprocess.TimeoutExpired:
            try:
                killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.communicate()
            what = summary or cmd.split(";", 1)[0][:60]
            raise StepTimeout(
                f"{host}: {what} timed out after {limit:g} s (ssh client killed)"
            ) from None
        return subprocess.CompletedProcess(argv, proc.returncode, out, err)

    def run(self, step: Step) -> str | None:
        proc = self._spawn(
            step.host,
            step.command,
            step.shell,
            step.stdin,
            self.timeout_for(step.kind),
            step.summary,
        )
        if proc.returncode != 0:
            tail = _error_lines(proc.stderr or proc.stdout)[-1:] or [""]
            raise SetupError(f"{step.host}: {step.summary} failed ({proc.returncode}): {tail[0]}")
        return proc.stdout if step.artifact else None

    def probe(self, host: str) -> HostFacts:
        os: Os = "linux"
        if host != CONTROL:

            def _uname() -> subprocess.CompletedProcess[str]:
                first = self._exec(host, "uname -s", "sh", None)
                if first.returncode == SSH_ERROR:
                    raise SetupError(
                        f"cannot reach {host} over SSH: "
                        f"{first.stderr.strip()[-200:] or 'no output'}"
                    )
                return first

            if run_with_retry(_uname, "probe").returncode != 0:
                os = "windows"
        if os == "windows":
            # No CIM or WMI cmdlets: a network-logon standard user is
            # denied WMI. Admin comes from the SSH token, the NICs from .NET.
            script = WINDOWS_PROBE
        else:
            script = LINUX_PROBE
        shell = "powershell" if os == "windows" else "sh"

        def _probe() -> subprocess.CompletedProcess[str]:
            done = self._exec(host, script, shell, None)
            if done.returncode != 0:
                raise SetupError(f"cannot probe {host}: {done.stderr.strip()[-200:]}")
            return done

        proc = run_with_retry(_probe, "probe")
        facts = parse_probe(host, os, proc.stdout)
        found = self.resolve_address(host) if host != CONTROL else None
        return replace(facts, address=found) if found else facts

    def resolve_address(self, host: str) -> str | None:
        """``host``'s Tailscale IPv4 as the control plane sees it, or None.

        Asked of the control plane's own client: the host's CLI can belong to another
        user and print nothing. Only a valid IPv4 first line counts, never error text.
        """
        try:
            proc = self._exec(CONTROL, f"tailscale ip -4 {shlex.quote(host)}", "sh", None)
        except (OSError, SetupError):
            return None
        first = proc.stdout.split(None, 1)[0] if proc.returncode == 0 and proc.stdout else ""
        try:
            return str(ipaddress.IPv4Address(first))
        except ValueError:
            return None

    def resources(self, host: str, os: Os) -> tuple[float, float]:
        """CPU count and memory in GB of ``host``."""
        if os == "windows":
            script = (
                "[Environment]::ProcessorCount; "
                "Add-Type -AssemblyName Microsoft.VisualBasic; [math]::Round((New-Object "
                "Microsoft.VisualBasic.Devices.ComputerInfo).TotalPhysicalMemory/1GB, 1)"
            )
        else:
            script = "nproc; awk '/MemTotal/{printf \"%.1f\\n\", $2/1048576}' /proc/meminfo"
        proc = self._exec(host, script, "powershell" if os == "windows" else "sh", None)
        try:
            cpus, mem = (float(x) for x in proc.stdout.split()[:2])
        except ValueError:
            raise SetupError(
                f"cannot read CPU and memory of {host}: {proc.stderr.strip()[-200:]}"
            ) from None
        return cpus, mem


_PROBE_LINE = re.compile(r"^(address|link|free|admin|home|task|drives)=(.*)$")


def parse_probe(name: str, os: Os, out: str) -> HostFacts:
    """Probe output: labelled ``key=value`` lines, so a missing field never shifts the rest.

    Keys: ``address`` (Tailscale, may be empty), ``link`` (network device or media types),
    ``free`` (bytes). Windows adds ``admin`` (whether the SSH account is an Administrator),
    ``home`` (its profile path), ``task`` (whether the worker task exists; ``True``/``False``)
    and ``drives`` (drive letters in use as ``letter=root`` joined by ``|``; paths may hold
    ``=`` and commas). Other lines are ignored; a missing key keeps its default.
    """
    f: dict[str, str] = {}
    for raw in out.splitlines():
        m = _PROBE_LINE.match(raw.strip())
        if m:
            f[m.group(1)] = m.group(2).strip()
    link, free = f.get("link", ""), f.get("free", "")
    wired = "802.11" not in link if os == "windows" else not link.startswith("wl")
    gb = float(free) / 1e9 if free.replace(".", "", 1).isdigit() else 0.0
    win = os == "windows"
    return HostFacts(
        name=name,
        os=os,
        address=f.get("address") or name,
        wired=wired,
        free_gb=gb,
        admin=f.get("admin", "").lower() != "false" if win else True,
        home=f.get("home", "") if win else "",
        task=f.get("task", "").lower() != "false" if win else True,
        drives=tuple(d for d in f.get("drives", "").split("|") if d.strip()) if win else (),
    )
