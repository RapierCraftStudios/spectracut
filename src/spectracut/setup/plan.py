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
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import partial
from os import killpg
from pathlib import Path
from typing import Protocol

from spectracut.queue.flamenco import job_type_sources
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
from spectracut.setup.model_assets import (
    ASSETS,
    BIN_DIR,
    ESSENTIAL_MODELS,
    LINUX_MODEL_ROOT,
    MODEL_ROOT_ENV,
    MODEL_ROOT_POINTER,
    SRC_DIR,
    TORCH_INDEX,
    ModelAsset,
    asset_for,
    extras_digest,
    model_version,
    reserve_gb,
    resolve_models,
)
from spectracut.setup.storage import (
    CONTROL,
    GPU_SCRIPT,
    LINUX_TOOLS,
    MANAGER_CONFIG_PATH,
    MANAGER_PORT,
    WINDOWS_DRIVE,
    WINDOWS_TOOLS,
    WORKER_TASK,
    Credential,
    StorageLayout,
    StorageTarget,
    choose_default_host,
    manager_config_steps,
    migration_notes,
    normalize_drive,
    plan_storage,
    storage_folder,
    storage_notes,
    verified_admin_command,
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
JOBTYPES_PSEUDO = "manager-jobtypes"
MANAGER_UNIT = "flamenco-manager"
# The Manager's folder when its unit sets no WorkingDirectory (setup's own install).
MANAGER_DIR_DEFAULT = "$HOME/.local/opt/flamenco"
HOST_PSEUDO = "host-profile"
HOST_REG_PSEUDO = "host-registration"
HOST_REG_REL = ".config/spectracut/host.json"
WORKER_SCRIPT = "windows_worker.ps1"
TASK_SCRIPT = "windows_worker_task.ps1"
WORKER_ARGS = "worker-args.json"
# Flamenco Worker reads its name from this variable (else it uses the OS hostname).
WORKER_NAME_ENV = "FLAMENCO_WORKER_NAME"
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
    "$id = [Security.Principal.WindowsIdentity]::GetCurrent(); "
    "'account=' + $id.Name; 'sid=' + $id.User.Value; "
    f"schtasks.exe /query /tn {WORKER_TASK} *> $null; "
    "'task=' + ($LASTEXITCODE -eq 0); "
    # Drive letters in use: letter=UNC (network), letter=folder (subst), letter=.
    "$sub = @{}; subst | ForEach-Object { if ($_ -match '^(\\w):\\\\: => (.*)$') "
    "{ $sub[$matches[1]] = $matches[2] } }; 'drives=' + ((@(Get-PSDrive -PSProvider FileSystem) "
    "| ForEach-Object { $r = if ($sub[$_.Name]) { $sub[$_.Name] } "
    "else { [string]$_.DisplayRoot }; $_.Name + '=' + $r }) -join '|')"
)
MANAGER_UNIT_PATH = "$HOME/.config/systemd/user/flamenco-manager.service"
# Missing-file tokens of the control-plane probe (``HostFacts.missing``).
MISSING_UNIT = "manager-unit"
MISSING_CONFIG = "manager-config"
MISSING_JOB_TYPES = "job-types"
# The Manager's folder: its unit's WorkingDirectory, else setup's own install (sh, sets $d).
MANAGER_DIR_LINES = (
    f"d=$(systemctl --user show -p WorkingDirectory --value {MANAGER_UNIT} 2>/dev/null || true)",
    'd="${d#[-!]}"',
    '[ "$d" != "~" ] || d="$HOME"',
    f'[ -n "$d" ] || d="{MANAGER_DIR_DEFAULT}"',
)
# Credentials a Flamenco Worker keeps after registering; a Manager with a new database
# answers them with "no such worker known" until they are deleted and the worker re-registers.
WORKER_CREDENTIALS = "flamenco-worker-credentials.yaml"
WINDOWS_CREDENTIALS = (
    f"Join-Path $env:LOCALAPPDATA 'Blender Foundation\\Flamenco\\{WORKER_CREDENTIALS}'"
)
LINUX_CREDENTIALS = f"$HOME/.local/share/flamenco/{WORKER_CREDENTIALS}"
LINUX_PROBE = (
    'echo "address=$(tailscale ip -4 2>/dev/null | head -1)"; '
    "echo \"link=$(ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<NF;i++) "
    'if($i=="dev"){print $(i+1);exit}}\')"; '
    'echo "free=$(df -Pk "$HOME" | awk \'NR==2{print $4*1024}\')"'
)


def control_probe(job_types: Sequence[str]) -> str:
    """sh printing ``missing=`` with the Manager files absent on disk (control plane only).

    Setup state records what setup ran, not what is on disk: a file lost or written
    elsewhere must still be rewritten. The job types are looked up where
    :func:`job_types_step` installs them.
    """
    files = " ".join(shlex.quote(n) for n in sorted(job_types))
    return "; ".join(
        [
            "m=",
            f'[ -f "{MANAGER_UNIT_PATH}" ] || m="$m,{MISSING_UNIT}"',
            f'[ -f "{MANAGER_CONFIG_PATH}" ] || m="$m,{MISSING_CONFIG}"',
            *MANAGER_DIR_LINES,
            f'for f in {files}; do [ -f "$d/scripts/$f" ] || m="$m,{MISSING_JOB_TYPES}"; done',
            'echo "missing=${m#,}"',
        ]
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
    # Requested by ``setup --models`` (None: essential) and ``--models-dir`` (None: the
    # host's SPECTRACUT_MODEL_ROOT or per-OS default); kept for later runs.
    models: str | None = None
    models_dir: str | None = None

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


def task_script_content(facts: HostFacts) -> str:
    """The task script with this host's account, SID and profile filled in (on the controller).

    Filled here rather than on the host so setup knows the exact bytes it writes and can
    print the SHA-256 that :func:`task_script_command` checks before running it elevated.
    """
    if not (facts.account and facts.sid and facts.home):
        raise SetupError(
            f"{facts.name}: the probe did not report the SSH account's name, SID and profile; "
            "re-run spectracut setup"
        )
    fill = {"__ACCOUNT__": facts.account, "__SID__": facts.sid, "__HOME__": facts.home}
    text = task_script_source()
    for key, value in fill.items():
        text = text.replace(key, value.replace("'", "''"))  # values sit in '...' literals
    return text


def task_script_command(facts: HostFacts) -> str:
    """The one command the owner runs in an elevated PowerShell (no secrets in it).

    It refuses a script whose SHA-256 differs from what setup wrote, and runs a copy
    outside the SSH account's profile (see :func:`verified_admin_command`).
    """
    path = f"{facts.home}\\spectracut\\{TASK_SCRIPT}"
    return verified_admin_command(path, task_script_sha256(facts))


def task_script_sha256(facts: HostFacts) -> str:
    """SHA-256 of the task script exactly as setup writes it to ``facts``' host."""
    return hashlib.sha256(task_script_content(facts).encode("utf-8")).hexdigest()


def worker_task_notes(facts: Mapping[str, HostFacts], hosts: Sequence[str]) -> list[str]:
    """One note per non-admin Windows worker: the elevated command that registers its task."""
    return [
        f"{h} needs a one-time admin step: its SSH account is not an Administrator, so after "
        f"this setup run open an elevated PowerShell on {h} and run "
        f"`{task_script_command(facts[h])}`; it refuses to run unless {TASK_SCRIPT} has the "
        f"SHA-256 {task_script_sha256(facts[h])} that setup wrote, runs a copy outside the "
        f"account's profile, and registers the {WORKER_TASK} task to run "
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


def worker_name(host: str) -> str:
    """The Flamenco worker name setup gives a registered host: the registered name itself.

    Setup sets it through :data:`WORKER_NAME_ENV` and matches Manager workers to
    hosts by this exact name when it assigns worker tags.
    """
    return host


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
    args = {
        "Manager": manager_url,
        "Drive": normalize_drive(layout.drive),
        "Share": target.share,
        "Name": worker_name(name),
    }
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
                MANAGER_UNIT_PATH,
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
    version = f"{pin}@{manager_url}#path#name"
    unit = (
        "[Unit]\nDescription=Flamenco Worker\n[Service]\nWorkingDirectory=%h/.local/opt/flamenco\n"
        "Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin\n"
        f"Environment={WORKER_NAME_ENV}={worker_name(name)}\n"
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
    task_script = task_script_content(facts)
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
        profile_write_step(
            name,
            f"worker-task-script:{name}",
            "install the one-time Worker task script",
            f"spectracut/{TASK_SCRIPT}",
            task_script,
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


def worker_credentials_reset_step(f: HostFacts) -> Step:
    """Stop the worker, delete its Flamenco credentials and start it, so it registers anew.

    For a worker the Manager does not know (its database is new): the old credentials
    only get ``403 no such worker known``. No admin or sudo; no component, never recorded.
    """
    stop, start = _worker_stop_start(f)
    if f.os == "windows":
        delete = (
            f"Remove-Item -LiteralPath ({WINDOWS_CREDENTIALS}) -Force -ErrorAction SilentlyContinue"
        )
    else:
        delete = f'rm -f "{LINUX_CREDENTIALS}"'
    return Step(
        f.name,
        f"worker:reset-credentials:{f.name}",
        "reset the Flamenco Worker credentials (the Manager does not know it)",
        f"{stop.command}; {delete}; {start.command}",
        stop.shell,
        kind="task",
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
    wheels copied to the host. Copies come before the worker stop, so a stalled copy
    never leaves a worker down.
    """
    steps: list[Step] = []
    copies: list[Step] = []
    fields = install_fields(f.os, wheels)
    stop_at: int | None = None
    for cname, comp in manifest.components.items():
        if not roles & set(comp.roles) or f.os not in comp.os:
            continue
        if have.get(cname) == pins[cname]:
            continue
        if stop_at is None and cname in WORKER_FILES and "host" in roles:
            stop_at = len(steps)
        if pins[cname] == wheels.pin and not copies:
            copies = wheel_copy_steps(f, wheels)
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
    return copies + steps


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


def job_types_version(sources: Mapping[str, str]) -> str:
    """Content hash of the packaged job types: a changed script gives a new version."""
    digest = hashlib.sha256()
    for name in sorted(sources):
        digest.update(f"{name}\0{sources[name]}\0".encode())
    return digest.hexdigest()[:12]


def job_types_step(host: str, sources: Mapping[str, str]) -> Step:
    """Install the custom job types into the Manager's ``scripts/`` folder; no sudo.

    Flamenco loads custom job types from ``scripts/`` next to ``flamenco-manager``,
    which is the Manager unit's WorkingDirectory (a hand-installed Manager may live
    elsewhere than setup's own install). A file is replaced only when its content
    differs, and the Manager is restarted only when a file changed.
    """
    lines = [
        "set -e",
        *MANAGER_DIR_LINES,
        's="$d/scripts"',
        'mkdir -p "$s"',
        "changed=",
        't=$(mktemp "$s/.spectracut.XXXXXX")',
    ]
    for name in sorted(sources):
        b64 = base64.b64encode(sources[name].encode("utf-8")).decode("ascii")
        lines += [
            f"printf '%s' '{b64}' | base64 -d > \"$t\"",
            f'if ! cmp -s "$t" "$s/{name}"; then chmod 644 "$t"; mv -f "$t" "$s/{name}"; '
            'changed=1; t=$(mktemp "$s/.spectracut.XXXXXX"); fi',
        ]
    lines += [
        'rm -f "$t"',
        f'if [ -n "$changed" ]; then systemctl --user restart {MANAGER_UNIT}; fi',
    ]
    return Step(
        host,
        "manager:job-types",
        f"install the Flamenco job types ({', '.join(sorted(sources))})",
        "; ".join(lines),
        component=JOBTYPES_PSEUDO,
        version=job_types_version(sources),
        kind="write",
    )


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


MODELS_DEPS = "models-deps"
"""State component of the family extras installed into the spectracut-host tool venv."""
MODEL_PREFIX = "model:"
MODELS_ROOT = "models-root"
"""State component: the ``--models-dir`` recorded in the model host's pointer file."""
"""State component prefix of one model's weights (``model:<model id>``)."""
DISK_MARGIN_GB = 2.0
_WINDOWS_ROOT_PS = "$env:LOCALAPPDATA/spectracut/models"
DEPS_TIMEOUT = 7200.0
"""Seconds for the family extras install (CUDA torch alone is a few GB)."""
SECONDS_PER_GB = 4096.0
"""Weights download allowance per GB: 256 KiB/s, below legion's slowest measured link."""


def models_deps_version(host_pin: str) -> str:
    """The models-deps state version: the spectracut-host pin plus the extras digest."""
    return f"{host_pin}+{extras_digest()}"


def _models_deps_step(f: HostFacts, wheels: LocalWheels, host_pin: str) -> Step:
    """Reinstall spectracut-host with every family extra and CUDA torch (cu128 index)."""
    fields = install_fields(f.os, wheels)
    extras = ",".join(a.extra for a in ASSETS.values())
    args = (
        "tool install --force --python 3.12 --python-preference only-managed "
        f'--find-links "{fields["wheel_dir"]}" --with "{fields["spectracut_wheel"]}" '
        f"--index {TORCH_INDEX} --index-strategy unsafe-best-match "
        f'"{fields["host_wheel"]}[{extras}]"'
    )
    if f.os == "windows":
        cmd = (
            "$ErrorActionPreference = 'Stop'; "
            f"& (Join-Path $env:USERPROFILE '.local/bin/uv.exe') {args}; "
            "if ($LASTEXITCODE -ne 0) "
            '{ throw "installing the model families failed ($LASTEXITCODE)" }'
        )
    else:
        cmd = f'set -e; "$HOME/.local/bin/uv" {args}'
    return Step(
        f.name,
        f"models:deps:{f.name}",
        "install the local model families into spectracut-host",
        cmd,
        "powershell" if f.os == "windows" else "sh",
        component=MODELS_DEPS,
        version=models_deps_version(host_pin),
        kind="install",
        timeout=DEPS_TIMEOUT,
    )


def _need_kib(asset: ModelAsset) -> int:
    return int((asset.size_gb + DISK_MARGIN_GB) * 1024 * 1024)


def _sh_root(models_dir: str | None) -> str:
    """Sets ``$root``: ``models_dir`` when given, else the env var or the Linux default."""
    if models_dir is not None:
        return f"root={shlex.quote(models_dir)}"
    return f'root="${{{MODEL_ROOT_ENV}:-{LINUX_MODEL_ROOT}}}"'


def _ps_root(models_dir: str | None) -> str:
    """Sets ``$root`` in PowerShell (``models_dir`` has no quotes: normalize_storage_path)."""
    if models_dir is not None:
        return f"$root = '{models_dir}'"
    return (
        f"$root = if ($env:{MODEL_ROOT_ENV}) {{ $env:{MODEL_ROOT_ENV} }} "
        f'else {{ "{_WINDOWS_ROOT_PS}" }}'
    )


def _sh_weights(model_id: str, asset: ModelAsset, models_dir: str | None = None) -> str:
    need = asset.size_gb + DISK_MARGIN_GB
    lines = [
        f"set -e; {_sh_root(models_dir)}",
        f'd="$root/{asset.family}/{model_id}"; mkdir -p "$d"',
        "avail=$(df -Pk \"$d\" | awk 'NR==2 {print $4}')",
        f'if [ "$avail" -lt {_need_kib(asset)} ]; then echo "not enough disk for {model_id}: '
        f'need {need:g} GB free at $d, have $((avail / 1048576)) GB" >&2; exit 1; fi',
        'hf="$("$HOME/.local/bin/uv" tool dir)/spectracut-host/bin/hf"',
    ]
    for w in asset.weights:
        inc = "".join(f" --include {shlex.quote(p)}" for p in w.include)
        lines.append(
            f'"$hf" download {w.repo} --revision {w.revision} --local-dir "$d/{w.subdir}"{inc}'
        )
    if asset.git_repo:
        src = f'"$d/{SRC_DIR}"'
        lines += [
            f"[ -d {src}/.git ] || git clone --quiet {asset.git_repo} {src}",
            f"git -C {src} fetch --quiet origin {asset.git_commit}",
            f"git -C {src} checkout --quiet --detach {asset.git_commit}",
        ]
    return "; ".join(lines)


def _ps_weights(model_id: str, asset: ModelAsset, models_dir: str | None = None) -> str:
    need = asset.size_gb + DISK_MARGIN_GB
    check = 'if ($LASTEXITCODE -ne 0) {{ throw "{what} failed ($LASTEXITCODE)" }}'
    lines = [
        "$ErrorActionPreference = 'Stop'",
        _ps_root(models_dir),
        f"$d = Join-Path $root '{asset.family}/{model_id}'",
        "New-Item -ItemType Directory -Force $d | Out-Null",
        "$free = (Get-PSDrive (Get-Item $d).PSDrive.Name).Free",
        f'if ($free -lt {_need_kib(asset) * 1024}) {{ throw "not enough disk for {model_id}: '
        f'need {need:g} GB free at $d, have $([math]::Floor($free / 1GB)) GB" }}',
        "$uv = Join-Path $env:USERPROFILE '.local/bin/uv.exe'",
        "$hf = Join-Path (& $uv tool dir | Out-String).Trim() 'spectracut-host/Scripts/hf.exe'",
    ]
    for w in asset.weights:
        inc = "".join(f" --include '{p}'" for p in w.include)
        lines += [
            f"& $hf download {w.repo} --revision {w.revision} "
            f"--local-dir (Join-Path $d '{w.subdir}'){inc}",
            check.format(what=f"downloading {w.repo}"),
        ]
    if asset.git_repo:
        lines += [
            f"$src = Join-Path $d '{SRC_DIR}'",
            f"if (-not (Test-Path (Join-Path $src '.git'))) "
            f"{{ git clone --quiet {asset.git_repo} $src; "
            + check.format(what=f"cloning {asset.git_repo}")
            + " }",
            f"git -C $src fetch --quiet origin {asset.git_commit}",
            check.format(what=f"fetching {asset.git_commit}"),
            f"git -C $src checkout --quiet --detach {asset.git_commit}",
            check.format(what=f"checking out {asset.git_commit}"),
        ]
    if asset.windows_archives:
        lines += [
            f"$bin = Join-Path $d '{BIN_DIR}'",
            "New-Item -ItemType Directory -Force $bin | Out-Null",
        ]
        for url in asset.windows_archives:
            lines += [
                "$z = Join-Path $env:TEMP 'spectracut-model.zip'",
                "if (Test-Path $z) { Remove-Item -Force $z }",
                f"Invoke-WebRequest '{url}' -OutFile $z -UseBasicParsing",
                "Expand-Archive -Force $z $bin; Remove-Item $z",
            ]
    return "; ".join(lines)


def model_weights_step(f: HostFacts, model_id: str, models_dir: str | None = None) -> Step:
    """Pull ``model_id``'s pinned weights (and source) onto ``f`` after a free-disk check.

    A rerun after an interrupted download resumes into the same folder.
    """
    asset = asset_for(model_id)
    windows = f.os == "windows"
    return Step(
        f.name,
        f"models:weights:{model_id}:{f.name}",
        f"download {model_id} weights (~{asset.size_gb:g} GB)",
        _ps_weights(model_id, asset, models_dir)
        if windows
        else _sh_weights(model_id, asset, models_dir),
        "powershell" if windows else "sh",
        component=f"{MODEL_PREFIX}{model_id}",
        version=model_version(model_id),
        kind="install",
        timeout=1800.0 + asset.size_gb * SECONDS_PER_GB,
    )


@dataclass(frozen=True)
class ModelDisk:
    """The model root's disk on the model host: free and total GB, model folders present."""

    root: str
    free_gb: float
    total_gb: float | None = None
    present: Mapping[str, float] = field(default_factory=dict)
    """Model id -> GB already under ``<root>/<family>/<id>`` (complete or partial)."""


def model_disk_step(f: HostFacts, models_dir: str | None = None) -> Step:
    """Read-only: free/total bytes at the model root (nearest existing folder), its models."""
    if f.os == "windows":
        cmd = "; ".join(
            [
                "$ErrorActionPreference = 'Stop'",
                _ps_root(models_dir),
                "$p = $root",
                "while (-not (Test-Path $p)) { $p = Split-Path $p -Parent }",
                "$v = Get-PSDrive (Get-Item $p).PSDrive.Name",
                "'root=' + $root; 'free=' + $v.Free; 'total=' + ($v.Free + $v.Used)",
                "$d = @()",
                "if (Test-Path $root) { foreach ($a in Get-ChildItem $root -Directory) "
                "{ foreach ($m in Get-ChildItem $a.FullName -Directory) "
                "{ $s = (Get-ChildItem $m.FullName -Recurse -File -Force -ErrorAction "
                "SilentlyContinue | Measure-Object Length -Sum).Sum; if (-not $s) { $s = 0 }; "
                "$d += $a.Name + '/' + $m.Name + ':' + $s } } }",
                "'dirs=' + ($d -join '|')",
            ]
        )
    else:
        cmd = "; ".join(
            [
                _sh_root(models_dir),
                'p="$root"; while [ ! -d "$p" ]; do p=$(dirname "$p"); done',
                'echo "root=$root"',
                "df -Pk \"$p\" | awk 'NR==2 {print \"free=\" $4*1024; print \"total=\" $2*1024}'",
                'd=""; for x in "$root"/*/*/; do [ -d "$x" ] || continue; r="${x%/}"; '
                'r="${r#"$root"/}"; d="$d|$r:$(( $(du -sk "$x" | cut -f1) * 1024 ))"; done',
                'echo "dirs=${d#|}"',
            ]
        )
    return Step(
        f.name,
        f"models:disk:{f.name}",
        "measure the model root's disk",
        cmd,
        "powershell" if f.os == "windows" else "sh",
        artifact="model-disk",
        kind="probe",
    )


def parse_model_disk(f: HostFacts, out: str | None) -> ModelDisk:
    """:func:`model_disk_step` output; a missing free falls back to the host probe's."""
    kv: dict[str, str] = {}
    for raw in (out or "").splitlines():
        key, sep, value = raw.strip().partition("=")
        if sep and key in ("root", "free", "total", "dirs"):
            kv[key] = value.strip()

    def gb(value: str) -> float | None:
        return float(value) / 1e9 if value.replace(".", "", 1).isdigit() else None

    ids = {f"{a.family}/{m}": m for a in ASSETS.values() for m in a.model_ids}
    present: dict[str, float] = {}
    for item in kv.get("dirs", "").split("|"):
        rel, _, size = item.strip().rpartition(":")
        if rel in ids and (g := gb(size)) is not None:
            present[ids[rel]] = g
    free = gb(kv.get("free", ""))
    return ModelDisk(
        kv.get("root", ""), f.free_gb if free is None else free, gb(kv.get("total", "")), present
    )


def probe_model_disk(runner: StepRunner, f: HostFacts, models_dir: str | None = None) -> ModelDisk:
    """Measure the model root's disk on ``f`` (read-only; changes nothing)."""
    return parse_model_disk(f, runner.run(model_disk_step(f, models_dir)))


def _models_root_step(f: HostFacts, models_dir: str) -> Step:
    """Write the pointer file the host's ``default_model_root()`` reads (``--models-dir``)."""
    if f.os == "windows":
        cmd = (
            "$ErrorActionPreference = 'Stop'; "
            "$p = Join-Path $env:LOCALAPPDATA 'spectracut'; "
            "New-Item -ItemType Directory -Force $p | Out-Null; "
            f"Set-Content -Encoding utf8 -Path (Join-Path $p '{MODEL_ROOT_POINTER}') "
            f"-Value '{models_dir}'"
        )
    else:
        cmd = (
            'set -e; mkdir -p "$HOME/.config/spectracut"; '
            f"printf '%s\\n' {shlex.quote(models_dir)} "
            f'> "$HOME/.config/spectracut/{MODEL_ROOT_POINTER}"'
        )
    return Step(
        f.name,
        f"models:root:{f.name}",
        f"keep model weights under {models_dir}",
        cmd,
        "powershell" if f.os == "windows" else "sh",
        component=MODELS_ROOT,
        version=models_dir,
        kind="write",
    )


def _models_clean_step(f: HostFacts, model_id: str, models_dir: str | None) -> Step:
    """Remove the unrecorded (partial) folder of a model that is no longer selected."""
    rel = f"{asset_for(model_id).family}/{model_id}"
    if f.os == "windows":
        cmd = (
            f"$ErrorActionPreference = 'Stop'; {_ps_root(models_dir)}; "
            f"$d = Join-Path $root '{rel}'; "
            "if (Test-Path $d) { Remove-Item -Recurse -Force $d }"
        )
    else:
        cmd = f'set -e; {_sh_root(models_dir)}; rm -rf -- "${{root:?}}/{rel}"'
    return Step(
        f.name,
        f"models:clean:{model_id}:{f.name}",
        f"remove the unfinished {model_id} download (not selected)",
        cmd,
        "powershell" if f.os == "windows" else "sh",
    )


def model_steps(
    f: HostFacts,
    have: Mapping[str, str],
    wheels: LocalWheels,
    *,
    reinstalled: bool,
    models: Sequence[str] = ESSENTIAL_MODELS,
    disk: ModelDisk | None = None,
    models_dir: str | None = None,
    skip: bool = False,
) -> tuple[list[Step], list[str]]:
    """The model host's deps step, then pointer, cleanup and weights steps it lacks.

    The deps step reruns when its version changed or spectracut-host is reinstalled in
    this plan (a reinstall drops the extras); ``skip`` plans nothing else. Weights are
    planned for the selected ``models`` not yet recorded, and only when their size (less
    what is already on disk) fits under free space plus the unfinished folders of
    unselected models (removed first) minus :func:`reserve_gb`; else :class:`ModelDiskError`.
    """
    steps: list[Step] = []
    deps = _models_deps_step(f, wheels, wheels.pin)
    if reinstalled or have.get(MODELS_DEPS) != deps.version:
        steps.append(deps)
    if skip:
        return steps, []
    if models_dir is not None and have.get(MODELS_ROOT) != models_dir:
        steps.append(_models_root_step(f, models_dir))
    disk = disk or ModelDisk(models_dir or "", f.free_gb)
    missing = [m for m in models if have.get(f"{MODEL_PREFIX}{m}") != model_version(m)]
    orphans = [m for m in disk.present if m not in models and f"{MODEL_PREFIX}{m}" not in have]
    need = sum(max(0.0, asset_for(m).size_gb - disk.present.get(m, 0.0)) for m in missing)
    reclaim = sum(disk.present[m] for m in orphans)
    reserve = reserve_gb(disk.total_gb)
    avail = disk.free_gb + reclaim - reserve
    if missing and need > avail:
        where = f" at {disk.root}" if disk.root else ""
        raise ModelDiskError(
            f"{f.name}: the selected model weights ({', '.join(missing)}) need about "
            f"{need:.1f} GB{where}, but {disk.free_gb:.1f} GB is free"
            + (f" (+{reclaim:.1f} GB of unfinished downloads)" if reclaim else "")
            + f" and {reserve:g} GB is kept in reserve: {need - avail:.1f} GB short. "
            "Nothing was downloaded. Options: --models essential|<ids> to install fewer "
            "models, --models-dir <path> to put the weights on a disk with more room, or "
            "--skip-models to update hosts and runtimes without weights"
        )
    steps += [_models_clean_step(f, m, models_dir) for m in orphans]
    steps += [model_weights_step(f, m, models_dir) for m in missing]
    return steps, []


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
    model_host: str | None = None,
    models: Sequence[str] | None = None,
    model_disk: ModelDisk | None = None,
    skip_models: bool = False,
) -> Plan:
    """Steps still needed to bring every machine to ``set_id`` with storage on ``storage``.

    ``storage`` defaults to the target already in ``state``, else the wired
    machine with the most free space. With ``signing_public_key`` every worker gets
    a ``host.json`` registering it under its registered name with that key. With
    ``model_host`` (a probed worker) the model families and pinned weights are installed
    there too, after every other step: ``models`` (default: ``state.models``, else the
    essential set) under ``state.models_dir``, checked against ``model_disk`` (else the
    host probe's free space); ``skip_models`` installs the families but no weights.
    Raises :class:`ModelDiskError` when the weights do not fit.
    """
    target = storage or state.storage or choose_default_host(facts)
    layout = state.layout()
    storage_host = target.host if target.kind == "host" else None
    pins = _pins(manifest, set_id, wheels)
    manager_url = f"http://{facts[CONTROL].address}:{MANAGER_PORT}"
    gpu_src = gpu_script_source()
    gpu_hash = hashlib.sha256(gpu_src.encode("utf-8")).hexdigest()[:12]
    worker_src = worker_script_source() + task_script_source()
    job_types = job_type_sources()
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
        lost = name == CONTROL and MISSING_UNIT in f.missing
        if lost or any(have.get(k) != v for k, v in want.items()):
            steps += svc
            if name in workers and f.os == "windows" and not f.admin:
                need_task.append(name)
        elif name in workers and f.os == "windows" and not f.admin and not f.task:
            need_task.append(name)
        if name == CONTROL and (
            MISSING_JOB_TYPES in f.missing
            or have.get(JOBTYPES_PSEUDO) != job_types_version(job_types)
        ):
            steps.append(job_types_step(name, job_types))
    storage_steps = plan_storage(
        target, state.storage, facts, workers, cred, layout, state.storage_layout
    )
    if MISSING_CONFIG in facts[CONTROL].missing and not storage_steps:
        storage_steps = manager_config_steps(facts, layout)
    steps += storage_steps
    model_notes: list[str] = []
    if model_host is not None and model_host in workers and model_host in facts:
        reinstall = f"install:spectracut-host:{model_host}"
        msteps, model_notes = model_steps(
            facts[model_host],
            state.hosts.get(model_host, HostState()).components,
            wheels,
            reinstalled=any(s.id == reinstall for s in steps),
            models=resolve_models(state.models or "essential") if models is None else models,
            disk=model_disk,
            models_dir=state.models_dir,
            skip=skip_models,
        )
        steps += msteps
    return Plan(
        set_id,
        tuple(steps),
        target,
        tuple(
            storage_notes(target, facts)
            + migration_notes(target, state.storage, facts, state.storage_layout)
            + wifi_warnings(facts, target)
            + worker_task_notes(facts, need_task)
            + model_notes
        ),
        layout,
    )


def probe_hosts(
    runner: StepRunner, names: Sequence[str], *, required: Collection[str]
) -> tuple[dict[str, HostFacts], dict[str, str]]:
    """Probe each host; an unreachable one is skipped unless it is ``required``.

    Returns the facts of every reachable host and ``name -> reason`` for each skipped
    one. A probe failure (:class:`SetupError`, a timeout included) of a required host
    is raised unchanged: the control plane and the storage host must be reachable.
    """
    facts: dict[str, HostFacts] = {}
    skipped: dict[str, str] = {}
    for name in names:
        try:
            facts[name] = runner.probe(name)
        except SetupError as exc:
            if name in required:
                raise
            skipped[name] = str(exc)
    return facts, skipped


def _run_steps(
    steps: Sequence[Step],
    runner: StepRunner,
    state: SetupState,
    home: Path | None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    starts = {s.id: s for s in steps if s.id.startswith(START_PREFIX)}
    pending: dict[str, Step] = {}  # worker:start steps for workers stopped and not yet started
    try:
        _run_each(steps, runner, state, home, sleep, starts, pending)
    except Exception as exc:
        failed: list[str] = []
        for start in list(pending.values()):
            try:
                run_with_retry(partial(runner.run, start), start.kind, sleep)
            except SetupError as again:
                failed.append(str(again))
        if failed:
            message = f"{exc}; and restarting the worker failed: {'; '.join(failed)}"
            combined = SetupError(message)
            if isinstance(exc, SetupError):
                combined.host, combined.summary = exc.host, exc.summary
            raise combined from exc
        raise


STOP_PREFIX = "worker:stop:"
START_PREFIX = "worker:start:"


def _run_each(
    steps: Sequence[Step],
    runner: StepRunner,
    state: SetupState,
    home: Path | None,
    sleep: Callable[[float], None],
    starts: Mapping[str, Step],
    pending: dict[str, Step],
) -> None:
    for step in steps:
        if step.id.startswith(STOP_PREFIX):
            start = starts.get(START_PREFIX + step.id.removeprefix(STOP_PREFIX))
            if start is not None:
                pending[start.id] = start
        try:
            out = run_with_retry(partial(runner.run, step), step.kind, sleep)
        except SetupError as exc:
            exc.host, exc.summary = step.host, step.summary
            raise
        pending.pop(step.id, None)
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

    A worker stopped for an upgrade is started again before the failure is raised.

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
    """A setup failure; ``host``/``summary`` name the step that failed once a run attributes it."""

    host: str | None = None
    summary: str | None = None


class ModelDiskError(SetupError):
    """The selected model weights do not fit under the model root's free space and reserve."""


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

    def step_timeout(self, step: Step) -> float:
        """The step's own timeout (scaled to its size) unless the runner overrides all."""
        if self.timeout is None and step.timeout is not None:
            return step.timeout
        return self.timeout_for(step.kind)

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
            self.step_timeout(step),
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
            if host == CONTROL:
                script = f"{script}; {control_probe(list(job_type_sources()))}"
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


_PROBE_LINE = re.compile(r"^(address|link|free|admin|home|account|sid|task|drives|missing)=(.*)$")
_MISSING = (MISSING_UNIT, MISSING_CONFIG, MISSING_JOB_TYPES)


def _valid_address(value: str, fallback: str) -> str:
    """The probed address only when it is an IP: remote output never reaches a shell as text."""
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return fallback


def parse_probe(name: str, os: Os, out: str) -> HostFacts:
    """Probe output: labelled ``key=value`` lines, so a missing field never shifts the rest.

    Keys: ``address`` (Tailscale, may be empty), ``link`` (network device or media types),
    ``free`` (bytes). Windows adds ``admin`` (whether the SSH account is an Administrator),
    ``home`` (its profile path), ``account`` and ``sid`` (its name and SID), ``task``
    (whether the worker task exists; ``True``/``False``)
    and ``drives`` (drive letters in use as ``letter=root`` joined by ``|``; paths may hold
    ``=`` and commas). The control plane adds ``missing`` (comma-separated Manager files
    absent on disk; unknown tokens ignored). Other lines are ignored; a missing key keeps
    its default.
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
        address=_valid_address(f.get("address", ""), name),
        wired=wired,
        free_gb=gb,
        admin=f.get("admin", "").lower() != "false" if win else True,
        home=f.get("home", "") if win else "",
        account=f.get("account", "") if win else "",
        sid=f.get("sid", "") if win else "",
        task=f.get("task", "").lower() != "false" if win else True,
        drives=tuple(d for d in f.get("drives", "").split("|") if d.strip()) if win else (),
        missing=tuple(
            dict.fromkeys(
                m for m in (x.strip() for x in f.get("missing", "").split(",")) if m in _MISSING
            )
        ),
    )
