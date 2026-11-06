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
