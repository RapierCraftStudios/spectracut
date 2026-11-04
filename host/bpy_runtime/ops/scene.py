# SPDX-License-Identifier: AGPL-3.0-or-later
"""Scene ops: ``object.import_gltf`` from the host's assets directory.

The assets root comes from the run manifest the execution guard writes (the
guard reads ``SPECTRACUT_ASSETS_DIR``; Blender's own environment has every
``SPECTRACUT_*`` variable removed). It is never taken from recipe arguments.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from bpy_runtime.ops import Report, ToolError, as_name, as_vec3, check_only

_CONTEXT: dict[str, Path | None] = {"assets_dir": None}
GLTF_SUFFIXES = (".gltf", ".glb")


def set_context(assets_dir: str | None) -> None:
    """Set the assets root for this run (called by the runner from the manifest)."""
    _CONTEXT["assets_dir"] = Path(assets_dir) if assets_dir else None


def resolve_asset(path: Any, assets_dir: Path | None, suffixes: tuple[str, ...]) -> Path:
    """Resolve ``path`` inside ``assets_dir`` or raise ``asset.path_refused``."""
    if assets_dir is None:
        raise ToolError(
            "no assets directory is configured on this host (SPECTRACUT_ASSETS_DIR)",
            "asset.path_refused",
        )
    if not isinstance(path, str) or not 0 < len(path) <= 1024 or "\x00" in path:
        raise ToolError("path must be a non-empty relative string", "asset.path_refused")
    posix, win = PurePosixPath(path), PureWindowsPath(path)
    if posix.is_absolute() or win.is_absolute() or win.drive or path.startswith(("/", "\\")):
        raise ToolError(
            f"path {path!r} must be relative to the assets directory", "asset.path_refused"
        )
    if ".." in posix.parts or ".." in win.parts:
        raise ToolError(f"path {path!r} must not contain '..'", "asset.path_refused")
    if not path.lower().endswith(suffixes):
        raise ToolError(
            f"path {path!r} must end with one of {list(suffixes)}", "asset.path_refused"
        )
    root = Path(os.path.realpath(assets_dir))
    full = Path(os.path.realpath(root / posix))
    if not full.is_relative_to(root):
        raise ToolError(f"path {path!r} leaves the assets directory", "asset.path_refused")
    if not full.is_file():
        raise ToolError(f"asset {path!r} does not exist", "asset.path_refused")
    return full


def object_import_gltf(bpy: Any, args: dict[str, Any]) -> Report:
    check_only(args, {"path", "name", "location", "scale"}, "object.import_gltf")
    full = resolve_asset(args.get("path"), _CONTEXT["assets_dir"], GLTF_SUFFIXES)
    name = as_name(args["name"], "name") if "name" in args else None
    location = as_vec3(args["location"], "location") if "location" in args else None
    scale = as_vec3(args["scale"], "scale") if "scale" in args else None
    before = {o.name for o in bpy.data.objects}
    bpy.ops.import_scene.gltf(filepath=str(full))
    new = [o for o in bpy.data.objects if o.name not in before]
    top = [o for o in new if o.parent is None or o.parent not in new]
    if name is not None and len(top) == 1:
        top[0].name = name
    for obj in top:
        if location is not None:
            obj.location = location
        if scale is not None:
            obj.scale = scale
    return {"objects": sorted(str(o.name) for o in new), "top_level": [str(o.name) for o in top]}
