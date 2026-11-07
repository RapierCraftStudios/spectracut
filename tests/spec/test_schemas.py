# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every schema validates, rejects bad input with a field path, and exports JSON Schema."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from spectracut.canon.schema import Canon, CanonPin
from spectracut.spec.asset import Asset, LicenseRecord
from spectracut.spec.brand import BrandKit, Look
from spectracut.spec.brief import Brief
from spectracut.spec.recipe import Recipe
from spectracut.spec.registry import SCHEMAS
from spectracut.spec.spec import Shot

AUDIENCE: dict[str, Any] = {
    "personas": [{"name": "Indie dev", "description": "Ships SaaS alone"}],
    "objections": ["too expensive"],
    "platform_expectations": {"youtube": "chapters"},
}
SOURCES: list[dict[str, Any]] = [{"id": "bench-2026", "title": "Benchmark", "ref": "docs/b.md"}]
PROOFS: list[dict[str, Any]] = [{"claim": "2x faster", "source_id": "bench-2026"}]
LICENSE: dict[str, Any] = {"source": "Poly Haven", "licence": "CC0-1.0", "commercial_safe": True}
SHOT: dict[str, Any] = {"id": "s01", "template_id": "device-hero", "duration_beats": 8}
LOOK: dict[str, Any] = {"id": "clean", "grain": 0.02}
STEPS: list[dict[str, Any]] = [
    {"index": 0, "tool": "scene.new", "args": {}},
    {"index": 1, "tool": "camera.set_lens", "args": {"mm": 35}},
]

VALID: dict[str, dict[str, Any]] = {
    "audience_profile": AUDIENCE,
    "brief": {
        "id": "launch",
        "product": "SpectraCut",
        "audience": AUDIENCE,
        "sources": SOURCES,
        "proof_points": PROOFS,
        "cta": "Try it",
    },
    "creative_brief": {
        "id": "launch",
        "message": "Agents can direct film",
        "audience": AUDIENCE,
        "sources": SOURCES,
        "proof_points": PROOFS,
        "tone": "confident",
        "success_measure": "signups",
        "cta": "Try it",
    },
    "look": LOOK,
    "brand_kit": {
        "id": "spellwise",
        "name": "Spellwise",
        "palette": {"swatches": {"primary": "#1A2B3C"}},
        "typography": {"families": {"display": "Inter"}},
        "tone": "warm",
        "looks": [LOOK],
        "default_look_id": "clean",
        "voice": {"provider": "elevenlabs", "voice_id": "v1"},
        "forbidden_words": ["Revolutionary", "revolutionary"],
        "claim_sources": SOURCES,
        "token_sources": {
            "palette.primary": {
                "path": "src/app/globals.css",
                "line": 7,
                "token": "--primary",
                "value": "#1a2b3c",
            }
        },
    },
    "license_record": LICENSE,
    "asset": {
        "id": "phone",
        "kind": "model",
        "name": "Phone",
        "file_ref": "a.glb",
        "license": LICENSE,
    },
    "canon": {
        "id": "verse",
        "scope": "universe",
        "characters": [{"id": "ada", "name": "Ada", "model_asset_id": "ada-mesh"}],
        "props": [{"id": "key", "name": "Key", "asset_id": "key-mesh"}],
        "continuity": [{"scene_id": "sc1", "time_of_day": "dusk", "holdings": {"ada": ["key"]}}],
    },
    "shot": SHOT,
    "spec": {
        "id": "ad-1",
        "format": "ad",
        "length_s": 15,
        "aspects": ["16:9", "9:16"],
        "shots": [SHOT],
    },
    "recipe": {"id": "r1", "shot_id": "s01", "blender_version": "4.2", "steps": STEPS},
}


def _with(name: str, path: tuple[str | int, ...], value: Any) -> dict[str, Any]:
    data = copy.deepcopy(VALID[name])
    node: Any = data
    for part in path[:-1]:
        node = node[part]
    node[path[-1]] = value
    return data


# (schema, path to corrupt, bad value, expected error loc)
BAD: list[tuple[str, tuple[str | int, ...], Any, tuple[str | int, ...]]] = [
    ("audience_profile", ("personas",), [], ("personas",)),
    ("brief", ("proof_points", 0, "source_id"), "unknown", ("proof_points",)),
    ("brief", ("audience", "personas", 0, "name"), "", ("audience", "personas", 0, "name")),
    ("creative_brief", ("tone",), "", ("tone",)),
    ("look", ("grain",), 0.5, ("qa_thresholds",)),
    ("look", ("lens",), {"vignette": 2.0}, ("lens", "vignette")),
    ("brand_kit", ("palette", "swatches", "primary"), "blue", ("palette", "swatches", "primary")),
    ("brand_kit", ("default_look_id",), "missing", ("default_look_id",)),
    (
        "brand_kit",
        ("token_sources", "palette.primary", "line"),
        0,
        ("token_sources", "palette.primary", "line"),
    ),
    ("license_record", ("non_commercial",), True, ("commercial_safe",)),
    ("asset", ("recipe_ref",), "r1", ("file_ref",)),
    ("asset", ("kind",), "video", ("kind",)),
    ("canon", ("continuity", 0, "holdings"), {"ada": ["sword"]}, ("continuity",)),
    ("canon", ("scope",), "galaxy", ("scope",)),
    ("shot", ("duration_beats",), 0, ("duration_beats",)),
    ("spec", ("shots", 0, "duration_beats"), -1, ("shots", 0, "duration_beats")),
    ("spec", ("aspects",), ["16:9", "16:9"], ("aspects",)),
    ("recipe", ("steps", 1, "index"), 5, ("steps",)),
    ("recipe", ("steps", 1, "tool"), "import os", ("steps", 1, "tool")),
    ("recipe", ("steps", 1, "args", "mm"), float("nan"), ("steps", 1, "args", "mm", "float")),
]


def test_every_registered_schema_has_fixtures() -> None:
    assert set(VALID) == set(SCHEMAS)
    assert {case[0] for case in BAD} == set(SCHEMAS)


@pytest.mark.parametrize("name", sorted(VALID))
def test_valid_fixture_round_trips(name: str) -> None:
    model: type[BaseModel] = SCHEMAS[name]
    obj = model.model_validate(VALID[name])
    again = model.model_validate_json(obj.model_dump_json())
    assert again == obj


@pytest.mark.parametrize("name", sorted(VALID))
def test_exports_json_schema(name: str) -> None:
    schema = SCHEMAS[name].model_json_schema()
    assert schema["type"] == "object"
    assert schema["properties"]
    assert schema["additionalProperties"] is False
    json.dumps(schema)


@pytest.mark.parametrize("name", sorted(VALID))
def test_rejects_unknown_field(name: str) -> None:
    with pytest.raises(ValidationError) as exc:
        SCHEMAS[name].model_validate({**VALID[name], "surprise": 1})
    assert ("surprise",) in [e["loc"] for e in exc.value.errors()]


@pytest.mark.parametrize(("name", "path", "value", "loc"), BAD)
def test_rejects_bad_input_with_field_path(
    name: str, path: tuple[str | int, ...], value: Any, loc: tuple[str | int, ...]
) -> None:
    with pytest.raises(ValidationError) as exc:
        SCHEMAS[name].model_validate(_with(name, path, value))
    assert [e["loc"] for e in exc.value.errors()] == [loc]


def test_asset_requires_license() -> None:
    data = copy.deepcopy(VALID["asset"])
    del data["license"]
    with pytest.raises(ValidationError) as exc:
        Asset.model_validate(data)
    assert exc.value.errors()[0]["loc"] == ("license",)


def test_non_commercial_licence_cannot_be_commercial_safe() -> None:
    ok = LicenseRecord(
        source="BBC SFX", licence="RemArc", non_commercial=True, commercial_safe=False
    )
    assert not ok.commercial_safe
    with pytest.raises(ValidationError, match="non-commercial"):
        LicenseRecord(source="BBC SFX", licence="RemArc", non_commercial=True, commercial_safe=True)


def test_forbidden_words_are_lowercased_and_deduplicated() -> None:
    kit = BrandKit.model_validate(VALID["brand_kit"])
    assert kit.forbidden_words == ["revolutionary"]


def test_brief_error_names_the_bad_proof_point() -> None:
    with pytest.raises(ValidationError, match=r"proof_points\[0\]\.source_id 'unknown'"):
        Brief.model_validate(_with("brief", ("proof_points", 0, "source_id"), "unknown"))


def test_models_are_frozen() -> None:
    look = Look.model_validate(LOOK)
    with pytest.raises(ValidationError):
        look.grain = 0.0  # type: ignore[misc]


def test_shot_and_asset_carry_hash_and_dirty_state() -> None:
    shot = Shot.model_validate(SHOT)
    dirty = Shot.model_validate({**SHOT, "dirty": True})
    assert shot.dirty is False and dirty.dirty is True
    assert shot.content_hash == dirty.content_hash
    assert shot.content_hash != Shot.model_validate({**SHOT, "duration_beats": 4}).content_hash
    asset = Asset.model_validate(VALID["asset"])
    assert asset.content_hash.startswith("sha256:")


def test_recipe_rejects_code_fields() -> None:
    with pytest.raises(ValidationError):
        Recipe.model_validate({**VALID["recipe"], "python": "import os"})


def test_canon_pin() -> None:
    assert CanonPin(canon_id="verse", version=2).version == 2
    with pytest.raises(ValidationError):
        CanonPin(canon_id="verse", version=0)


def test_canon_rejects_duplicate_ids() -> None:
    chars = VALID["canon"]["characters"] * 2
    with pytest.raises(ValidationError, match="duplicate character id 'ada'") as exc:
        Canon.model_validate({**VALID["canon"], "characters": chars})
    assert exc.value.errors()[0]["loc"] == ("characters",)
