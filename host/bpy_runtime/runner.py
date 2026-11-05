# SPDX-License-Identifier: AGPL-3.0-or-later
"""Recipe runner: the entry point inside Blender.

Launched only by the host execution guard as::

    blender -b --factory-startup --python-exit-code 1 --python runner.py -- <manifest>

It refuses to run unless ``SPECTRACUT_EXECUTION_TOKEN`` matches the token in the
manifest the guard wrote, dispatches each step only through the fixed tool
registry, writes any steps' JSON reports to ``report.json`` in the output
directory, then renders the requested frames. It never evaluates code.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sys
from pathlib import Path
from typing import Any

TOKEN_ENV = "SPECTRACUT_EXECUTION_TOKEN"


class RunnerRefused(RuntimeError):
    """The runner was not launched by the execution guard, or the manifest is bad."""


def manifest_path(argv: list[str]) -> Path:
    if "--" not in argv:
        raise RunnerRefused("usage: blender ... --python runner.py -- <manifest.json>")
    rest = argv[argv.index("--") + 1 :]
    if len(rest) != 1:
        raise RunnerRefused("expected exactly one manifest path after --")
    return Path(rest[0])


def load_manifest(path: Path, env: dict[str, str]) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RunnerRefused(f"can't read manifest: {exc}") from None
    token = manifest.get("token") if isinstance(manifest, dict) else None
    if not isinstance(token, str) or not token or env.get(TOKEN_ENV) != token:
        raise RunnerRefused("not launched by the SpectraCut execution guard")
    return manifest


def write_report(
    output_dir: Path, reports: list[dict[str, Any]], scene_graph_hash: str | None = None
) -> Path:
    """Write the steps' reports and the scene-graph hash to ``output_dir/report.json``.

    The write is atomic: Flamenco chunk tasks of one job share the output dir.
    """
    body: dict[str, Any] = {"steps": reports}
    if scene_graph_hash is not None:
        body["scene_graph_hash"] = scene_graph_hash
    try:
        text = json.dumps(body, sort_keys=True, allow_nan=False, indent=2)
    except (TypeError, ValueError) as exc:
        raise RunnerRefused(f"[runtime.error] step reports are not JSON: {exc}") from exc
    path = output_dir / "report.json"
    tmp = output_dir / f".report-{os.getpid()}.json.tmp"
    tmp.write_text(text + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


# --- scene-graph hash -------------------------------------------------------------------

ROUND_DIGITS = 6
_MAX_DEPTH = 4


def plain(value: Any, depth: int = 0) -> Any:
    """Turn a bpy value into canonical JSON data (floats rounded, vectors as lists).

    Anything that is not a number, string or sequence becomes its type name, so
    the result never depends on memory addresses.
    """
    if value is None or isinstance(value, bool | str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return str(value)
        rounded = round(value, ROUND_DIGITS)
        return 0.0 if rounded == 0 else rounded
    if depth < _MAX_DEPTH:
        try:
            items = list(value)
        except TypeError:
            items = None
        if items is not None:
            return [plain(v, depth + 1) for v in items]
    return type(value).__name__


def canonical_scene_hash(state: Any) -> str:
    """sha256 of the canonical JSON of ``state`` (sorted keys, compact, rounded floats)."""
    text = json.dumps(plain_tree(state), sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def plain_tree(state: Any) -> Any:
    if isinstance(state, dict):
        return {str(k): plain_tree(v) for k, v in state.items()}
    if isinstance(state, list | tuple):
        return [plain_tree(v) for v in state]
    return plain(state)


def _name(obj: Any) -> str | None:
    name = getattr(obj, "name", None) if obj is not None else None
    return name if isinstance(name, str) else None


def _items(collection: Any) -> list[Any]:
    try:
        items = list(collection)
    except TypeError:
        return []
    return sorted(items, key=lambda o: _name(o) or "")


def _file_sha256(path: str) -> str | None:
    try:
        with open(path, "rb") as fh:
            return hashlib.file_digest(fh, "sha256").hexdigest()
    except OSError:
        return None


def _image_path(image: Any) -> str | None:
    path = getattr(image, "filepath", None)
    if not isinstance(path, str) or not path or path.startswith("//"):
        return None
    return path


def scene_state(bpy: Any) -> dict[str, Any]:
    """Collect what the frames depend on: objects, materials, images, camera, render.

    Image *contents* are hashed, never their paths, so hosts that mount the assets
    dir at different places (``S:`` on Windows) agree.
    """
    scene = bpy.context.scene
    objects = []
    for obj in _items(bpy.data.objects):
        entry: dict[str, Any] = {
            "name": _name(obj),
            "type": getattr(obj, "type", None),
            "parent": _name(getattr(obj, "parent", None)),
            "matrix_world": getattr(obj, "matrix_world", None),
            "materials": [_name(s) for s in _items(getattr(obj, "material_slots", []))],
        }
        if entry["type"] == "LIGHT":
            data = obj.data
            entry["light"] = {
                "type": getattr(data, "type", None),
                "energy": getattr(data, "energy", None),
                "color": getattr(data, "color", None),
            }
        objects.append(entry)
    materials = []
    for mat in _items(bpy.data.materials):
        nodes = []
        tree = getattr(mat, "node_tree", None) if getattr(mat, "use_nodes", False) else None
        for node in _items(getattr(tree, "nodes", [])):
            inputs = {}
            for sock in getattr(node, "inputs", []):
                if hasattr(sock, "default_value"):
                    key = getattr(sock, "identifier", None) or _name(sock)
                    inputs[str(key)] = sock.default_value
            nodes.append(
                {
                    "name": _name(node),
                    "type": getattr(node, "bl_idname", None),
                    "image": _name(getattr(node, "image", None)),
                    "inputs": inputs,
                }
            )
        materials.append(
            {
                "name": _name(mat),
                "diffuse_color": getattr(mat, "diffuse_color", None),
                "nodes": nodes,
            }
        )
    images = {}
    for image in _items(bpy.data.images):
        path = _image_path(image)
        images[_name(image) or ""] = _file_sha256(path) if path else None
    camera = scene.camera
    cam: dict[str, Any] | None = None
    if camera is not None:
        data = camera.data
        dof = data.dof
        cam = {
            "name": _name(camera),
            "matrix_world": camera.matrix_world,
            "lens": data.lens,
            "sensor_width": data.sensor_width,
            "sensor_height": data.sensor_height,
            "sensor_fit": data.sensor_fit,
            "dof": {
                "use_dof": dof.use_dof,
                "focus_object": _name(dof.focus_object),
                "focus_distance": dof.focus_distance,
                "aperture_fstop": dof.aperture_fstop,
            },
        }
    render = scene.render
    view = scene.view_settings
    settings = {
        "engine": render.engine,
        "resolution": [render.resolution_x, render.resolution_y, render.resolution_percentage],
        "fps": render.fps,
        # The scene's own range, read before the manifest's chunk range is applied,
        # so every chunk of one job hashes the same.
        "frame_range": [scene.frame_start, scene.frame_end],
        "film_transparent": render.film_transparent,
        "use_motion_blur": render.use_motion_blur,
        "motion_blur_shutter": render.motion_blur_shutter,
        "cycles_samples": scene.cycles.samples,
        "eevee_samples": scene.eevee.taa_render_samples,
        "view_transform": view.view_transform,
        "look": view.look,
        "exposure": view.exposure,
        "gamma": view.gamma,
    }
    return {
        "objects": objects,
        "materials": materials,
        "images": images,
        "camera": cam,
        "render": settings,
    }


def scene_graph_hash(bpy: Any) -> str:
    """The deterministic hash of the built scene, computed before the chunk range is set."""
    return canonical_scene_hash(scene_state(bpy))


def run(manifest: dict[str, Any], bpy: Any) -> None:
    from bpy_runtime.ops.scene import set_context
    from bpy_runtime.tools import run_step

    assets_dir = manifest.get("assets_dir")
    set_context(assets_dir if isinstance(assets_dir, str) else None)
    reports: list[dict[str, Any]] = []
    for i, step in enumerate(manifest["steps"]):
        try:
            report = run_step(bpy, step["tool"], step.get("args", {}))
        except Exception as exc:
            code = getattr(exc, "code", None)
            code = code if isinstance(code, str) else "runtime.error"
            raise RunnerRefused(f"step {i} ({step.get('tool')}) [{code}]: {exc}") from exc
        if report is not None:
            reports.append({"index": i, "tool": step["tool"], "report": report})
    # Only ops that return data are listed under "steps"; the scene-graph hash is
    # always written so replays can be compared.
    write_report(Path(manifest["output_dir"]), reports, scene_graph_hash(bpy))
    scene = bpy.context.scene
    scene.frame_start = int(manifest["frame_start"])
    scene.frame_end = int(manifest["frame_end"])
    scene.render.filepath = str(Path(manifest["output_dir"]) / "frame_")
    scene.render.use_file_extension = True
    bpy.ops.render.render(animation=True)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv if argv is None else argv
    try:
        manifest = load_manifest(manifest_path(argv), dict(os.environ))
        # Make ``bpy_runtime`` importable when Blender runs this file as a script.
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import bpy  # type: ignore[import-not-found]

        run(manifest, bpy)
    except RunnerRefused as exc:
        print(f"spectracut runner: refused: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    code = main()
    if code:
        sys.exit(code)
