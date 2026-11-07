# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared session helpers for the per-family loaders.

A loader is ``load(spec) -> session``: it checks the spec (:func:`check_vram_spec`),
finds its weights (:func:`weights_dir`) and returns a :class:`BaseSession` subclass that
implements ``_generate`` (write one output to ``out_path``) and ``_unload`` (drop the
model). torch is imported lazily and only when installed, so this module imports on any
host.
"""

from __future__ import annotations

import gc
import importlib
from pathlib import Path
from typing import Any

from spectracut.providers.base import ProviderError
from spectracut.providers.local.runtime import ModelSpec
from spectracut.setup.model_assets import SRC_DIR, asset_for, default_model_root

MAX_VRAM_GB = 8
"""The model host's card (legion); every model must load and run within it."""
GIB = 1024**3


def _torch() -> Any | None:
    """torch when it is installed, else ``None`` (no family deps on this host)."""
    try:
        return importlib.import_module("torch")
    except ImportError:
        return None


def check_vram_spec(spec: ModelSpec) -> None:
    """Refuse a model whose declared VRAM does not fit the model host's card."""
    if spec.vram_gb > MAX_VRAM_GB:
        raise ProviderError(
            "local", f"{spec.id} needs {spec.vram_gb} GB VRAM; the model host has {MAX_VRAM_GB}"
        )


def model_dir(spec: ModelSpec, root: Path | None = None) -> Path:
    """``<model root>/<family>/<model id>``, where setup puts this model's files."""
    return (root or default_model_root()) / spec.family / spec.id


def weights_dir(spec: ModelSpec, repo: str | None = None, *, root: Path | None = None) -> Path:
    """The local copy of ``repo`` (default: the model's first weights repo) for ``spec``.

    Raises ``ProviderError`` naming ``spectracut setup`` when the weights are not there.
    """
    try:
        asset = asset_for(spec.id)
    except KeyError as exc:
        raise ProviderError("local", f"{spec.id}: no weights are registered for it") from exc
    source = next((w for w in asset.weights if repo is None or w.repo == repo), None)
    if source is None:
        raise ProviderError("local", f"{spec.id}: {repo!r} is not one of its weights repos")
    path = model_dir(spec, root) / source.subdir
    if not path.is_dir():
        raise ProviderError(
            "local", f"{spec.id}: weights not installed at {path}: run `spectracut setup`"
        )
    return path


def source_dir(spec: ModelSpec, *, root: Path | None = None) -> Path:
    """The pinned source checkout a family's loader imports (``.../<model id>/src``)."""
    path = model_dir(spec, root) / SRC_DIR
    if not path.is_dir():
        raise ProviderError(
            "local", f"{spec.id}: source not installed at {path}: run `spectracut setup`"
        )
    return path


class BaseSession:
    """A loaded model: ``generate`` writes ``out_path``, ``close`` frees the GPU."""

    def __init__(self, spec: ModelSpec) -> None:
        check_vram_spec(spec)
        self.spec = spec
        self.closed = False
        torch = _torch()
        if torch is not None and torch.cuda.is_available():
            torch.cuda.reset_peak_memory_stats()

    def _generate(self, prompt: str, out_path: Path, **options: Any) -> None:
        raise NotImplementedError

    def _unload(self) -> None:
        """Drop references to the model (the subclass's attributes)."""

    def generate(self, prompt: str, *, out_path: Path, **options: Any) -> Path:
        if self.closed:
            raise ProviderError("local", f"{self.spec.id}: session is closed")
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        self._generate(prompt, out_path, **options)
        if not out_path.is_file():
            raise ProviderError("local", f"{self.spec.id}: no output written to {out_path}")
        self.check_peak_vram()
        return out_path

    def peak_vram_bytes(self) -> int | None:
        """Peak CUDA memory allocated since load, or ``None`` without CUDA."""
        torch = _torch()
        if torch is None or not torch.cuda.is_available():
            return None
        return int(torch.cuda.max_memory_allocated())

    def check_peak_vram(self) -> None:
        """Raise when the peak exceeded the model's declared ``vram_gb`` (at most 8 GB)."""
        peak = self.peak_vram_bytes()
        limit = min(self.spec.vram_gb, MAX_VRAM_GB) * GIB
        if peak is not None and peak > limit:
            raise ProviderError(
                "local",
                f"{self.spec.id}: peak VRAM {peak / GIB:.2f} GB exceeds {limit / GIB:.0f} GB",
            )

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self._unload()
        finally:
            gc.collect()
            torch = _torch()
            if torch is not None and torch.cuda.is_available():
                torch.cuda.empty_cache()
