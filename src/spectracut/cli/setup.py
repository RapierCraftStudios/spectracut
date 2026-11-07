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
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Annotated, Protocol

import typer

from spectracut.cli.render import HomeOpt, HostEntry, _home, _read_registry
from spectracut.providers.base import redact
from spectracut.queue.flamenco import FlamencoClient, FlamencoError, job_type_names
from spectracut.queue.signing import ensure_signing_key, read_public_key
from spectracut.setup.manifest import HostFacts, StepRunner, load_manifest
from spectracut.setup.plan import (
    CommandRunner,
    SetupError,
    SetupState,
    apply_plan,
    build_plan,
    load_state,
    probe_hosts,
    probe_model_disk,
    save_state,
    worker_credentials_reset_step,
)
from spectracut.setup.storage import (
    CONTROL,
    MANAGER_PORT,
    Credential,
    StorageError,
    StorageTarget,
    normalize_drive,
    normalize_storage_path,
    parse_storage_host,
)
from spectracut.setup.model_assets import resolve_models
from spectracut.setup.wheels import build_wheels
from spectracut.setup.worker_tags import TagApi, desired_tags, sync_worker_tags

SHARE_USER = "spectracut-share"
# Shown in a dry run before the signing key exists; the real run creates the key first.
PENDING_PUBLIC_KEY = "<created by setup>"
NAS_USER_ENV = "SPECTRACUT_NAS_USER"
NAS_PASSWORD_ENV = "SPECTRACUT_NAS_PASSWORD"


# Seconds to wait for the Manager to list the job types (it restarts when one changed).
JOB_TYPES_TIMEOUT = 60.0
JOB_TYPES_POLL = 2.0


class ManagerApi(TagApi, Protocol):
    """The Manager API setup uses: worker tags and the job types it knows."""

    def job_types(self) -> list[str]: ...


def manager_client(url: str) -> ManagerApi:
    """The Manager API client setup uses for worker tags and job types (replaced in tests)."""
    return FlamencoClient(url)


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
    require_all: Annotated[
        bool,
        typer.Option(
            "--require-all",
            help=(
                "Fail when any registered host is unreachable (default: skip it with a "
                "warning; the control plane and the storage host must always be reachable)."
            ),
        ),
    ] = False,
    models: Annotated[
        str | None,
        typer.Option(
            "--models",
            help=(
                "Model weights for the model host: essential (sfx, music, temp VO, ASR, "
                "CLAP; the default), all, or comma-separated model ids. Kept for later runs."
            ),
        ),
    ] = None,
    models_dir: Annotated[
        str | None,
        typer.Option(
            "--models-dir",
            help=(
                "Folder on the model host for model weights, e.g. D:/spectracut/models "
                "(default: SPECTRACUT_MODEL_ROOT, else the per-OS default). Kept for later runs."
            ),
        ),
    ] = None,
    skip_models: Annotated[
        bool,
        typer.Option(
            "--skip-models",
            help="Update hosts and runtimes without downloading or checking model weights.",
        ),
    ] = False,
    home: HomeOpt = None,
) -> None:
    """Install and configure every bundled component on the control plane and render hosts."""
    root = _home(home)
    manifest = load_manifest()
    try:
        state = load_state(root)
        registered = _read_registry(root).hosts
        workers = [h.name for h in registered]
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
        if models is not None:
            resolve_models(models)  # unknown ids fail before any host is touched
            settings["models"] = models.strip()
        if models_dir is not None:
            try:
                settings["models_dir"] = normalize_storage_path(models_dir)
            except StorageError as exc:
                raise ValueError(f"--models-dir: {exc}".replace("storage path", "path")) from exc
        if settings:
            state = state.model_copy(update=settings)
        runner: StepRunner = CommandRunner()
        names = [CONTROL, *workers]
        facts, skipped = probe_hosts(
            runner, names, required=names if require_all else _required_hosts(target, state)
        )
        for name, reason in skipped.items():
            typer.echo(redact(f"warning: skipped {name} (unreachable): {reason}"))
        workers = [w for w in workers if w in facts]
        registered = tuple(h for h in registered if h.name in facts)
        cred = share_credential(root, target or state.storage)
        wheels = build_wheels(root / "wheels")
        # The control plane's job-signing key: created once (0600), never printed.
        if dry_run:
            public_key = read_public_key(root) or PENDING_PUBLIC_KEY
        else:
            public_key = ensure_signing_key(root)
        model_host = _model_host(root)
        disk = None
        if model_host is not None and model_host in workers and not skip_models:
            # Read-only: free and total space at the model root, measured up front.
            disk = probe_model_disk(runner, facts[model_host], state.models_dir)
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
            model_host=model_host,
            model_disk=disk,
            skip_models=skip_models,
        )
        if settings and not dry_run:
            save_state(state, root)  # valid for this farm; kept even if a later step stops
        for note in plan.notes:
            typer.echo(f"warning: {note}")
        if not plan.steps:
            typer.echo(f"nothing to do: set {set_id}, storage {plan.storage.host} is up to date")
        for step in plan.steps:
            typer.echo(redact(f"{step.host}: {step.summary}"))
        if plan.steps and not dry_run:
            apply_plan(plan, runner, state, root)
        # Worker tags live in the Manager, so they are synced even when the hosts converged.
        url = f"http://{facts[CONTROL].address}:{MANAGER_PORT}"
        _sync_tags(url, registered, dry_run, runner, facts)
        # Likewise the job types: the Manager must list every packaged one after setup.
        _check_job_types(url, dry_run)
        if dry_run or not plan.steps:
            _skipped_summary(skipped)
            return
    except (StorageError, SetupError, ValueError, OSError) as exc:
        failure = _fail(str(exc))
        if isinstance(exc, SetupError) and exc.host is not None:
            typer.echo(redact(f"failed steps:\n  {exc.host}: {exc.summary}"), err=True)
        raise failure from exc
    typer.echo(f"set {set_id} applied; storage on {plan.storage.host}")
    _skipped_summary(skipped)


def _model_host(home: Path) -> str | None:
    """The model host studio uses (``SPECTRACUT_MODEL_HOST``, ``[models] host``, GPU host)."""
    from spectracut.cli.studio import model_host_name

    return model_host_name(home)


def _required_hosts(target: StorageTarget | None, state: SetupState) -> set[str]:
    """Hosts setup cannot skip: the control plane and the storage host it sets up or keeps."""
    required = {CONTROL}
    storage = target or state.storage
    if storage is not None and storage.kind == "host":
        required.add(storage.host)
    return required


def _skipped_summary(skipped: Mapping[str, str]) -> None:
    """Name the unreachable hosts setup left as they were (re-run setup once they are up)."""
    if skipped:
        typer.echo(
            f"skipped (unreachable): {', '.join(skipped)}; run `spectracut setup` again "
            "once they are online"
        )


def _check_job_types(
    url: str,
    dry_run: bool,
    *,
    timeout: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Fail unless the Manager lists every packaged job type (a dry run only warns).

    Polls up to ``timeout`` s (default :data:`JOB_TYPES_TIMEOUT`): setup restarts the
    Manager when a job type changed.
    """
    if timeout is None:
        timeout = JOB_TYPES_TIMEOUT
    wanted = job_type_names()
    client = manager_client(url)
    deadline = clock() + timeout
    missing: list[str] = []
    problem = ""
    while True:
        try:
            listed = set(client.job_types())
            missing = [n for n in wanted if n not in listed]
            problem = ""
        except FlamencoError as exc:
            missing, problem = wanted, f" ({exc})"
        if not missing or dry_run or clock() >= deadline:
            break
        sleep(min(JOB_TYPES_POLL, max(deadline - clock(), 0.0)))
    if not missing:
        return
    message = (
        f"Flamenco Manager does not list job type {', '.join(missing)}{problem}; check the "
        "scripts/ folder next to flamenco-manager and run `spectracut setup` again"
    )
    if dry_run:
        typer.echo(redact(f"warning: {message}"))
        return
    raise SetupError(message)


def _sync_tags(
    url: str,
    hosts: Sequence[HostEntry],
    dry_run: bool,
    runner: StepRunner | None = None,
    facts: Mapping[str, HostFacts] | None = None,
) -> None:
    """Create and assign each registered host's worker tags (a dry run only reads).

    With ``runner`` and ``facts``, a host whose worker the Manager does not know (a new
    Manager database) gets its worker credentials reset once so it registers again.
    """
    desired = desired_tags(hosts)
    if not desired:
        return

    def reset(host: str) -> None:
        assert runner is not None and facts is not None
        step = worker_credentials_reset_step(facts[host])
        typer.echo(redact(f"{host}: {step.summary}"))
        runner.run(step)

    can_reset = not dry_run and runner is not None and facts is not None
    try:
        actions = sync_worker_tags(
            manager_client(url), desired, dry_run=dry_run, reset=reset if can_reset else None
        )
    except SetupError as exc:
        if not dry_run:
            raise
        typer.echo(f"warning: {exc}")
        actions = [f"{h}: worker tags {', '.join(t)}" for h, t in desired.items()]
    for action in actions:
        typer.echo(redact(action))
