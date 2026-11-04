# SPDX-License-Identifier: AGPL-3.0-or-later
"""``spectracut render``, ``status`` and ``hosts``: queue renders and see the farm.

The control plane never runs Blender. ``render`` checks the frame cache and,
on a miss, writes a signed job to ``<home>/jobs/<job_id>.json``, the directory
``spectracut-host --once --jobs-dir`` pulls from. A host's result is recorded
next to it as ``<job_id>.result.json``.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal

import typer
from pydantic import BaseModel, Field, ValidationError

from spectracut.queue.cache import MANIFEST, FrameCache
from spectracut.queue.executors.base import GpuCard
from spectracut.queue.executors.hostpool import HostSlot
from spectracut.queue.profile import RenderProfile
from spectracut.queue.signing import (
    JobEnvelope,
    JobSignatureError,
    load_private_key,
    parse_signed_job,
    sign_job,
)
from spectracut.setup.manifest import load_manifest
from spectracut.setup.plan import CommandRunner, SetupError, bootstrap_host
from spectracut.spec._base import Slug, StrictModel, canonical_json
from spectracut.spec.recipe import Recipe
from spectracut.studio.store import ProductionError, ProductionStore, spectracut_home, write_atomic

HomeOpt = Annotated[
    Path | None,
    typer.Option("--home", help="SpectraCut home (default $SPECTRACUT_HOME or XDG data dir)."),
]

RESULT_SUFFIX = ".result.json"


def _home(home: Path | None) -> Path:
    return Path(home) if home is not None else spectracut_home()


def _fail(message: str) -> typer.Exit:
    typer.echo(f"error: {message}", err=True)
    return typer.Exit(2)


def _read_model[M: BaseModel](path: Path, model: type[M], what: str) -> M:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise _fail(f"cannot read {what} {path}: {exc.strerror or exc}") from exc
    try:
        return model.model_validate_json(data)
    except ValidationError as exc:
        raise _fail(
            f"{path} is not a valid {what}: {exc.error_count()} error(s), "
            f"first: {exc.errors()[0]['msg']}"
        ) from exc


def _frames(value: str) -> tuple[int, int]:
    start, sep, end = value.partition("-")
    try:
        first = int(start)
        last = int(end) if sep else first
    except ValueError:
        raise typer.BadParameter(f"expected A-B, got {value!r}") from None
    if first < 0 or last < first:
        raise typer.BadParameter(f"expected 0 <= A <= B, got {value!r}")
    return first, last


def render_cmd(
    recipe_file: Annotated[Path, typer.Argument(metavar="RECIPE", help="Recipe JSON file.")],
    profile_file: Annotated[
        Path,
        typer.Option("--profile", help="Render profile JSON (from `spectracut-host profile`)."),
    ],
    frames: Annotated[str, typer.Option("--frames", help="Frame range A-B.")] = "1-1",
    job_id: Annotated[
        str | None, typer.Option("--job-id", help="Job id (default derived).")
    ] = None,
    tag: Annotated[
        str | None, typer.Option("--tag", help="Flamenco worker tag the job requires.")
    ] = None,
    ttl_hours: Annotated[
        float, typer.Option("--ttl-hours", min=0.01, help="Hours until the job expires.")
    ] = 24.0,
    home: HomeOpt = None,
) -> None:
    """Queue a recipe render: reuse cached frames, else write a signed job for hosts to pull."""
    recipe = _read_model(recipe_file, Recipe, "recipe")
    profile = _read_model(profile_file, RenderProfile, "render profile")
    first, last = _frames(frames)
    root = _home(home)
    recipe_hash = recipe.content_hash
    cache = FrameCache(root / "cache")
    hit = cache.get(recipe_hash, profile)
    if hit is not None:
        digest = hit.key.removeprefix("sha256:")
        typer.echo(f"cached {hit.key}: {cache.root / digest[:2] / digest / MANIFEST}")
        return
    try:
        key = load_private_key()
    except ValueError as exc:
        raise _fail(str(exc)) from exc
    now = datetime.now(UTC)
    try:
        envelope = JobEnvelope(
            job_id=job_id or f"{recipe.shot_id}-{recipe_hash.removeprefix('sha256:')[:12]}",
            recipe=recipe,
            recipe_hash=recipe_hash,
            frame_start=first,
            frame_end=last,
            output=recipe.shot_id,
            required_tag=tag,
            issued_at=now,
            expires_at=now + timedelta(hours=ttl_hours),
        )
    except ValidationError as exc:
        raise _fail(f"invalid job: {exc.errors()[0]['msg']}") from exc
    path = root / "jobs" / f"{envelope.job_id}.json"
    write_atomic(path, canonical_json(sign_job(envelope, key)))
    typer.echo(f"queued {envelope.job_id}: {path}")


def _job_lines(jobs_dir: Path) -> list[str]:
    if not jobs_dir.is_dir():
        return []
    lines = []
    for path in sorted(jobs_dir.glob("*.json")):
        if path.name.endswith(RESULT_SUFFIX):
            continue
        try:
            env = parse_signed_job(path.read_bytes()).envelope
        except (OSError, JobSignatureError) as exc:
            lines.append(f"  {path.stem}: unreadable ({exc})")
            continue
        if (jobs_dir / f"{env.job_id}{RESULT_SUFFIX}").is_file():
            state = "done"
        elif env.is_expired():
            state = "expired"
        else:
            state = "queued"
        lines.append(
            f"  {env.job_id}: {state} recipe={env.recipe_hash[:19]} "
            f"frames={env.frame_start}-{env.frame_end} expires={env.expires_at.isoformat()}"
        )
    return lines


def status_cmd(home: HomeOpt = None) -> None:
    """List productions with node-status counts, and queued render jobs."""
    root = _home(home)
    store = ProductionStore(root)
    ids = store.list()
    typer.echo(f"productions: {len(ids)}")
    for pid in ids:
        try:
            graph, meta = store.load(pid)
        except ProductionError as exc:
            typer.echo(f"  {pid}: unreadable ({exc})")
            continue
        counts = Counter(s.value for s in graph.statuses().values())
        summary = " ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        typer.echo(f"  {pid}: profile={meta.profile or 'none'} {summary}")
    jobs = _job_lines(root / "jobs")
    typer.echo(f"jobs: {len(jobs)}")
    for line in jobs:
        typer.echo(line)


# ---- hosts -----------------------------------------------------------------


HostRole = Literal["general", "gpu-only"]
GPU_ONLY_RAM_GB = 4.0


class HostEntry(StrictModel):
    """One registered render host, as stored in ``<home>/hosts.json``."""

    name: Slug
    cpus: float = Field(gt=0)
    mem_gb: float = Field(gt=0)
    gpus: tuple[GpuCard, ...] = ()
    profile: RenderProfile
    role: HostRole = "general"
    ram_cap_gb: float | None = Field(default=None, gt=0)

    def slot(self) -> HostSlot:
        gpu_only = self.role == "gpu-only"
        cap = (self.ram_cap_gb or GPU_ONLY_RAM_GB) if gpu_only else self.mem_gb
        return HostSlot(
            name=self.name,
            profile=self.profile,
            cpus=self.cpus,
            mem_gb=min(self.mem_gb, cap),
            gpus=self.gpus,
            build_lanes=not gpu_only,
        )


class HostRegistry(StrictModel):
    hosts: tuple[HostEntry, ...] = ()


def hosts_path(home: Path | None = None) -> Path:
    return _home(home) / "hosts.json"


def _read_registry(home: Path | None) -> HostRegistry:
    path = hosts_path(home)
    if not path.is_file():
        return HostRegistry()
    try:
        return HostRegistry.model_validate_json(path.read_bytes())
    except (OSError, ValidationError) as exc:
        raise ValueError(f"cannot read host registry {path}: {exc}") from exc


def load_hosts(home: Path | None = None) -> tuple[HostSlot, ...]:
    """Registered hosts as :class:`HostSlot`s for a :class:`HostPoolExecutor`."""
    return tuple(h.slot() for h in _read_registry(home).hosts)


hosts_app = typer.Typer(no_args_is_help=True, help="Registered render hosts.")


def _registry(home: Path | None) -> HostRegistry:
    try:
        return _read_registry(home)
    except ValueError as exc:
        raise _fail(str(exc)) from exc


def _gpu(value: str) -> GpuCard:
    model, sep, vram = value.rpartition(":")
    try:
        if not sep:
            raise ValueError
        return GpuCard(name=model, vram_gb=int(vram))
    except (ValueError, ValidationError):
        raise typer.BadParameter(f"expected MODEL:VRAM_GB, got {value!r}") from None


@hosts_app.command("list")
def hosts_list(home: HomeOpt = None) -> None:
    """List registered hosts and the capacity each lends."""
    reg = _registry(home)
    if not reg.hosts:
        typer.echo("no hosts registered")
    for h in reg.hosts:
        gpus = ", ".join(f"{g.name} {g.vram_gb} GB" for g in h.gpus) or "no GPU"
        typer.echo(
            f"{h.name}: cpus={h.cpus:g} mem_gb={h.mem_gb:g} gpus=[{gpus}] "
            f"blender={h.profile.blender_version} os={h.profile.os}"
        )


@hosts_app.command("add")
def hosts_add(
    alias: Annotated[
        str | None,
        typer.Argument(metavar="[SSH_ALIAS]", help="Host name; the SSH alias setup uses."),
    ] = None,
    name: Annotated[str | None, typer.Option("--name", help="Host name (slug).")] = None,
    cpus: Annotated[
        float | None, typer.Option("--cpus", help="CPUs lent to the pool (probed if omitted).")
    ] = None,
    mem_gb: Annotated[
        float | None, typer.Option("--mem-gb", help="Memory lent, in GB (probed if omitted).")
    ] = None,
    profile_file: Annotated[
        Path | None,
        typer.Option(
            "--profile-file",
            help="JSON from `spectracut-host profile`; omit to bootstrap the host over SSH.",
        ),
    ] = None,
    gpu: Annotated[
        list[str] | None, typer.Option("--gpu", help="Whole GPU as MODEL:VRAM_GB (repeatable).")
    ] = None,
    role: Annotated[
        HostRole,
        typer.Option("--role", help="general, or gpu-only (GPU work only, no build lanes)."),
    ] = "general",
    ram_cap_gb: Annotated[
        float | None,
        typer.Option("--ram-cap-gb", help="Memory cap in GB (gpu-only defaults to 4)."),
    ] = None,
    replace: Annotated[
        bool, typer.Option("--replace", help="Replace a host of that name.")
    ] = False,
    home: HomeOpt = None,
) -> None:
    """Register a render host; with no --profile-file, install spectracut-host over SSH first."""
    if alias and name and alias != name:
        raise _fail(f"host given twice: {alias!r} and --name {name!r}")
    name = alias or name
    if not name:
        raise _fail("give the host's SSH alias (or --name)")
    reg = _registry(home)
    others = tuple(h for h in reg.hosts if h.name != name)
    if len(others) != len(reg.hosts) and not replace:
        raise _fail(f"host {name!r} is already registered (use --replace)")
    gpus = tuple(_gpu(g) for g in gpu or ())
    if profile_file is not None:
        if cpus is None or mem_gb is None:
            raise _fail("--cpus and --mem-gb are required with --profile-file")
        profile = _read_model(profile_file, RenderProfile, "render profile")
    else:
        try:
            found = bootstrap_host(load_manifest(), name, CommandRunner(), _home(home))
        except (SetupError, ValueError, OSError) as exc:
            raise _fail(str(exc)) from exc
        profile = found.profile
        cpus = cpus if cpus is not None else found.cpus
        mem_gb = mem_gb if mem_gb is not None else found.mem_gb
        if not gpus and profile.gpu_model != "unknown" and profile.vram_gb:
            gpus = (GpuCard(name=profile.gpu_model, vram_gb=profile.vram_gb),)
    try:
        entry = HostEntry(
            name=name,
            cpus=cpus,
            mem_gb=mem_gb,
            gpus=gpus,
            profile=profile,
            role=role,
            ram_cap_gb=ram_cap_gb,
        )
    except ValidationError as exc:
        raise _fail(f"invalid host: {exc.errors()[0]['loc']}: {exc.errors()[0]['msg']}") from exc
    write_atomic(hosts_path(home), canonical_json(HostRegistry(hosts=(*others, entry))))
    typer.echo(f"registered {name}")


@hosts_app.command("remove")
def hosts_remove(
    name: Annotated[str, typer.Argument(help="Host name.")], home: HomeOpt = None
) -> None:
    """Unregister a render host."""
    reg = _registry(home)
    others = tuple(h for h in reg.hosts if h.name != name)
    if len(others) == len(reg.hosts):
        raise _fail(f"no host {name!r} registered")
    write_atomic(hosts_path(home), canonical_json(HostRegistry(hosts=others)))
    typer.echo(f"removed {name}")


__all__ = ["HostEntry", "HostRegistry", "hosts_app", "load_hosts", "render_cmd", "status_cmd"]
