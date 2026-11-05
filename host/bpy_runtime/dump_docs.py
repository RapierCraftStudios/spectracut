# SPDX-License-Identifier: AGPL-3.0-or-later
"""Operator-only: dump the bpy RNA API index for the harness docs search.

Run by hand on a machine with Blender, never from a job or the recipe runner
(it is not a runtime tool and is not in ``bpy_runtime.tools``)::

    blender --background --factory-startup --python host/bpy_runtime/dump_docs.py -- OUT.json

The output uses the schema of ``src/spectracut/harness/docs/bpy_api.json``
(``path``, ``title``, ``text``, ``signature``). Point ``SPECTRACUT_BPY_DOCS`` at
it to replace the seed index; the control plane attaches ``settable_via``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def _signature(prop: Any) -> str:
    kind = str(prop.type).lower()
    parts = [f"{prop.identifier}: {kind}"]
    if getattr(prop, "array_length", 0):
        parts[0] += f" array of {prop.array_length}"
    if kind == "enum":
        items = [i.identifier for i in prop.enum_items][:12]
        parts.append("one of " + ", ".join(repr(i) for i in items))
    elif kind in ("int", "float"):
        parts.append(f"in [{prop.hard_min}, {prop.hard_max}]")
    if getattr(prop, "is_readonly", False):
        parts.append("read-only")
    return ", ".join(parts)


def dump(bpy: Any) -> list[dict[str, str]]:
    """One entry per RNA property and function of every ``bpy.types`` struct."""
    entries: list[dict[str, str]] = []
    for name in sorted(dir(bpy.types)):
        cls = getattr(bpy.types, name, None)
        rna = getattr(cls, "bl_rna", None)
        if rna is None:
            continue
        for prop in rna.properties:
            if prop.identifier == "rna_type":
                continue
            entries.append(
                {
                    "path": f"bpy.types.{name}.{prop.identifier}",
                    "title": prop.name or prop.identifier,
                    "text": prop.description or "",
                    "signature": _signature(prop),
                }
            )
        for func in rna.functions:
            params = ", ".join(p.identifier for p in func.parameters if not p.is_output)
            entries.append(
                {
                    "path": f"bpy.types.{name}.{func.identifier}",
                    "title": func.identifier,
                    "text": func.description or "",
                    "signature": f"{func.identifier}({params})",
                }
            )
    return entries


def main(argv: list[str]) -> int:
    args = argv[argv.index("--") + 1 :] if "--" in argv else []
    if len(args) != 1:
        print("usage: blender --background --python dump_docs.py -- OUT.json", file=sys.stderr)
        return 2
    import bpy

    entries = dump(bpy)
    Path(args[0]).write_text(json.dumps(entries, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {len(entries)} entries to {args[0]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
