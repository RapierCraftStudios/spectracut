# SPDX-License-Identifier: AGPL-3.0-or-later
"""``spectracut setup``: install, configure, pin and upgrade every bundled component.

The control plane is this machine; render hosts are the registered hosts
(``spectracut hosts``), reached over SSH by name. Setup is idempotent: only what
differs from the manifest is done. ``--storage-host`` moves Flamenco's shared
storage (a registered host, ``control``, or ``nas://server/share``);
``--storage-path`` and ``--windows-drive`` set the served folder and the Windows
drive letter, kept in the setup state for later runs.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Annotated

import typer

from spectracut.cli.render import HomeOpt, _home, _read_registry
from spectracut.providers.base import redact
from spectracut.queue.signing import ensure_signing_key, read_public_key
from spectracut.setup.manifest import HostFacts, StepRunner, load_manifest
from spectracut.setup.plan import (
    CommandRunner,
    SetupError,
    apply_plan,
    build_plan,
    load_state,
    save_state,
)
from spectracut.setup.storage import (
    CONTROL,
    Credential,
    StorageError,
    StorageTarget,
    normalize_drive,
    normalize_storage_path,
    parse_storage_host,
)
from spectracut.setup.wheels import build_wheels

SHARE_USER = "spectracut-share"
# Shown in a dry run before the signing key exists; the real run creates the key first.
PENDING_PUBLIC_KEY = "<created by setup>"
NAS_USER_ENV = "SPECTRACUT_NAS_USER"
NAS_PASSWORD_ENV = "SPECTRACUT_NAS_PASSWORD"


def _fail(message: str) -> typer.Exit:
    typer.echo(redact(f"error: {message}"), err=True)
    return typer.Exit(2)


def share_credential(home: Path, target: StorageTarget | None) -> Credential:
    """The share account: the env pair for a NAS, else a generated one kept 0600 in the home."""
    if target is not None and target.kind == "nas":
        user = os.environ.get(NAS_USER_ENV, "guest")
        return Credential(user, os.environ.get(NAS_PASSWORD_ENV, ""))
    path = home / "storage.cred"
    if path.is_file():
        return Credential(SHARE_USER, path.read_text(encoding="utf-8").strip())
    password = secrets.token_urlsafe(24)
    home.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(password + "\n")
    return Credential(SHARE_USER, password)


def setup_cmd(
    upgrade: Annotated[
        bool, typer.Option("--upgrade", help="Move to the next tested set of pins.")
    ] = False,
    storage_host: Annotated[
        str | None,
        typer.Option(
            "--storage-host",
            help=(
                "Where Flamenco storage lives: 'control', a registered host (a Windows host "
                "needs a one-time admin script, printed by setup), or nas://server/share."
            ),
        ),
    ] = None,
    storage_path: Annotated[
        str | None,
        typer.Option(
            "--storage-path",
            help=(
                "Folder the storage host serves, e.g. D:/SpectraCut/flamenco or /data/flamenco "
                "(default C:/spectracut/storage on Windows, /srv/flamenco on Linux); kept for "
                "later runs."
            ),
        ),
    ] = None,
    windows_drive: Annotated[
        str | None,
        typer.Option(
            "--windows-drive",
            help=(
                "Drive letter every Windows host sees storage on (default F:); must be free "
                "on each Windows host. Kept for later runs."
            ),
        ),
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print the plan; change nothing.")
    ] = False,
    home: HomeOpt = None,
) -> None:
    """Install and configure every bundled component on the control plane and render hosts."""
    root = _home(home)
    manifest = load_manifest()
    try:
        state = load_state(root)
        workers = [h.name for h in _read_registry(root).hosts]
        set_id = state.set_id or manifest.sets[0].id
        if upgrade:
            nxt = manifest.next_tested_set(state.set_id)
            if nxt is None:
                typer.echo(f"already on the latest tested set ({set_id})")
            else:
                set_id = nxt.id
        target = parse_storage_host(storage_host, [CONTROL, *workers]) if storage_host else None
        settings: dict[str, str] = {}
        if storage_path is not None:
            settings["storage_path"] = normalize_storage_path(storage_path)
        if windows_drive is not None:
            settings["windows_drive"] = normalize_drive(windows_drive)
        if settings:
            state = state.model_copy(update=settings)
        runner: StepRunner = CommandRunner()
        facts: dict[str, HostFacts] = {n: runner.probe(n) for n in (CONTROL, *workers)}
        cred = share_credential(root, target or state.storage)
        wheels = build_wheels(root / "wheels")
        # The control plane's job-signing key: created once (0600), never printed.
        if dry_run:
            public_key = read_public_key(root) or PENDING_PUBLIC_KEY
        else:
            public_key = ensure_signing_key(root)
        plan = build_plan(
            manifest,
            set_id,
            facts,
            workers,
            state,
            cred,
            target,
            wheels=wheels,
            signing_public_key=public_key,
        )
        if settings and not dry_run:
            save_state(state, root)  # valid for this farm; kept even if a later step stops
        for note in plan.notes:
            typer.echo(f"warning: {note}")
        if not plan.steps:
            typer.echo(f"nothing to do: set {set_id}, storage {plan.storage.host} is up to date")
            return
        for step in plan.steps:
            typer.echo(redact(f"{step.host}: {step.summary}"))
        if dry_run:
            return
        apply_plan(plan, runner, state, root)
    except (StorageError, SetupError, ValueError, OSError) as exc:
        raise _fail(str(exc)) from exc
    typer.echo(f"set {set_id} applied; storage on {plan.storage.host}")
