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
import re
import shlex
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from spectracut.queue.profile import RenderProfile
from spectracut.setup.manifest import HostFacts, Manifest, Os, Role, Shell, Step, StepRunner
from spectracut.setup.storage import (
    CONTROL,
    GPU_SCRIPT,
    LINUX_TOOLS,
    MANAGER_PORT,
    WINDOWS_DRIVE,
    WINDOWS_LOCAL_PATH,
    WINDOWS_TOOLS,
    Credential,
    StorageTarget,
    choose_default_host,
    plan_storage,
    storage_notes,
    wifi_warnings,
    write_file_step,
)
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
WORKER_SCRIPT = "windows_worker.ps1"
PROFILE_DIR = "profiles"
LINUX_BIN = "$HOME/.local/bin"
WORKER_TASK = "spectracut-worker"
# Components whose files the running worker holds open: replaced only with the worker stopped.
WORKER_FILES = ("flamenco-worker", "spectracut-host")
SSH_ERROR = 255


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


def worker_script_source() -> str:
    """``windows_worker.ps1``: the per-user logon script every Windows worker runs."""
    return Path(__file__).with_name(WORKER_SCRIPT).read_text(encoding="utf-8")


def profile_write_step(host: str, step_id: str, summary: str, rel: str, content: str) -> Step:
    """Write ``content`` (stdin) to ``rel`` under the Windows account's profile."""
    cmd = (
        f"$p = Join-Path $env:USERPROFILE '{rel}'; "
        "New-Item -ItemType Directory -Force (Split-Path $p) | Out-Null; "
        "[IO.File]::WriteAllText($p, [Console]::In.ReadToEnd())"
    )
    return Step(host, step_id, summary, cmd, "powershell", stdin=content)


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
    manager_url: str, target: StorageTarget, facts: Mapping[str, HostFacts], name: str, user: str
) -> str:
    """Arguments of ``windows_worker.ps1``: storage drive, share and the account that maps it."""
    server = facts[target.host].address if target.kind == "host" else target.host
    args = f'-Manager "{manager_url}" -Drive "{WINDOWS_DRIVE}" -Share "{target.share}"'
    if target.kind == "host" and target.host == name:
        return f'{args} -LocalPath "{WINDOWS_LOCAL_PATH}"'
    return f'{args} -Server "{server}" -User "{user}"'


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
        # A per-user logon task needs no admin; at-startup tasks do.
        cmd = (
            f"$s = Join-Path $env:USERPROFILE 'spectracut/{WORKER_SCRIPT}'; "
            "Unregister-ScheduledTask -TaskName spectracut-worker -Confirm:$false "
            "-ErrorAction SilentlyContinue; "
            "Register-ScheduledTask -TaskName spectracut-worker -Force "
            "-Action (New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "
            "('-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File \"' + $s + '\" "
            f"{worker_args}')) "
            '-Trigger (New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\\$env:USERNAME") '
            "-Settings (New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries "
            "-DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Seconds 0)) "
            "| Out-Null; Start-ScheduledTask -TaskName spectracut-worker"
        )
        return [
            Step(
                name,
                f"service:worker:{name}",
                "register the Worker logon task",
                cmd,
                "powershell",
                component=SERVICE_PSEUDO,
                version=f"{pin}@{manager_url}#{script_hash}",
            )
        ]
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
            f"Stop-ScheduledTask -TaskName {WORKER_TASK} -ErrorAction SilentlyContinue; "
            "$root = Join-Path $env:USERPROFILE ''; "
            "$p = @(Get-Process -Name flamenco-worker, spectracut-host -ErrorAction "
            "SilentlyContinue | Where-Object { $_.Path -and $_.Path.StartsWith($root) }); "
            "$p | Stop-Process -Force -ErrorAction SilentlyContinue; "
            "$p | ForEach-Object { $_.WaitForExit(15000) | Out-Null }; $true | Out-Null"
        )
        start = (
            f"if (Get-ScheduledTask -TaskName {WORKER_TASK} -ErrorAction SilentlyContinue) "
            f"{{ Start-ScheduledTask -TaskName {WORKER_TASK} }}"
        )
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
            f.name, f"worker:stop:{f.name}", "stop the Flamenco Worker for the upgrade", stop, shell
        ),
        Step(f.name, f"worker:start:{f.name}", "start the Flamenco Worker again", start, shell),
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
) -> Plan:
    """Steps still needed to bring every machine to ``set_id`` with storage on ``storage``.

    ``storage`` defaults to the target already in ``state``, else the wired
    machine with the most free space.
    """
    target = storage or state.storage or choose_default_host(facts)
    storage_host = target.host if target.kind == "host" else None
    pins = _pins(manifest, set_id, wheels)
    manager_url = f"http://{facts[CONTROL].address}:{MANAGER_PORT}"
    gpu_src = gpu_script_source()
    gpu_hash = hashlib.sha256(gpu_src.encode("utf-8")).hexdigest()[:12]
    worker_src = worker_script_source()
    steps: list[Step] = []
    for name in [CONTROL, *workers]:
        f = facts[name]
        have = state.hosts.get(name, HostState()).components
        roles = _roles(name, storage_host)
        steps += _install_steps(manifest, pins, f, have, roles, wheels)
        if name == CONTROL:
            svc = _service_steps(f, manager_url, pins["flamenco-manager"])
        elif name in workers:
            steps += _worker_prep_steps(f, have, pins, gpu_src, gpu_hash)
            args = _worker_args(manager_url, target, facts, name, cred.user)
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
    steps += plan_storage(target, state.storage, facts, workers, cred)
    return Plan(
        set_id,
        tuple(steps),
        target,
        tuple(storage_notes(target, facts) + wifi_warnings(facts, target)),
    )


def _run_steps(
    steps: Sequence[Step], runner: StepRunner, state: SetupState, home: Path | None
) -> None:
    for step in steps:
        out = runner.run(step)
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
    plan: Plan, runner: StepRunner, state: SetupState, home: Path | None = None
) -> SetupState:
    """Run the steps in order; record each success. A failure stops the run (state kept)."""
    _run_steps(plan.steps, runner, state, home)
    state = state.model_copy(update={"set_id": plan.set_id, "storage": plan.storage})
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
    _run_steps(steps, runner, state, home)
    path = (Path(home) if home is not None else spectracut_home()) / PROFILE_DIR
    try:
        profile = RenderProfile.model_validate_json((path / f"{alias}.json").read_bytes())
    except (OSError, ValueError) as exc:
        raise SetupError(f"{alias}: cannot read the render profile: {exc}") from exc
    cpus, mem_gb = runner.resources(alias, facts.os)
    return Bootstrapped(profile, cpus, mem_gb)


class SetupError(RuntimeError):
    pass


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

    def __init__(self, timeout: float = 1800.0) -> None:
        self.timeout = timeout

    def _exec(
        self, host: str, cmd: str, shell: str, stdin: str | None
    ) -> subprocess.CompletedProcess[str]:
        if shell == "powershell":
            cmd = f"$ProgressPreference = 'SilentlyContinue'; {cmd}"
            enc = base64.b64encode(cmd.encode("utf-16-le")).decode("ascii")
            remote = f"powershell -NoProfile -NonInteractive -EncodedCommand {enc}"
        else:
            remote = f"sh -c {shlex.quote(cmd)}"
        argv = ["sh", "-c", cmd] if host == CONTROL else ["ssh", host, remote]
        return subprocess.run(
            argv, input=stdin, capture_output=True, text=True, timeout=self.timeout, check=False
        )

    def run(self, step: Step) -> str | None:
        proc = self._exec(step.host, step.command, step.shell, step.stdin)
        if proc.returncode != 0:
            tail = _error_lines(proc.stderr or proc.stdout)[-1:] or [""]
            raise SetupError(f"{step.host}: {step.summary} failed ({proc.returncode}): {tail[0]}")
        return proc.stdout if step.artifact else None

    def probe(self, host: str) -> HostFacts:
        os: Os = "linux"
        if host != CONTROL:
            first = self._exec(host, "uname -s", "sh", None)
            if first.returncode == SSH_ERROR:
                raise SetupError(
                    f"cannot reach {host} over SSH: {first.stderr.strip()[-200:] or 'no output'}"
                )
            if first.returncode != 0:
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

    def resources(self, host: str, os: Os) -> tuple[float, float]:
        """CPU count and memory in GB of ``host``."""
        if os == "windows":
            script = (
                "[Environment]::ProcessorCount; "
                "[math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory/1GB, 1)"
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


def parse_probe(name: str, os: Os, out: str) -> HostFacts:
    """Probe output: Tailscale address, network device or media type, free bytes."""
    lines = [ln.strip() for ln in out.splitlines()]
    lines += [""] * (3 - len(lines))
    address, link, free = lines[:3]
    wired = "802.11" not in link if os == "windows" else not link.startswith("wl")
    gb = float(free) / 1e9 if free.replace(".", "", 1).isdigit() else 0.0
    return HostFacts(name=name, os=os, address=address or name, wired=wired, free_gb=gb)
