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

import json
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


def write_report(output_dir: Path, reports: list[dict[str, Any]]) -> Path:
    """Write the steps' JSON reports to ``output_dir/report.json``."""
    try:
        text = json.dumps({"steps": reports}, sort_keys=True, allow_nan=False, indent=2)
    except (TypeError, ValueError) as exc:
        raise RunnerRefused(f"[runtime.error] step reports are not JSON: {exc}") from exc
    path = output_dir / "report.json"
    path.write_text(text + "\n", encoding="utf-8")
    return path


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
    if reports:
        # Only ops that return data produce a report; plain build steps leave the
        # output dir with frames only.
        write_report(Path(manifest["output_dir"]), reports)
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
