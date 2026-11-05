# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deterministic mock text provider for dev and CI (no network, no CLI).

By default it answers with a minimal JSON instance of the requested schema, with
values derived from a hash of the system prompt and prompt. It understands the
schemas pydantic emits (``model_json_schema()``): objects with required
properties, scalar types, arrays, ``enum``/``const``, ``anyOf``/``oneOf``/``allOf``
and local ``$ref`` into ``$defs``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any

from pydantic import JsonValue

from spectracut.providers.base import Completion, ProviderError

Responder = Callable[[str, str, dict[str, Any]], JsonValue]

_MAX_DEPTH = 32


class MockTextProvider:
    """Returns a schema-valid instance, or whatever ``respond`` returns."""

    name = "mock-text"

    def __init__(self, respond: Responder | None = None) -> None:
        self._respond = respond

    def complete(self, system: str, prompt: str, *, schema: dict[str, Any]) -> Completion:
        if self._respond is not None:
            output = self._respond(system, prompt, schema)
        else:
            seed = hashlib.sha256(f"{system}\n{prompt}".encode()).hexdigest()
            output = instance_of(schema, seed)
        usage = {"mock_text_requests": 1, "mock_text_input_chars": len(system) + len(prompt)}
        return Completion(output=output, usage=usage)


def instance_of(schema: dict[str, Any], seed: str = "0" * 64) -> JsonValue:
    """A minimal JSON value satisfying ``schema`` (see the module docstring)."""
    return _build(schema, schema.get("$defs", {}), seed, 0)


def _build(node: dict[str, Any], defs: dict[str, Any], seed: str, depth: int) -> JsonValue:
    if depth > _MAX_DEPTH:
        raise ProviderError("mock-text", "schema nests too deeply")
    ref = node.get("$ref")
    if isinstance(ref, str):
        prefix = "#/$defs/"
        if not ref.startswith(prefix) or ref[len(prefix) :] not in defs:
            raise ProviderError("mock-text", f"unsupported $ref {ref!r}")
        return _build(defs[ref[len(prefix) :]], defs, seed, depth + 1)
    if "const" in node:
        return node["const"]  # type: ignore[no-any-return]
    if node.get("enum"):
        return node["enum"][0]  # type: ignore[no-any-return]
    for key in ("anyOf", "oneOf", "allOf"):
        branches = node.get(key)
        if branches:
            return _build(branches[0], defs, seed, depth + 1)
    kind = node.get("type")
    if isinstance(kind, list):
        kind = kind[0] if kind else None
    if kind is None:
        kind = "object" if "properties" in node else "string"
    if kind == "object":
        props: dict[str, Any] = node.get("properties", {})
        return {
            name: _build(props.get(name, {}), defs, seed, depth + 1)
            for name in node.get("required", [])
        }
    if kind == "array":
        count = max(int(node.get("minItems", 1)), 0)
        if "maxItems" in node:
            count = min(count, int(node["maxItems"]))
        items = node.get("items", {})
        return [_build(items, defs, f"{seed[1:]}{seed[0]}", depth + 1) for _ in range(count)]
    if kind == "string":
        text = f"mock-{seed[:8]}"
        min_len = int(node.get("minLength", 0))
        if len(text) < min_len:
            text = text + "x" * (min_len - len(text))
        if "maxLength" in node:
            text = text[: int(node["maxLength"])]
        return text
    if kind in ("integer", "number"):
        value: float = int(seed[:4], 16) % 100
        if "minimum" in node:
            value = max(value, node["minimum"])
        if "exclusiveMinimum" in node:
            value = max(value, node["exclusiveMinimum"] + 1)
        if "maximum" in node:
            value = min(value, node["maximum"])
        if "exclusiveMaximum" in node:
            value = min(value, node["exclusiveMaximum"] - (1 if kind == "integer" else 0.5))
        return int(value) if kind == "integer" else float(value)
    if kind == "boolean":
        return int(seed[0], 16) % 2 == 0
    if kind == "null":
        return None
    raise ProviderError("mock-text", f"unsupported schema type {kind!r}")
