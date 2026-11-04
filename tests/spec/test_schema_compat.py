# SPDX-License-Identifier: AGPL-3.0-or-later
"""The schema compatibility check passes on the repo and fails on breaking changes."""

from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_schema_compat.py"

_spec = importlib.util.spec_from_file_location("check_schema_compat", SCRIPT)
assert _spec is not None and _spec.loader is not None
compat = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(compat)
diff_schema = cast(Callable[[dict[str, Any], dict[str, Any], str], list[str]], compat.diff_schema)
main = cast(Callable[[list[str]], int], compat.main)

BASE: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id"],
    "properties": {
        "id": {"type": "string", "pattern": "^[a-z]+$"},
        "kind": {"enum": ["a", "b"], "type": "string"},
        "count": {"type": "integer", "minimum": 0},
        "child": {"$ref": "#/$defs/Child"},
        "note": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None},
    },
    "$defs": {"Child": {"type": "object", "properties": {"x": {"type": "number"}}}},
}


def mutate(fn: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    new = copy.deepcopy(BASE)
    fn(new)
    return new


def test_repo_schemas_match_committed_baselines() -> None:
    result = subprocess.run([sys.executable, str(SCRIPT)], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "note:" not in result.stdout, "baselines are stale; run with --update"


def test_identical_schema_has_no_breaking_changes() -> None:
    assert diff_schema(BASE, copy.deepcopy(BASE), "s") == []


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (lambda s: s["properties"].pop("count"), "s.count: property removed"),
        (lambda s: s["required"].append("count"), "s.count: newly required"),
        (lambda s: s["properties"]["count"].update(type="string"), "type changed"),
        (lambda s: s["properties"]["kind"].update(enum=["a"]), "enum values removed ['b']"),
        (lambda s: s["properties"]["count"].update(minimum=1), "minimum tightened"),
        (lambda s: s["properties"]["id"].update(pattern="^[a-z]{2}$"), "pattern changed"),
        (lambda s: s["properties"]["note"].update(anyOf=[{"type": "string"}]), "anyOf branch"),
        (lambda s: s["properties"]["child"].update({"$ref": "#/$defs/Other"}), "$ref changed"),
        (lambda s: s["$defs"].pop("Child"), "s.$defs.Child: definition removed"),
        (lambda s: s["$defs"]["Child"]["properties"].pop("x"), "Child.x: property removed"),
        (lambda s: s.pop("additionalProperties"), None),
    ],
)
def test_detects_breaking_change(
    change: Callable[[dict[str, Any]], None], expected: str | None
) -> None:
    found = diff_schema(BASE, mutate(change), "s")
    if expected is None:
        assert found == []
    else:
        assert any(expected in f for f in found), found


def test_forbidding_additional_properties_is_breaking() -> None:
    old = mutate(lambda s: s.pop("additionalProperties"))
    assert any("additionalProperties forbidden" in f for f in diff_schema(old, BASE, "s"))


@pytest.mark.parametrize(
    "change",
    [
        lambda s: s["properties"].update(extra={"type": "string", "default": ""}),
        lambda s: s["properties"]["kind"].update(enum=["a", "b", "c"]),
        lambda s: s["properties"]["count"].update(description="how many"),
        lambda s: s["properties"]["count"].pop("minimum"),
        lambda s: s["$defs"].update(New={"type": "object"}),
    ],
)
def test_allows_compatible_change(change: Callable[[dict[str, Any]], None]) -> None:
    assert diff_schema(BASE, mutate(change), "s") == []


def _snapshot(tmp_path: Path) -> Path:
    assert main(["--update", "--snapshots", str(tmp_path)]) == 0
    return tmp_path


def test_main_fails_on_breaking_snapshot_drift(tmp_path: Path, capsys: Any) -> None:
    snaps = _snapshot(tmp_path)
    path = snaps / "recipe.schema.json"
    schema = json.loads(path.read_text())
    schema["properties"]["legacy"] = {"type": "string"}
    path.write_text(json.dumps(schema))
    assert main(["--snapshots", str(snaps)]) == 1
    assert "recipe.legacy: property removed" in capsys.readouterr().err


def test_main_fails_when_schema_removed(tmp_path: Path, capsys: Any) -> None:
    snaps = _snapshot(tmp_path)
    (snaps / "gone.schema.json").write_text(json.dumps({"type": "object"}))
    assert main(["--snapshots", str(snaps)]) == 1
    assert "gone: schema removed" in capsys.readouterr().err


def test_main_passes_on_new_schema_with_note(tmp_path: Path, capsys: Any) -> None:
    snaps = _snapshot(tmp_path)
    (snaps / "spec.schema.json").unlink()
    assert main(["--snapshots", str(snaps)]) == 0
    assert "spec: new schema has no baseline" in capsys.readouterr().out
