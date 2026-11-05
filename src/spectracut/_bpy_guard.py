# SPDX-License-Identifier: AGPL-3.0-or-later
"""Import blocker: the control plane never imports ``bpy`` or ``bpy_runtime``.

Blender and the bpy runtime run only on registered render hosts, as a separate
Blender process. Importing :mod:`spectracut` installs a :data:`sys.meta_path`
finder that refuses both, so control-plane code can't load Blender by accident,
even when ``host/`` is on ``sys.path`` (editable workspace installs).
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from importlib.abc import MetaPathFinder
from importlib.machinery import ModuleSpec
from types import ModuleType

BLOCKED: tuple[str, ...] = ("bpy", "bpy_runtime")


def is_blocked(fullname: str) -> bool:
    """Return whether ``fullname`` is ``bpy``/``bpy_runtime`` or one of their submodules."""
    return any(fullname == name or fullname.startswith(name + ".") for name in BLOCKED)


class BpyImportBlocker(MetaPathFinder):
    """Meta-path finder that raises :class:`ImportError` for Blender modules."""

    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None,
        target: ModuleType | None = None,
    ) -> ModuleSpec | None:
        if is_blocked(fullname):
            raise ImportError(
                f"{fullname!r} can't be imported in the SpectraCut control plane: "
                "Blender and the bpy runtime run only on registered render hosts",
                name=fullname,
            )
        return None


def install() -> None:
    """Put the blocker first on :data:`sys.meta_path`; calling it again is a no-op."""
    if not any(isinstance(f, BpyImportBlocker) for f in sys.meta_path):
        sys.meta_path.insert(0, BpyImportBlocker())
