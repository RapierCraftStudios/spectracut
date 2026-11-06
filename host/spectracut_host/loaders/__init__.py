# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-family model loaders for ``spectracut-host models-serve``.

Each registry family has one module here, ``spectracut_host.loaders.<family>`` (family
with ``-`` as ``_``, e.g. ``moss_sfx``), exposing ``load(spec) -> session``; the built-in
table in :mod:`spectracut_host.models` names them. A module imports its heavy
dependencies (torch, diffusers, ...) inside ``load``, never at import time, and builds
its session on :class:`~spectracut_host.loaders._base.BaseSession`, which writes to
``out_path``, guards peak VRAM and frees CUDA memory on ``close``. Weights are read from
:func:`~spectracut_host.loaders._base.weights_dir`, where ``spectracut setup`` puts them.
"""

from __future__ import annotations

from spectracut_host.loaders._base import (
    MAX_VRAM_GB,
    BaseSession,
    check_vram_spec,
    source_dir,
    weights_dir,
)

__all__ = ["MAX_VRAM_GB", "BaseSession", "check_vram_spec", "source_dir", "weights_dir"]
