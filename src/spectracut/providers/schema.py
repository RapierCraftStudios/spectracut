# SPDX-License-Identifier: AGPL-3.0-or-later
"""JSON Schema normalisation for ``claude --json-schema`` strict mode.

Pydantic emits JSON Schema 2020-12 (``prefixItems`` for tuples) and OpenAPI's
``discriminator`` for tagged unions. The Claude CLI checks ``--json-schema`` in
strict draft-07 mode and rejects every unknown keyword, so a schema is reduced
to :data:`STRICT_KEYWORDS` before it is sent. The reply is still validated
against the original Pydantic model by the caller, which enforces what the
reduced schema can no longer express (exact tuple member types, the
discriminator).
"""

from __future__ import annotations

import copy
import json
from typing import Any

#: Draft-07 keywords Ajv accepts in strict mode. ``format`` (unknown formats
#: error without ajv-formats), ``prefixItems`` and ``discriminator`` are left out.
STRICT_KEYWORDS: frozenset[str] = frozenset(
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

#: Keywords whose value maps names to schemas.
_SCHEMA_MAPS = frozenset({"properties", "$defs", "definitions", "patternProperties"})
#: Keywords whose value is one schema.
_SCHEMA_ONE = frozenset(
    {"items", "additionalItems", "additionalProperties", "propertyNames", "contains", "not"}
    | {"if", "then", "else"}
)
#: Keywords whose value is a list of schemas.
_SCHEMA_LISTS = frozenset({"anyOf", "oneOf", "allOf"})


def strict_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """A strict-mode copy of ``schema``; the input is never mutated."""
    return _normalise(schema)


def _normalise_any(value: Any) -> Any:
    return _normalise(value) if isinstance(value, dict) else copy.deepcopy(value)


def _normalise(schema: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key == "prefixItems" and isinstance(value, list):
            _tuple_to_items(out, value)
        elif key not in STRICT_KEYWORDS:
            continue
        elif key in _SCHEMA_MAPS and isinstance(value, dict):
            out[key] = {name: _normalise_any(sub) for name, sub in value.items()}
        elif key in _SCHEMA_ONE and isinstance(value, dict):
            out[key] = _normalise(value)
        elif key in _SCHEMA_LISTS and isinstance(value, list):
            out[key] = [_normalise_any(sub) for sub in value]
        elif key == "items" and isinstance(value, list):
            # Draft-07 tuple form: treat it like prefixItems.
            _tuple_to_items(out, value)
        elif key in ("minItems", "maxItems") and key in out:
            continue  # already set from a tuple
        else:
            out[key] = copy.deepcopy(value)
    return out


def _tuple_to_items(out: dict[str, Any], members: list[Any]) -> None:
    """``prefixItems`` ``[s1..sn]`` as one ``items`` schema with exact length ``n``."""
    distinct: list[Any] = []
    seen: set[str] = set()
    for member in members:
        normalised = _normalise_any(member)
        key = json.dumps(normalised, sort_keys=True)
        if key not in seen:
            seen.add(key)
            distinct.append(normalised)
    if len(distinct) == 1:
        out["items"] = distinct[0]
    elif distinct:
        out["items"] = {"anyOf": distinct}
    out["minItems"] = out["maxItems"] = len(members)


#: Property name a non-object root is wrapped under; the provider unwraps it.
WRAP_KEY = "value"

_DEF_MAPS = ("$defs", "definitions")


def claude_output_schema(schema: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """The schema sent to ``claude --json-schema`` and the key its reply is wrapped in.

    A tool input schema needs ``"type": "object"`` at its root. Pydantic writes a
    self-referencing model (``Edit`` nests ``Edit``) as a root ``$ref``; that
    definition is inlined into the root and ``$defs`` kept, so nested references
    still resolve. A root that is still not an object is wrapped as the single
    property :data:`WRAP_KEY`, which is returned so the caller can unwrap the reply.
    Otherwise the key is ``None``. The input is never mutated.
    """
    root = _inline_root_ref(strict_json_schema(schema))
    if root.get("type") == "object":
        return root, None
    defs = {name: root.pop(name) for name in _DEF_MAPS if name in root}
    wrapped: dict[str, Any] = {
        "type": "object",
        "properties": {WRAP_KEY: root},
        "required": [WRAP_KEY],
        "additionalProperties": False,
        **defs,
    }
    return wrapped, WRAP_KEY


def _inline_root_ref(root: dict[str, Any]) -> dict[str, Any]:
    """Replace a local root ``$ref`` (with no ``type``) by the definition it names."""
    seen: set[str] = set()
    while "type" not in root and isinstance(ref := root.get("$ref"), str) and ref not in seen:
        seen.add(ref)
        target = _local_def(root, ref)
        if target is None:
            break
        rest = {key: value for key, value in root.items() if key != "$ref"}
        root = {**copy.deepcopy(target), **rest}
    return root


def _local_def(root: dict[str, Any], ref: str) -> dict[str, Any] | None:
    for name in _DEF_MAPS:
        prefix = f"#/{name}/"
        if ref.startswith(prefix):
            defs = root.get(name)
            target = defs.get(ref[len(prefix) :]) if isinstance(defs, dict) else None
            return target if isinstance(target, dict) else None
    return None
