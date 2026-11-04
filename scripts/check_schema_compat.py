# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fail if a schema changed in a way that breaks existing records or readers.

Usage: ``python scripts/check_schema_compat.py [--update] [--snapshots DIR]``.

Exports every schema in :data:`spectracut.spec.registry.SCHEMAS` as JSON Schema
and compares it with the committed baseline ``<name>.schema.json``. Breaking
changes (exit 1): a schema, ``$defs`` entry or property removed; a property newly
required; a type, ``$ref``, ``const``, ``format`` or ``pattern`` changed; an enum
value or ``anyOf`` branch removed; a bound tightened; additional properties
forbidden. Compatible changes (a new optional property, schema or enum value)
pass with a note to refresh the baselines with ``--update``.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from spectracut.spec.registry import export_json_schemas

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOTS = ROOT / "src" / "spectracut" / "spec" / "snapshots"

ANNOTATIONS = frozenset({"title", "description", "default", "examples"})
LOWER_BOUNDS = ("minimum", "exclusiveMinimum", "minLength", "minItems", "minProperties")
UPPER_BOUNDS = ("maximum", "exclusiveMaximum", "maxLength", "maxItems", "maxProperties")
EXACT_KEYS = ("$ref", "const", "format", "pattern")

Schema = dict[str, Any]


def _strip(node: Any) -> Any:
    """Drop annotation keywords so comparisons see only validation rules."""
    if isinstance(node, dict):
        return {k: _strip(v) for k, v in node.items() if k not in ANNOTATIONS}
    if isinstance(node, list):
        return [_strip(v) for v in node]
    return node


def _key(node: Any) -> str:
    return json.dumps(_strip(node), sort_keys=True)


def _types(node: Schema) -> set[str] | None:
    t = node.get("type")
    if t is None:
        return None
    return {t} if isinstance(t, str) else set(t)


def _diff_node(old: Schema, new: Schema, path: str, out: list[str]) -> None:
    old_t, new_t = _types(old), _types(new)
    if old_t is not None and new_t is not None and not old_t <= new_t:
        out.append(f"{path}: type changed {sorted(old_t)} -> {sorted(new_t)}")
    elif old_t is None and new_t is not None and "anyOf" not in old and "$ref" not in old:
        out.append(f"{path}: type added {sorted(new_t)}")
    for key in EXACT_KEYS:
        if key in new and old.get(key) != new[key]:
            out.append(f"{path}: {key} changed {old.get(key)!r} -> {new[key]!r}")
        elif key in old and key not in new and key == "$ref" and "anyOf" not in new:
            out.append(f"{path}: $ref {old[key]!r} removed")
    if "enum" in old or "enum" in new:
        if "enum" in new:
            gone = [v for v in old.get("enum", []) if v not in new["enum"]]
            if "enum" not in old:
                out.append(f"{path}: enum added {new['enum']!r}")
            elif gone:
                out.append(f"{path}: enum values removed {gone!r}")
    for key in LOWER_BOUNDS:
        if key in new and (key not in old or new[key] > old[key]):
            out.append(f"{path}: {key} tightened {old.get(key)!r} -> {new[key]!r}")
    for key in UPPER_BOUNDS:
        if key in new and (key not in old or new[key] < old[key]):
            out.append(f"{path}: {key} tightened {old.get(key)!r} -> {new[key]!r}")
    if "anyOf" in old:
        new_branches = {_key(b) for b in new.get("anyOf", [new])}
        for branch in old["anyOf"]:
            if _key(branch) not in new_branches:
                out.append(f"{path}: anyOf branch removed {_key(branch)}")
    elif "anyOf" in new:
        if _key(old) not in {_key(b) for b in new["anyOf"]}:
            out.append(f"{path}: became anyOf without the old type")
    _diff_object(old, new, path, out)
    for key in ("items", "additionalProperties"):
        o, n = old.get(key), new.get(key)
        if isinstance(o, dict) and isinstance(n, dict):
            _diff_node(o, n, f"{path}.{key}", out)
    if "prefixItems" in old and "prefixItems" in new:
        for i, (o, n) in enumerate(zip(old["prefixItems"], new["prefixItems"], strict=False)):
            _diff_node(o, n, f"{path}.prefixItems[{i}]", out)
        if len(old["prefixItems"]) != len(new["prefixItems"]):
            out.append(f"{path}: prefixItems length changed")


def _diff_object(old: Schema, new: Schema, path: str, out: list[str]) -> None:
    old_props: Schema = old.get("properties", {})
    new_props: Schema = new.get("properties", {})
    for name in sorted(old_props):
        if name not in new_props:
            out.append(f"{path}.{name}: property removed")
        else:
            _diff_node(old_props[name], new_props[name], f"{path}.{name}", out)
    newly = sorted(set(new.get("required", [])) - set(old.get("required", [])))
    for name in newly:
        out.append(f"{path}.{name}: newly required")
    old_ap = old.get("additionalProperties", True)
    new_ap = new.get("additionalProperties", True)
    if new_ap is False and old_ap is not False:
        out.append(f"{path}: additionalProperties forbidden")


def diff_schema(old: Schema, new: Schema, name: str = "schema") -> list[str]:
    """Return human-readable breaking changes from ``old`` to ``new`` (empty if none)."""
    out: list[str] = []
    _diff_node(old, new, name, out)
    old_defs: Schema = old.get("$defs", {})
    new_defs: Schema = new.get("$defs", {})
    for def_name in sorted(old_defs):
        if def_name not in new_defs:
            out.append(f"{name}.$defs.{def_name}: definition removed")
        else:
            _diff_node(old_defs[def_name], new_defs[def_name], f"{name}.$defs.{def_name}", out)
    return out


def _dump(schema: Schema) -> str:
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def compare(snapshots: Path, current: dict[str, Schema]) -> tuple[list[str], list[str]]:
    """Return ``(breaking, notes)`` comparing baselines in ``snapshots`` to ``current``."""
    breaking: list[str] = []
    notes: list[str] = []
    baseline = {p.name.removesuffix(".schema.json"): p for p in snapshots.glob("*.schema.json")}
    for name in sorted(baseline):
        if name not in current:
            breaking.append(f"{name}: schema removed")
            continue
        old = json.loads(baseline[name].read_text(encoding="utf-8"))
        changes = diff_schema(old, current[name], name)
        breaking.extend(changes)
        if not changes and _dump(old) != _dump(current[name]):
            notes.append(f"{name}: changed compatibly; refresh with --update")
    for name in sorted(set(current) - set(baseline)):
        notes.append(f"{name}: new schema has no baseline; refresh with --update")
    return breaking, notes


def write(snapshots: Path, current: dict[str, Schema]) -> None:
    snapshots.mkdir(parents=True, exist_ok=True)
    for stale in snapshots.glob("*.schema.json"):
        if stale.name.removesuffix(".schema.json") not in current:
            stale.unlink()
    for name, schema in current.items():
        (snapshots / f"{name}.schema.json").write_text(_dump(schema), encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--update", action="store_true", help="rewrite the baselines")
    parser.add_argument("--snapshots", type=Path, default=SNAPSHOTS, help="baseline directory")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    current = export_json_schemas()
    if args.update:
        write(args.snapshots, current)
        print(f"wrote {len(current)} schema baseline(s) to {args.snapshots}")
        return 0
    breaking, notes = compare(args.snapshots, current)
    for note in notes:
        print(f"note: {note}")
    for change in breaking:
        print(f"breaking: {change}", file=sys.stderr)
    if breaking:
        print(
            f"{len(breaking)} breaking schema change(s); bump the record version and "
            "migrate, or revert",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
