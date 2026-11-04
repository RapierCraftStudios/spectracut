# SPDX-License-Identifier: AGPL-3.0-or-later
"""``spectracut setup``: install, configure, pin and upgrade every bundled component.

The control plane is this machine; render hosts are the registered hosts
(``spectracut hosts``), reached over SSH by name. Setup is idempotent: only what
differs from the manifest is done. ``--storage-host`` moves Flamenco's shared
storage (a registered host, ``control``, or ``nas://server/share``).
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Annotated

import typer

from spectracut.cli.render import HomeOpt, _home, _read_registry
from spectracut.providers.base import redact
from spectracut.setup.manifest import HostFacts, StepRunner, load_manifest
from spectracut.setup.plan import (
    CommandRunner,
    SetupError,
    apply_plan,
    build_plan,
    load_state,
)
from spectracut.setup.storage import (
    CONTROL,
    Credential,
    StorageError,
    StorageTarget,
    parse_storage_host,
)

SHARE_USER = "spectracut-share"
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
        runner: StepRunner = CommandRunner()
        facts: dict[str, HostFacts] = {n: runner.probe(n) for n in (CONTROL, *workers)}
        cred = share_credential(root, target or state.storage)
        plan = build_plan(manifest, set_id, facts, workers, state, cred, target)
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
