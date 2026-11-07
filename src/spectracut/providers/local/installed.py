# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which local models are installed on a model host, from setup state.

``spectracut setup`` records each model whose weights it installed as the state
component ``model:<id>`` of the host, valued with :func:`model_version` (a digest of
the pinned sources). Reading that record is free: no ssh round trip to the host.
"""

from __future__ import annotations

from pathlib import Path


def installed_models(home: Path, host: str) -> frozenset[str] | None:
    """Model ids setup installed on ``host`` at their pinned version, or ``None``.

    ``None`` means unknown: no setup state, an unreadable one, or no record of
    ``host`` (a host set up by hand). A host recorded with no models (``setup
    --skip-models``) gives the empty set.
    """
    # Lazy: setup imports studio, which imports this package (a cycle at module level).
    from spectracut.setup.model_assets import model_version
    from spectracut.setup.plan import MODEL_PREFIX, load_state

    try:
        state = load_state(home)
    except (OSError, ValueError):
        return None
    record = state.hosts.get(host)
    if record is None:
        return None
    installed: set[str] = set()
    for component, version in record.components.items():
        if not component.startswith(MODEL_PREFIX):
            continue
        model_id = component[len(MODEL_PREFIX) :]
        try:
            current = model_version(model_id)
        except (KeyError, ValueError):
            continue
        if version == current:
            installed.add(model_id)
    return frozenset(installed)
