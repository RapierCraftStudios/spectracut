# SPDX-License-Identifier: AGPL-3.0-or-later
"""``spectracut setup``: install, pin and report every bundled component."""

from __future__ import annotations

from pathlib import Path

import spectracut
from spectracut.setup.manifest import Manifest, load_manifest
from spectracut.setup.plan import load_state
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
    return lines


__all__ = ["component_lines"]
