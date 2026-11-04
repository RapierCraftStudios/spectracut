# SPDX-License-Identifier: AGPL-3.0-or-later
"""Recipe hashing is stable across Python runs and key order."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from typing import Any

from spectracut.spec._base import canonical_json
from spectracut.spec.recipe import Recipe

STEPS: list[dict[str, Any]] = [
    {"index": 0, "tool": "scene.new", "args": {}},
    {
        "index": 1,
        "tool": "camera.set",
        "args": {"lens_mm": 35, "target": [0.0, 1.5, -2.0], "name": "cam·α", "dof": True},
    },
    {"index": 2, "tool": "light.area", "args": {"power": 400.5, "size": {"x": 1, "y": 2}}},
]
RECIPE: dict[str, Any] = {"id": "r1", "shot_id": "s01", "blender_version": "4.2.3", "steps": STEPS}

# Golden value: changes only if canonical serialisation or hash content changes.
GOLDEN = "sha256:aa4a53b1e25ed6d4138ef1fa25fc91696e5f06d7b3b7c7e0b47301223f4f86ef"

SNIPPET = """
import json, sys
from spectracut.spec.recipe import Recipe
print(Recipe.model_validate(json.loads(sys.argv[1])).content_hash)
"""


def _reverse(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _reverse(value[k]) for k in reversed(list(value))}
    if isinstance(value, list):
        return [_reverse(v) for v in value]
    return value


def test_hash_format() -> None:
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", Recipe.model_validate(RECIPE).content_hash)


def test_hash_is_independent_of_key_order() -> None:
    reordered = _reverse(RECIPE)
    assert list(reordered) != list(RECIPE)
    assert (
        Recipe.model_validate(reordered).content_hash == Recipe.model_validate(RECIPE).content_hash
    )


def test_hash_ignores_identity_fields() -> None:
    a = Recipe.model_validate(RECIPE)
    b = Recipe.model_validate({**RECIPE, "id": "other", "version": 7})
    assert a.content_hash == b.content_hash


def test_hash_changes_with_content() -> None:
    base = Recipe.model_validate(RECIPE).content_hash
    steps = [dict(s) for s in STEPS]
    steps[1] = {**steps[1], "args": {**steps[1]["args"], "lens_mm": 50}}
    assert Recipe.model_validate({**RECIPE, "steps": steps}).content_hash != base
    assert Recipe.model_validate({**RECIPE, "blender_version": "4.3"}).content_hash != base
    assert Recipe.model_validate({**RECIPE, "parent_hash": base}).content_hash != base


def test_hash_is_stable_across_python_runs() -> None:
    payload = canonical_json(RECIPE).decode()
    hashes = set()
    for seed in ("0", "12345", "random"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        out = subprocess.run(
            [sys.executable, "-c", SNIPPET, payload],
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        hashes.add(out.stdout.strip())
    assert hashes == {Recipe.model_validate(RECIPE).content_hash}


def test_golden_hash() -> None:
    assert Recipe.model_validate(RECIPE).content_hash == GOLDEN


def test_hash_is_sha256_of_sorted_compact_json() -> None:
    import hashlib
    import json

    content = {"blender_version": "4.2.3", "parent_hash": None, "shot_id": "s01", "steps": STEPS}
    raw = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    expected = "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()
    assert Recipe.model_validate(RECIPE).content_hash == expected
