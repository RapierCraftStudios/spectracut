# SPDX-License-Identifier: AGPL-3.0-or-later
"""Flamenco shared storage: where it lives, how it is served, mounted and mapped.

The storage host is a user setting (``--storage-host``): the control plane, a
registered render host, or an existing NAS share (``nas://server/share``).
Every Linux machine sees the share at :data:`LINUX_PATH`, every Windows machine
at :data:`WINDOWS_DRIVE`, so the two-way ``storage`` variable is one pair of
values for the whole farm. Windows paths in Flamenco variables always use
forward slashes (Flamenco eats backslashes). Passwords travel on stdin only.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml

from spectracut.setup.manifest import HostFacts, Step
from spectracut.spec._base import StrictModel

CONTROL = "control"
SHARE = "flamenco"
LINUX_PATH = "/srv/flamenco"
WINDOWS_DRIVE = "F:"
WINDOWS_LOCAL_PATH = "C:/spectracut/storage"
LINUX_TOOLS = "/opt/spectracut/tools"
WINDOWS_TOOLS = "C:/spectracut/tools"
GPU_SCRIPT = "flamenco_enable_gpu.py"
TAILSCALE_RANGE = "100.64.0.0/10"
CRED_FILE = "/etc/spectracut-storage.cred"
MIGRATE_MOUNT = "/mnt/spectracut-migrate"
MANAGER_PORT = 8085
ADMIN_SCRIPT = "windows_storage.ps1"
WINDOWS_MARKER = f"{WINDOWS_LOCAL_PATH}/.spectracut-share"
WORKER_TASK = "spectracut-worker"
# Whether the worker task exists, via schtasks.exe: the CIM task cmdlets are
# denied to a network-logon standard user, schtasks.exe is not.
_TASK_EXISTS = f"schtasks.exe /query /tn {WORKER_TASK} *> $null; $has = $LASTEXITCODE -eq 0"


def windows_task_stop() -> str:
    """PowerShell that ends the worker task if it is registered; a missing task is a no-op."""
    return f"{_TASK_EXISTS}; if ($has) {{ schtasks.exe /end /tn {WORKER_TASK} *> $null }}"


def windows_task_start() -> str:
    """PowerShell that runs the worker task if it is registered; a missing task is a no-op."""
    return (
        f"{_TASK_EXISTS}; if ($has) {{ schtasks.exe /run /tn {WORKER_TASK} | Out-Null; "
        "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE } }; exit 0"
    )


def windows_task_restart() -> str:
    """End then run the worker task (schtasks.exe only, no admin once access is granted)."""
    return f"{windows_task_stop()}; {windows_task_start()}"


class StorageTarget(StrictModel):
    """Where the share lives: a machine we set it up on, or an existing NAS share."""

    kind: Literal["host", "nas"]
    host: str
    share: str = SHARE


@dataclass(frozen=True)
class Credential:
    user: str
    password: str = field(repr=False)


class StorageError(ValueError):
    pass


def to_flamenco_path(path: str) -> str:
    """Windows paths in Flamenco variables use forward slashes."""
    return path.replace("\\", "/")


def parse_storage_host(value: str, known_hosts: Sequence[str]) -> StorageTarget:
    """``<host>`` (the control plane or a registered host) or ``nas://server/share``."""
    if value.startswith("nas://"):
        server, _, share = value[len("nas://") :].partition("/")
        if not server or not share or "/" in share:
            raise StorageError(f"expected nas://server/share, got {value!r}")
        return StorageTarget(kind="nas", host=server, share=share)
    if value not in known_hosts:
        raise StorageError(f"unknown storage host {value!r}; choose one of {sorted(known_hosts)}")
    return StorageTarget(kind="host", host=value)


def choose_default_host(facts: Mapping[str, HostFacts]) -> StorageTarget:
    """The wired machine with the most free space; any machine if none is wired."""
    if not facts:
        raise StorageError("no machines to host storage")
    pool = [f for f in facts.values() if f.wired] or list(facts.values())
    best = max(pool, key=lambda f: (f.free_gb, f.name == CONTROL, f.name))
    return StorageTarget(kind="host", host=best.name)


def wifi_warnings(facts: Mapping[str, HostFacts], target: StorageTarget) -> list[str]:
    """Flag Wi-Fi machines: 4K EXR output over Wi-Fi measured ~2 MB/s."""
    notes = []
    for f in sorted(facts.values(), key=lambda f: f.name):
        if f.wired:
            continue
        role = "storage host" if target.kind == "host" and target.host == f.name else "machine"
        notes.append(
            f"{f.name} ({role}) is on Wi-Fi; 4K EXR output over Wi-Fi measured ~2 MB/s, "
            "prefer a wired machine for storage"
        )
    return notes


def storage_variables(
    gpu_args: bool = True, windows_drive: str = WINDOWS_DRIVE
) -> dict[str, object]:
    """The Flamenco ``variables`` block: two-way ``storage`` plus the GPU startup args."""
    variables: dict[str, object] = {
        "storage": {
            "is_twoway": True,
            "values": [
                {"platform": "linux", "value": LINUX_PATH},
                {"platform": "windows", "value": to_flamenco_path(windows_drive + "/")},
            ],
        }
    }
    if gpu_args:
        variables["blenderArgs"] = {
            "values": [
                {"platform": "linux", "value": f"--python {LINUX_TOOLS}/{GPU_SCRIPT}"},
                {
                    "platform": "windows",
                    "value": f"--python {to_flamenco_path(WINDOWS_TOOLS)}/{GPU_SCRIPT}",
                },
            ]
        }
    return variables


def manager_config(listen: str) -> str:
    """``flamenco-manager.yaml``: Tailscale-only listener, Shaman off, shared variables."""
    doc = {
        "_meta": {"version": 3},
        "manager_name": "SpectraCut",
        "database": "flamenco-manager.sqlite",
        "listen": listen,
        "shared_storage_path": LINUX_PATH,
        "shaman": {"enabled": False},
        "variables": storage_variables(),
    }
    return yaml.safe_dump(doc, sort_keys=False)


def write_file_step(
    host: str, os: str, step_id: str, summary: str, path: str, content: str, mode: str = "644"
) -> Step:
    """Write ``content`` to ``path`` on the host, content via stdin."""
    if os == "windows":
        cmd = (
            f"$d = Split-Path '{path}'; New-Item -ItemType Directory -Force $d | Out-Null; "
            f"[IO.File]::WriteAllText('{path}', [Console]::In.ReadToEnd())"
        )
        return Step(host, step_id, summary, cmd, "powershell", stdin=content)
    cmd = (
        f"d=$(dirname '{path}'); sudo mkdir -p \"$d\" && sudo tee '{path}' >/dev/null "
        f"&& sudo chmod {mode} '{path}'"
    )
    return Step(host, step_id, summary, cmd, "sh", stdin=content)


def _smb_server(target: StorageTarget, facts: Mapping[str, HostFacts]) -> str:
    return facts[target.host].address if target.kind == "host" else target.host


def windows_storage_source() -> str:
    """``windows_storage.ps1``: the one-time admin script that creates the SMB share."""
    return Path(__file__).with_name(ADMIN_SCRIPT).read_text(encoding="utf-8")


_PROFILE_DIR = (
    "$d = Join-Path $env:USERPROFILE 'spectracut'; "
    "New-Item -ItemType Directory -Force $d | Out-Null; "
)


def _windows_admin_steps(host: str, cred: Credential) -> list[Step]:
    """Write the admin script and its ACL'd password file, then wait for an admin to run it.

    Creating an SMB share and a local account needs admin, which the SSH account lacks.
    The gate step fails until the script has run; re-running ``spectracut setup`` continues.
    """
    write_script = (
        _PROFILE_DIR + f"[IO.File]::WriteAllText((Join-Path $d '{ADMIN_SCRIPT}'), "
        "[Console]::In.ReadToEnd())"
    )
    write_pw = (
        _PROFILE_DIR
        + "$f = Join-Path $d 'storage.pw'; New-Item -ItemType File -Force $f | Out-Null; "
        "icacls $f /inheritance:r /grant:r ($env:USERNAME + ':F') '*S-1-5-32-544:F' | Out-Null; "
        "[IO.File]::WriteAllText($f, [Console]::In.ReadLine())"
    )
    gate = (
        _PROFILE_DIR + f"if (-not (Test-Path '{WINDOWS_MARKER}')) {{ "
        f"$s = Join-Path $d '{ADMIN_SCRIPT}'; $f = Join-Path $d 'storage.pw'; "
        "[Console]::Error.WriteLine('run once as Administrator: powershell -ExecutionPolicy Bypass "
        f"-File ' + $s + ' -PasswordFile ' + $f + ' -User {cred.user} -Share {SHARE}"
        "; then re-run spectracut setup'); exit 1 }"
    )
    return [
        Step(
            host,
            "storage:admin-script",
            "write the one-time admin script",
            write_script,
            "powershell",
            stdin=windows_storage_source(),
        ),  # fmt: skip
        Step(
            host,
            "storage:admin-password",
            "store the share password (ACL'd file)",
            write_pw,
            "powershell",
            stdin=cred.password + "\n",
        ),  # fmt: skip
        Step(
            host,
            "storage:serve",
            "wait for the admin script to have created the share",
            gate,
            "powershell",
        ),  # fmt: skip
    ]


def storage_notes(target: StorageTarget, facts: Mapping[str, HostFacts]) -> list[str]:
    """Heads-up for a Windows storage host: an administrator must run the script once."""
    if target.kind == "host" and facts[target.host].os == "windows":
        return [
            f"{target.host} needs a one-time admin step: setup writes {ADMIN_SCRIPT} and an "
            "ACL'd password file under its profile's spectracut folder and stops until the "
            "script has been run as Administrator; then re-run setup to mount and map the share"
        ]
    return []


def _serve_steps(
    target: StorageTarget, facts: Mapping[str, HostFacts], cred: Credential
) -> list[Step]:
    """Set up the share on the chosen machine (Tailscale-only, dedicated account)."""
    host = facts[target.host]
    if host.os == "windows":
        return _windows_admin_steps(target.host, cred)
    smb_conf = (
        f"[{SHARE}]\n   path = {LINUX_PATH}\n   read only = no\n   valid users = {cred.user}\n"
        f"   hosts allow = {TAILSCALE_RANGE} 127.0.0.1\n   hosts deny = 0.0.0.0/0\n"
        "   server min protocol = SMB3\n"
    )
    return [
        write_file_step(
            target.host,
            "linux",
            "storage:smb-conf",
            "write the Samba share config",
            f"/etc/samba/spectracut.d/{SHARE}.conf",
            smb_conf,
        ),
        Step(
            target.host,
            "storage:serve",
            "serve the Samba share (Tailscale-only)",
            f"sudo mkdir -p {LINUX_PATH} && id -u {cred.user} >/dev/null 2>&1 "
            f"|| sudo useradd --system --no-create-home --shell /usr/bin/nologin {cred.user}; "
            f"sudo chown {cred.user} {LINUX_PATH}; "
            "grep -q 'spectracut.d' /etc/samba/smb.conf "
            "|| echo 'include = /etc/samba/spectracut.d/flamenco.conf' | sudo tee -a /etc/samba/smb.conf >/dev/null; "  # noqa: E501
            f'read -r pw; printf \'%s\\n%s\\n\' "$pw" "$pw" | sudo smbpasswd -s -a {cred.user}; '
            "sudo systemctl enable --now smb && sudo systemctl restart smb",
            "sh",
            stdin=cred.password + "\n",
        ),
    ]


def _attach_steps(name: str, os: str, server: str, share: str, cred: Credential) -> list[Step]:
    """Mount (Linux) or map (Windows) the share on one machine, replacing any old attachment."""
    if os == "windows":
        script = (
            "$p = [Console]::In.ReadLine(); "
            f"if (Test-Path {WINDOWS_DRIVE}) {{ Remove-SmbMapping -LocalPath {WINDOWS_DRIVE} -Force "  # noqa: E501
            "-UpdateProfile -EA SilentlyContinue }; "
            f"New-SmbMapping -LocalPath {WINDOWS_DRIVE} -RemotePath '\\\\{server}\\{share}' "
            f"-UserName '{cred.user}' -Password $p -Persistent $true | Out-Null"
        )
        return [
            Step(
                name,
                f"storage:attach:{name}",
                f"map {WINDOWS_DRIVE} to the share",
                script,
                "powershell",
                stdin=cred.password + "\n",
            )
        ]
    cred_body = f"username={cred.user}\npassword={cred.password}\n"
    return [
        write_file_step(
            name,
            "linux",
            f"storage:cred:{name}",
            "store the share credential (0600)",
            CRED_FILE,
            cred_body,
            mode="600",
        ),
        Step(
            name,
            f"storage:attach:{name}",
            f"mount the share at {LINUX_PATH}",
            f"sudo mkdir -p {LINUX_PATH}; mountpoint -q {LINUX_PATH} && sudo umount {LINUX_PATH}; "
            f"sudo mount -t cifs '//{server}/{share}' {LINUX_PATH} "
            f"-o credentials={CRED_FILE},vers=3.0,uid=$(id -u),gid=$(id -g)",
        ),
    ]


def plan_storage(
    target: StorageTarget,
    previous: StorageTarget | None,
    facts: Mapping[str, HostFacts],
    workers: Sequence[str],
    cred: Credential,
) -> list[Step]:
    """Steps that put storage on ``target`` and re-point the Manager and every worker.

    Empty when ``previous == target`` (already converged). A change migrates:
    files on the control plane's share are copied across before the re-point.
    """
    if previous == target:
        return []
    for name in (CONTROL, *workers):
        if name not in facts:
            raise StorageError(f"no facts for {name}")
    if target.kind == "host" and target.host not in facts:
        raise StorageError(f"unknown storage host {target.host!r}")
    server = _smb_server(target, facts)
    steps: list[Step] = []
    if target.kind == "host":
        steps += _serve_steps(target, facts, cred)
    control_is_storage = target.kind == "host" and target.host == CONTROL
    if previous is not None and not control_is_storage:
        steps += _attach_steps(CONTROL, "linux", server, target.share, cred)[:1]
        steps += _migrate_steps(server, target.share, cred)
    machines = [CONTROL, *[w for w in workers if w != CONTROL]]
    for name in machines:
        if name == CONTROL and control_is_storage:
            continue  # served locally at LINUX_PATH
        if name == target.host and facts[name].os == "linux" and target.kind == "host":
            continue
        steps += _attach_steps(name, facts[name].os, server, target.share, cred)
    listen = f"{facts[CONTROL].address}:{MANAGER_PORT}"
    steps.append(
        write_file_step(
            CONTROL,
            "linux",
            "storage:variables",
            "write the Manager config with the two-way storage variables",
            "$HOME/.local/opt/flamenco/flamenco-manager.yaml",
            manager_config(listen),
        )
    )
    steps.append(
        Step(
            CONTROL,
            "storage:restart-manager",
            "restart the Flamenco Manager",
            "systemctl --user restart flamenco-manager",
        )
    )
    for name in workers:
        steps.append(
            Step(
                name,
                f"storage:restart-worker:{name}",
                "restart the Flamenco Worker",
                windows_task_restart()
                if facts[name].os == "windows"
                else "systemctl --user restart spectracut-worker",
                "powershell" if facts[name].os == "windows" else "sh",
            )
        )
    unique: dict[str, Step] = {}
    for step in steps:
        unique.setdefault(step.id, step)
    return list(unique.values())


def _migrate_steps(server: str, share: str, cred: Credential) -> list[Step]:
    """Copy the control plane's current share contents into the new share before re-pointing."""
    return [
        Step(
            CONTROL,
            "storage:migrate",
            "copy existing files to the new storage",
            f"sudo mkdir -p {MIGRATE_MOUNT} && sudo mount -t cifs '//{server}/{share}' "
            f"{MIGRATE_MOUNT} -o credentials={CRED_FILE},vers=3.0,uid=$(id -u),gid=$(id -g) "
            f"&& rsync -a {LINUX_PATH}/ {MIGRATE_MOUNT}/; rc=$?; "
            f"sudo umount {MIGRATE_MOUNT}; exit $rc",
        )
    ]
