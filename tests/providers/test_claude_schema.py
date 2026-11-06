# SPDX-License-Identifier: AGPL-3.0-or-later
"""Department output schemas sent to ``claude --json-schema`` stay in strict mode (#265)."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from pydantic import BaseModel, JsonValue

from spectracut.providers.claude_code import claude_text_argv
from spectracut.providers.schema import strict_json_schema
from spectracut.studio.department import DepartmentOutputError
from spectracut.studio.departments import all_departments
from spectracut.studio.departments.art import ArtDirector

#: Draft-07 keywords the Claude CLI's strict validator accepts (no prefixItems,
#: discriminator or format).
STRICT_ALLOWLIST = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "additionalItems",
        "minItems",
        "maxItems",
        "uniqueItems",
        "contains",
        "enum",
        "const",
        "anyOf",
        "oneOf",
        "allOf",
        "not",
        "$ref",
        "$defs",
        "definitions",
        "title",
        "description",
        "default",
        "examples",
        "pattern",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "patternProperties",
        "propertyNames",
        "minProperties",
        "maxProperties",
        "dependencies",
        "if",
        "then",
        "else",
        "$schema",
        "$id",
        "$comment",
    }
)
_NAME_MAPS = {"properties", "$defs", "definitions", "patternProperties"}
_ONE = {"items", "additionalItems", "additionalProperties", "propertyNames", "contains", "not"}
_ONE |= {"if", "then", "else"}
_LISTS = {"anyOf", "oneOf", "allOf"}


def _keywords(schema: Any, found: set[str]) -> set[str]:
    """Every keyword used at a schema position (property names are not keywords)."""
    if not isinstance(schema, dict):
        return found
    for key, value in schema.items():
        found.add(key)
        if key in _NAME_MAPS and isinstance(value, dict):
            for sub in value.values():
                _keywords(sub, found)
        elif key in _ONE or key == "prefixItems":
            for sub in value if isinstance(value, list) else [value]:
                _keywords(sub, found)
        elif key in _LISTS and isinstance(value, list):
            for sub in value:
                _keywords(sub, found)
    return found


def _sent_schema(schema: dict[str, Any]) -> str:
    argv = claude_text_argv("claude", "system", "prompt", schema)
    return argv[argv.index("--json-schema") + 1]


DEPARTMENTS = sorted(all_departments().items())


@pytest.mark.parametrize("dept", [d for _, d in DEPARTMENTS], ids=[n for n, _ in DEPARTMENTS])
def test_department_schemas_sent_to_claude_use_strict_keywords(dept: Any) -> None:
    sent = _sent_schema(dept.output_schema())
    assert "prefixItems" not in sent
    unknown = _keywords(json.loads(sent), set()) - STRICT_ALLOWLIST
    assert not unknown, f"{dept.name}: keywords outside the strict allowlist: {sorted(unknown)}"


def test_art_director_is_among_the_checked_departments() -> None:
    assert "prefixItems" in json.dumps(ArtDirector.output_schema())  # raw schema unchanged
    assert ArtDirector in all_departments().values()


class _Homogeneous(BaseModel):
    rgb: tuple[float, float, float]


class _Mixed(BaseModel):
    pair: tuple[int, str]


def test_homogeneous_tuple_becomes_items_with_exact_length() -> None:
    prop = strict_json_schema(_Homogeneous.model_json_schema())["properties"]["rgb"]
    assert prop["type"] == "array"
    assert prop["items"] == {"type": "number"}
    assert prop["minItems"] == 3 and prop["maxItems"] == 3
    assert "prefixItems" not in prop


def test_mixed_tuple_becomes_items_any_of() -> None:
    prop = strict_json_schema(_Mixed.model_json_schema())["properties"]["pair"]
    assert prop["items"] == {"anyOf": [{"type": "integer"}, {"type": "string"}]}
    assert prop["minItems"] == 2 and prop["maxItems"] == 2


def test_input_is_not_mutated_and_property_names_survive() -> None:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "format": {"type": "string", "format": "uri"},
            "discriminator": {"type": "array", "prefixItems": [{"type": "integer"}]},
        },
        "required": ["format", "discriminator"],
        "discriminator": {"propertyName": "type"},
    }
    before = copy.deepcopy(schema)
    out = strict_json_schema(schema)
    assert schema == before
    assert set(out["properties"]) == {"format", "discriminator"}
    assert out["properties"]["format"] == {"type": "string"}
    assert out["properties"]["discriminator"]["items"] == {"type": "integer"}
    assert "discriminator" not in out
    assert out["required"] == ["format", "discriminator"]


def _art_direction(slope: list[JsonValue]) -> dict[str, JsonValue]:
    return {
        "mood_board": [{"ref": "Apple keynote", "take": "calm light"}],
        "palette": {"swatches": {"primary": "#112233"}},
        "look": {"id": "alterlab", "grade": {"cdl": {"slope": slope}}},
    }


def test_reply_still_validated_against_the_tuple_model() -> None:
    ok = ArtDirector.validate_output("art_direction", _art_direction([1.0, 0.9, 1.1]))
    assert isinstance(ok, dict)
    bad_slopes: list[list[JsonValue]] = [[1.0, 1.0], [1.0, 1.0, 1.0, 1.0], [1.0, "x", 1.0]]
    for bad in bad_slopes:
        with pytest.raises(DepartmentOutputError):
            ArtDirector.validate_output("art_direction", _art_direction(bad))


# --- #269: the sent schema has an object root -----------------------------------


@pytest.mark.parametrize("dept", [d for _, d in DEPARTMENTS], ids=[n for n, _ in DEPARTMENTS])
def test_department_schemas_sent_to_claude_have_object_root(dept: Any) -> None:
    sent = json.loads(_sent_schema(dept.output_schema()))
    assert sent.get("type") == "object", f"{dept.name}: root type is {sent.get('type')!r}"
    assert "$ref" not in sent, f"{dept.name}: root is still a $ref"
    unknown = _keywords(sent, set()) - STRICT_ALLOWLIST
    assert not unknown, f"{dept.name}: keywords outside the strict allowlist: {sorted(unknown)}"


def test_animatic_root_inlines_the_recursive_edit_and_keeps_defs() -> None:
    from spectracut.edit.timeline import Edit
    from spectracut.providers.schema import claude_output_schema
    from spectracut.studio.departments.art import AnimaticEditor

    raw = AnimaticEditor.output_schema()
    assert "$ref" in raw and "type" not in raw  # Pydantic's root $ref is unchanged
    sent, key = claude_output_schema(raw)
    assert key is None
    assert set(Edit.model_fields) <= set(sent["properties"])
    assert "Edit" in sent["$defs"]  # Clip.nested still refers to #/$defs/Edit
    assert "#/$defs/Edit" in json.dumps(sent["$defs"])


def test_object_root_is_sent_unchanged() -> None:
    from spectracut.providers.schema import claude_output_schema

    schema = ArtDirector.output_schema()
    assert claude_output_schema(schema) == (strict_json_schema(schema), None)


def test_non_object_root_is_wrapped_with_its_defs_moved_up() -> None:
    from pydantic import RootModel

    from spectracut.providers.schema import WRAP_KEY, claude_output_schema

    class Rgb(RootModel[list[_Homogeneous]]):
        pass

    raw = Rgb.model_json_schema()
    before = copy.deepcopy(raw)
    sent, key = claude_output_schema(raw)
    assert raw == before
    assert key == WRAP_KEY
    assert sent["type"] == "object"
    assert sent["required"] == [WRAP_KEY] and sent["additionalProperties"] is False
    inner = sent["properties"][WRAP_KEY]
    assert inner["type"] == "array" and "$defs" not in inner
    assert "_Homogeneous" in sent["$defs"]
    assert not _keywords(sent, set()) - STRICT_ALLOWLIST


def _fake_reply(payload: dict[str, JsonValue]) -> str:
    return json.dumps({"is_error": False, "result": "", "structured_output": payload})


def test_wrapped_reply_is_unwrapped_and_validates_against_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pydantic import RootModel

    from spectracut.providers import claude_code
    from spectracut.providers.schema import WRAP_KEY

    class Counts(RootModel[list[int]]):
        pass

    sent: list[str] = []

    def fake_run_cli(argv: list[str], **_: Any) -> str:
        sent.append(argv[argv.index("--json-schema") + 1])
        return _fake_reply({WRAP_KEY: [1, 2, 3]})

    monkeypatch.setattr(claude_code, "run_cli", fake_run_cli)
    completion = claude_code.ClaudeCodeTextProvider("claude").complete(
        "system", "prompt", schema=Counts.model_json_schema()
    )
    assert json.loads(sent[0])["type"] == "object"
    assert completion.output == [1, 2, 3]
    assert Counts.model_validate(completion.output).root == [1, 2, 3]


def test_wrapped_reply_without_the_key_is_a_provider_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from spectracut.providers import claude_code
    from spectracut.providers.base import ProviderError

    monkeypatch.setattr(claude_code, "run_cli", lambda argv, **_: _fake_reply({"other": [1]}))
    with pytest.raises(ProviderError):
        claude_code.ClaudeCodeTextProvider("claude").complete(
            "system", "prompt", schema={"type": "array", "items": {"type": "integer"}}
        )


def test_object_reply_is_returned_as_is(monkeypatch: pytest.MonkeyPatch) -> None:
    from spectracut.providers import claude_code

    reply: dict[str, JsonValue] = {"rgb": [1.0, 0.5, 0.25]}
    monkeypatch.setattr(claude_code, "run_cli", lambda argv, **_: _fake_reply(reply))
    completion = claude_code.ClaudeCodeTextProvider("claude").complete(
        "system", "prompt", schema=_Homogeneous.model_json_schema()
    )
    assert completion.output == reply
