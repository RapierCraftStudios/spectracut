# SPDX-License-Identifier: AGPL-3.0-or-later
"""SpectraCut: an agent-run CGI studio."""

from importlib.metadata import PackageNotFoundError as _PackageNotFoundError
from importlib.metadata import version as _version

from spectracut import _bpy_guard

_bpy_guard.install()

try:
    __version__ = _version("spectracut")
except _PackageNotFoundError:  # pragma: no cover - source tree without metadata
    __version__ = "0.0.0"
