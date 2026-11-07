# SPDX-License-Identifier: AGPL-3.0-or-later
"""``spectracut setup``: install, pin and report every bundled component."""

from __future__ import annotations

from pathlib import Path

import spectracut
from spectracut.setup.manifest import Manifest, StepRunner, load_manifest
from spectracut.setup.model_assets import (
    ESSENTIAL_MODELS,
    extras_digest,
    model_version,
    reserve_gb,
    resolve_models,
)
from spectracut.setup.plan import (
    MODEL_PREFIX,
    MODELS_DEPS,
    SetupError,
    SetupState,
    load_state,
    probe_model_disk,
)
from spectracut.setup.wheels import CONTROL_PLANE_PIN


def component_lines(home: Path | None = None, manifest: Manifest | None = None) -> list[str]:
    """One line per component and machine: installed version and health.

    ``healthy`` means the installed version equals the pin of the active tested set;
    ``drift`` that it differs (``spectracut setup`` fixes it); ``not installed`` that
    setup has not put the component on any machine. A component built on the control
    plane (spectracut-host) is healthy when its installed version, without the wheel
    digest after ``+``, is this control plane's version.
    """
    manifest = manifest or load_manifest()
    state = load_state(home)
    set_id = state.set_id or manifest.sets[0].id
    try:
        pins = manifest.tested_set(set_id).versions
    except KeyError:
        set_id = manifest.sets[0].id
        pins = manifest.sets[0].versions
    lines = [f"tested set {set_id}"]
    for name in manifest.components:
        installed = {
            host: hs.components[name] for host, hs in state.hosts.items() if name in hs.components
        }
        pin = spectracut.__version__ if pins[name] == CONTROL_PLANE_PIN else pins[name]
        if not installed:
            lines.append(f"{name} {pin}: not installed")
        for host in sorted(installed):
            ver = installed[host]
            have = ver.split("+", 1)[0] if pins[name] == CONTROL_PLANE_PIN else ver
            health = "healthy" if have == pin else f"drift (pinned {pin})"
            lines.append(f"{name} {ver} on {host}: {health}")
    return lines + model_lines(home, state=state)


def model_lines(
    home: Path | None = None, model_host: str | None = None, *, state: SetupState | None = None
) -> list[str]:
    """One line per registered model (registry order), then the family deps.

    Each model line ends with ``(essential|optional, selected|not selected)``: whether it
    is in the essential set and in the selection ``setup`` installs (``setup --models``,
    default essential). ``healthy`` when the installed weights version equals the pinned
    :func:`~spectracut.setup.model_assets.model_version`, ``drift`` otherwise, ``not
    installed`` when no host (or not ``model_host``, when given) has the model.
    """
    from spectracut.providers.local.registry import MODELS

    state = state or load_state(home)
    hosts = {
        h: hs.components for h, hs in state.hosts.items() if model_host is None or h == model_host
    }
    try:
        selected = set(resolve_models(state.models or "essential"))
    except ValueError:
        selected = set(ESSENTIAL_MODELS)
    lines: list[str] = []
    for spec in MODELS.values():
        tag = "essential" if spec.id in ESSENTIAL_MODELS else "optional"
        tag += ", selected" if spec.id in selected else ", not selected"
        pin = model_version(spec.id)
        installed = {
            h: c[MODEL_PREFIX + spec.id] for h, c in hosts.items() if MODEL_PREFIX + spec.id in c
        }
        if not installed:
            lines.append(f"{spec.id} {spec.licence}: not installed ({tag})")
        for host in sorted(installed):
            ver = installed[host]
            health = "healthy" if ver == pin else f"drift (pinned {pin[:12]})"
            lines.append(f"{spec.id} {ver[:12]} on {host}: {health} ({tag})")
    digest = extras_digest()
    deps = {h: c[MODELS_DEPS] for h, c in hosts.items() if MODELS_DEPS in c}
    if not deps:
        lines.append(f"{MODELS_DEPS} {digest}: not installed")
    for host in sorted(deps):
        ver = deps[host]
        health = "healthy" if ver.endswith(f"+{digest}") else f"drift (pinned +{digest})"
        lines.append(f"{MODELS_DEPS} {ver} on {host}: {health}")
    return lines


def model_disk_lines(
    model_host: str | None, runner: StepRunner, home: Path | None = None
) -> list[str]:
    """Free and total space at the model root on ``model_host``, measured now (read-only).

    Empty without a model host; ``unreachable`` when the host cannot be probed.
    """
    if model_host is None:
        return []
    state = load_state(home)
    try:
        disk = probe_model_disk(runner, runner.probe(model_host), state.models_dir)
    except (SetupError, OSError, ValueError) as exc:
        return [f"model disk on {model_host}: unreachable ({exc})"]
    where = f" at {disk.root}" if disk.root else ""
    total = f" of {disk.total_gb:.1f} GB" if disk.total_gb else ""
    return [
        f"model disk on {model_host}{where}: {disk.free_gb:.1f} GB free{total} "
        f"(reserve {reserve_gb(disk.total_gb):g} GB)"
    ]


__all__ = ["component_lines", "model_disk_lines", "model_lines"]
