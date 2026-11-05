# SPDX-License-Identifier: AGPL-3.0-or-later
"""Flamenco shared storage: where it lives, how it is served, mounted and mapped.

The storage host is a user setting (``--storage-host``): the control plane, a
registered render host, or an existing NAS share (``nas://server/share``).
Every Linux machine sees the share at :data:`LINUX_PATH`, every Windows machine
at one drive letter (``--windows-drive``, default :data:`WINDOWS_DRIVE`), so the
two-way ``storage`` variable is one pair of values for the whole farm. The folder
the storage host serves is ``--storage-path`` (default :data:`WINDOWS_LOCAL_PATH`
or :data:`LINUX_PATH`); see :class:`StorageLayout`. Windows paths in Flamenco variables always use
forward slashes (Flamenco eats backslashes). Passwords travel on stdin only.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml

from spectracut.setup.manifest import HostFacts, Step
from spectracut.setup.transport import STEP_INPUT
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
MARKER_NAME = ".spectracut-share"
WINDOWS_MARKER = f"{WINDOWS_LOCAL_PATH}/{MARKER_NAME}"
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


class StorageLayout(StrictModel):
    """How storage is laid out: the served folder (``None``: per-OS default) and the drive."""

    path: str | None = None
    drive: str = WINDOWS_DRIVE


_DRIVE = re.compile(r"^([A-Za-z]):?$")
_WINDOWS_ABS = re.compile(r"^[A-Za-z]:[\\/]")


def normalize_drive(value: str) -> str:
    """``s``, ``S`` or ``s:`` -> ``S:``; anything else is an error."""
    m = _DRIVE.match(value.strip())
    if not m:
        raise StorageError(f"expected a drive letter like S:, got {value!r}")
    return m.group(1).upper() + ":"


def normalize_storage_path(value: str) -> str:
    """An absolute Linux (``/srv/x``) or Windows (``D:/x``) folder, forward slashes, no quotes."""
    v = value.strip()
    if not v or any(c in v for c in "'\"`$;|\n\r"):
        raise StorageError(f"storage path must be a plain absolute folder, got {value!r}")
    if _WINDOWS_ABS.match(v):
        v = to_flamenco_path(v)
        v = v[0].upper() + v[1:]
        return v if len(v) == 3 else v.rstrip("/")
    if v.startswith("/"):
        return v if v == "/" else v.rstrip("/")
    raise StorageError(f"storage path must be absolute (D:/folder or /folder), got {value!r}")


def storage_folder(
    target: StorageTarget, facts: Mapping[str, HostFacts], layout: StorageLayout
) -> str:
    """The folder the storage host serves, checked against its OS."""
    windows = facts[target.host].os == "windows"
    if layout.path is None:
        return WINDOWS_LOCAL_PATH if windows else LINUX_PATH
    path = normalize_storage_path(layout.path)
    if windows != bool(_WINDOWS_ABS.match(path)):
        kind = "a Windows path like D:/folder" if windows else "a Linux path like /srv/folder"
        raise StorageError(f"{target.host} needs {kind} for --storage-path, got {path!r}")
    return path


def _norm_root(value: str) -> str:
    return value.replace("/", "\\").rstrip("\\").lower()


def check_windows_drive(
    target: StorageTarget,
    facts: Mapping[str, HostFacts],
    machines: Sequence[str],
    layout: StorageLayout,
) -> None:
    """Fail when the drive letter is taken on a Windows machine by anything but the storage.

    ``HostFacts.drives`` lists ``letter=root`` (root: the UNC of a network drive, the
    folder of a subst drive, empty for a local volume). The share's UNC is fine
    everywhere; on the storage host its own folder (subst) is fine too.
    """
    letter = normalize_drive(layout.drive)[0]
    unc = _norm_root(f"\\\\{_smb_server(target, facts)}\\{target.share}")
    for name in machines:
        f = facts[name]
        if f.os != "windows":
            continue
        ok = {unc}
        if target.kind == "host" and target.host == name:
            ok.add(_norm_root(storage_folder(target, facts, layout)))
        for entry in f.drives:
            used, _, root = entry.partition("=")
            if used.strip().rstrip(":").upper() == letter and _norm_root(root.strip()) not in ok:
                what = root.strip() or "a local disk"
                raise StorageError(
                    f"drive {letter}: is already used on {name} ({what}); "
                    "choose a free letter with --windows-drive"
                )


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


def manager_config(listen: str, windows_drive: str = WINDOWS_DRIVE) -> str:
    """``flamenco-manager.yaml``: Tailscale-only listener, Shaman off, shared variables."""
    doc = {
        "_meta": {"version": 3},
        "manager_name": "SpectraCut",
        "database": "flamenco-manager.sqlite",
        "listen": listen,
        "shared_storage_path": LINUX_PATH,
        "shaman": {"enabled": False},
        "variables": storage_variables(windows_drive=windows_drive),
    }
    return yaml.safe_dump(doc, sort_keys=False)


HOME_PREFIX = "$HOME/"
UNSAFE_PATH_CHARS = ('"', "`", "\\", "$", "\n")


def write_file_step(
    host: str, os: str, step_id: str, summary: str, path: str, content: str, mode: str = "644"
) -> Step:
    """Write ``content`` to ``path`` on the host, content via stdin (framed on Windows).

    A Linux ``$HOME/...`` path is expanded by the remote shell and written without sudo;
    any other Linux path is a literal system path written with sudo.
    """
    if os != "windows" and not re.fullmatch(r"[0-7]{3,4}", mode):
        raise ValueError(f"mode must be 3-4 octal digits: {mode!r}")
    if os == "windows":
        cmd = (
            f"$d = Split-Path '{path}'; New-Item -ItemType Directory -Force $d | Out-Null; "
            f"[IO.File]::WriteAllText('{path}', {STEP_INPUT})"
        )
        return Step(host, step_id, summary, cmd, "powershell", stdin=content, kind="write")
    if path.startswith(HOME_PREFIX):
        # Under the SSH user's home: expand $HOME (double quotes) and write as that user, so
        # the folders stay user-owned for ``systemctl --user`` and later user-run steps.
        rest = path[len(HOME_PREFIX) :]
        if not rest or any(c in rest for c in UNSAFE_PATH_CHARS):
            raise ValueError(f"unsafe path under $HOME: {path!r}")
        q = f'"{path}"'
        cmd = (
            f'd=$(dirname {q}); mkdir -p "$d" && (umask 077 && cat > {q}.tmp) '
            f"&& chmod {mode} {q}.tmp && mv -f {q}.tmp {q}"
        )
        return Step(host, step_id, summary, cmd, "sh", stdin=content, kind="write")
    if "$" in path or "'" in path:
        raise ValueError(f"path must be absolute or start with $HOME/: {path!r}")
    # Create under umask 077 as root and rename into place, so the content is never readable
    # beyond the final mode, even when the file already exists with a looser one.
    cmd = (
        f"d=$(dirname '{path}'); sudo mkdir -p \"$d\" && sudo sh -c "
        '\'umask 077; cat > "$1.tmp" && chmod "$2" "$1.tmp" && mv -f "$1.tmp" "$1"\' '
        f"sh '{path}' '{mode}'"
    )
    return Step(host, step_id, summary, cmd, "sh", stdin=content, kind="write")


def _smb_server(target: StorageTarget, facts: Mapping[str, HostFacts]) -> str:
    return facts[target.host].address if target.kind == "host" else target.host


def windows_storage_source() -> str:
    """``windows_storage.ps1``: the one-time admin script that creates the SMB share."""
    return Path(__file__).with_name(ADMIN_SCRIPT).read_text(encoding="utf-8")


_PROFILE_DIR = (
    "$d = Join-Path $env:USERPROFILE 'spectracut'; "
    "New-Item -ItemType Directory -Force $d | Out-Null; "
)


def _windows_admin_steps(
    host: str, cred: Credential, path: str, share: str, admin: bool
) -> list[Step]:
    """Write the admin script and its ACL'd password file, then run it or wait for an admin.

    Creating an SMB share and a local account needs admin. When the SSH account is an
    Administrator setup runs the script itself; otherwise the gate step fails until an
    admin has run it, and re-running ``spectracut setup`` continues. Either way the
    password reaches the script only through the ACL'd file (written from stdin).
    """
    marker = f"{path}/{MARKER_NAME}"
    write_script = (
        _PROFILE_DIR + f"[IO.File]::WriteAllText((Join-Path $d '{ADMIN_SCRIPT}'), {STEP_INPUT})"
    )
    write_pw = (
        _PROFILE_DIR
        + "$f = Join-Path $d 'storage.pw'; New-Item -ItemType File -Force $f | Out-Null; "
        "icacls $f /inheritance:r /grant:r ($env:USERNAME + ':F') '*S-1-5-32-544:F' | Out-Null; "
        f'[IO.File]::WriteAllText($f, {STEP_INPUT}.TrimEnd("`r", "`n"))'
    )
    if admin:
        serve = (
            _PROFILE_DIR + f"$s = Join-Path $d '{ADMIN_SCRIPT}'; $f = Join-Path $d 'storage.pw'; "
            "powershell.exe -NoProfile -ExecutionPolicy Bypass -File $s -PasswordFile $f "
            f"-User '{cred.user}' -Share '{share}' -Path '{path}'; "
            "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }; "
            f"if (-not (Test-Path '{marker}')) {{ "
            f"[Console]::Error.WriteLine('the storage admin script did not mark {path}'); exit 1 }}"
        )
        summary = "create the share (admin script, run as the Administrator SSH account)"
    else:
        serve = (
            _PROFILE_DIR + f"if (-not (Test-Path '{marker}')) {{ "
            f"$s = Join-Path $d '{ADMIN_SCRIPT}'; $f = Join-Path $d 'storage.pw'; "
            "[Console]::Error.WriteLine('run once as Administrator: powershell -ExecutionPolicy "
            f"Bypass -File ' + $s + ' -PasswordFile ' + $f + ' -User {cred.user} -Share {share} "
            f'-Path "{path}"; then re-run spectracut setup\'); exit 1 }}'
        )
        summary = "wait for the admin script to have created the share"
    return [
        Step(
            host,
            "storage:admin-script",
            "write the one-time admin script",
            write_script,
            "powershell",
            stdin=windows_storage_source(),
            kind="write",
        ),  # fmt: skip
        Step(
            host,
            "storage:admin-password",
            "store the share password (ACL'd file)",
            write_pw,
            "powershell",
            stdin=cred.password + "\n",
            kind="write",
        ),  # fmt: skip
        Step(host, "storage:serve", summary, serve, "powershell"),
    ]


def storage_notes(target: StorageTarget, facts: Mapping[str, HostFacts]) -> list[str]:
    """Heads-up for a non-admin Windows storage host: an administrator must run the script once."""
    if (
        target.kind == "host"
        and facts[target.host].os == "windows"
        and not facts[target.host].admin
    ):
        return [
            f"{target.host} needs a one-time admin step: setup writes {ADMIN_SCRIPT} and an "
            "ACL'd password file under its profile's spectracut folder and stops until the "
            "script has been run as Administrator; then re-run setup to mount and map the share"
        ]
    return []


def _serve_steps(
    target: StorageTarget,
    facts: Mapping[str, HostFacts],
    cred: Credential,
    layout: StorageLayout,
) -> list[Step]:
    """Set up the share on the chosen machine (Tailscale-only, dedicated account)."""
    host = facts[target.host]
    folder = storage_folder(target, facts, layout)
    if host.os == "windows":
        return _windows_admin_steps(target.host, cred, folder, target.share, host.admin)
    smb_conf = (
        f"[{SHARE}]\n   path = {folder}\n   read only = no\n   valid users = {cred.user}\n"
        f"   hosts allow = {TAILSCALE_RANGE} 127.0.0.1\n   hosts deny = 0.0.0.0/0\n"
        "   server min protocol = SMB3\n"
    )
    qfolder, quser = shlex.quote(folder), shlex.quote(cred.user)
    steps = [
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
            f"sudo mkdir -p {qfolder} && id -u {quser} >/dev/null 2>&1 "
            f"|| sudo useradd --system --no-create-home --shell /usr/bin/nologin {quser}; "
            f"sudo chown {quser} {qfolder}; "
            "grep -q 'spectracut.d' /etc/samba/smb.conf "
            "|| echo 'include = /etc/samba/spectracut.d/flamenco.conf' | sudo tee -a /etc/samba/smb.conf >/dev/null; "  # noqa: E501
            f'read -r pw; printf \'%s\\n%s\\n\' "$pw" "$pw" | sudo smbpasswd -s -a {quser}; '
            "sudo systemctl enable --now smb && sudo systemctl restart smb",
            "sh",
            stdin=cred.password + "\n",
        ),
    ]
    if folder != LINUX_PATH:
        # The rest of the farm and the Manager see storage at LINUX_PATH: bind the folder there.
        steps.append(
            Step(
                target.host,
                "storage:bind",
                f"bind {folder} at {LINUX_PATH}",
                f"sudo mkdir -p {LINUX_PATH}; "
                f"mountpoint -q {LINUX_PATH} && sudo umount {LINUX_PATH}; "
                f"sudo mount --bind {qfolder} {LINUX_PATH}",
            )
        )
    return steps


def _attach_steps(name: str, server: str, share: str, cred: Credential) -> list[Step]:
    """Mount the share on one Linux machine, replacing any old mount.

    Windows machines get no step here: drive mappings live in a logon session, so the
    worker task maps the drive itself (``windows_worker.ps1``: subst of the folder on
    the storage host, the UNC share elsewhere) from ``worker-args.json``.
    """
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
            f"sudo mount -t cifs {shlex.quote(f'//{server}/{share}')} {LINUX_PATH} "
            f"-o credentials={CRED_FILE},vers=3.0,uid=$(id -u),gid=$(id -g)",
        ),
    ]


MANAGER_CONFIG_PATH = "$HOME/.local/opt/flamenco/flamenco-manager.yaml"


def manager_config_steps(
    facts: Mapping[str, HostFacts], layout: StorageLayout | None = None
) -> list[Step]:
    """Write the Manager config (listen address, two-way storage variables), then restart it.

    Part of every storage change, and planned on its own when the file is missing on disk.
    """
    layout = layout or StorageLayout()
    listen = f"{facts[CONTROL].address}:{MANAGER_PORT}"
    return [
        write_file_step(
            CONTROL,
            "linux",
            "storage:variables",
            "write the Manager config with the two-way storage variables",
            MANAGER_CONFIG_PATH,
            manager_config(listen, normalize_drive(layout.drive)),
        ),
        Step(
            CONTROL,
            "storage:restart-manager",
            "restart the Flamenco Manager",
            "systemctl --user restart flamenco-manager",
        ),
    ]


def plan_storage(
    target: StorageTarget,
    previous: StorageTarget | None,
    facts: Mapping[str, HostFacts],
    workers: Sequence[str],
    cred: Credential,
    layout: StorageLayout | None = None,
    previous_layout: StorageLayout | None = None,
) -> list[Step]:
    """Steps that put storage on ``target`` and re-point the Manager and every worker.

    Empty when ``previous == target`` and the layout (folder, drive) is unchanged
    (already converged). A change migrates: files on the control plane's share are
    copied across before the re-point. The drive letter is checked on every Windows
    machine first, so a conflict fails the plan (and ``--dry-run``).
    """
    layout = layout or StorageLayout()
    for name in (CONTROL, *workers):
        if name not in facts:
            raise StorageError(f"no facts for {name}")
    if target.kind == "host" and target.host not in facts:
        raise StorageError(f"unknown storage host {target.host!r}")
    check_windows_drive(target, facts, [CONTROL, *workers], layout)
    if previous == target and (previous_layout or StorageLayout()) == layout:
        return []
    server = _smb_server(target, facts)
    steps: list[Step] = []
    if target.kind == "host":
        steps += _serve_steps(target, facts, cred, layout)
    control_is_storage = target.kind == "host" and target.host == CONTROL
    if previous is not None and not control_is_storage:
        steps += _attach_steps(CONTROL, server, target.share, cred)[:1]
        steps += _migrate_steps(server, target.share, cred)
    machines = [CONTROL, *[w for w in workers if w != CONTROL]]
    for name in machines:
        if name == CONTROL and control_is_storage:
            continue  # served locally at LINUX_PATH
        if name == target.host and facts[name].os == "linux" and target.kind == "host":
            continue
        if facts[name].os == "windows":
            continue  # mapped in the worker task's session (worker-args.json)
        steps += _attach_steps(name, server, target.share, cred)
    steps += manager_config_steps(facts, layout)
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
                kind="task",
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
            f"sudo mkdir -p {MIGRATE_MOUNT} && sudo mount -t cifs "
            f"{shlex.quote(f'//{server}/{share}')} "
            f"{MIGRATE_MOUNT} -o credentials={CRED_FILE},vers=3.0,uid=$(id -u),gid=$(id -g) "
            f"&& rsync -a {LINUX_PATH}/ {MIGRATE_MOUNT}/; rc=$?; "
            f"sudo umount {MIGRATE_MOUNT}; exit $rc",
        )
    ]
