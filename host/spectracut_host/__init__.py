# SPDX-License-Identifier: AGPL-3.0-or-later
"""spectracut-host: the render host agent that runs next to Blender on GPU hosts."""

from importlib.metadata import PackageNotFoundError as _PackageNotFoundError
from importlib.metadata import version as _version

try:
    __version__ = _version("spectracut-host")
except _PackageNotFoundError:  # pragma: no cover - source tree without metadata
    __version__ = "0.0.0"
