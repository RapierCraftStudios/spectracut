# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared base model, constrained types, canonical JSON and content hashing.

Every record is a frozen Pydantic model that rejects unknown fields, so bad input
fails with a field path instead of being dropped silently. Content hashes are
``sha256:<hex>`` over canonical JSON (sorted keys, compact separators, UTF-8,
no NaN or infinity), which makes them independent of dict key order and of the
interpreter's hash seed.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable
from typing import Annotated, Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Slug = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]*$", max_length=128)]
"""Stable identifier: lowercase letters, digits, ``-`` and ``_``."""

HexColor = Annotated[str, StringConstraints(pattern=r"^#[0-9a-fA-F]{6}$")]
ContentHash = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
Version = Annotated[int, Field(ge=1)]
NonEmptyStr = Annotated[str, StringConstraints(min_length=1)]
UnitFloat = Annotated[float, Field(ge=0.0, le=1.0)]

HASH_PREFIX = "sha256:"


class StrictModel(BaseModel):
    """Frozen, closed record: unknown fields and non-finite floats are rejected."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        validate_default=True,
        allow_inf_nan=False,
    )


def _plain(data: Any) -> Any:
    if isinstance(data, BaseModel):
        return data.model_dump(mode="json")
    return data


def canonical_json(data: Any) -> bytes:
    """Serialise a model or JSON-compatible value to canonical UTF-8 JSON bytes."""
    return json.dumps(
        _plain(data),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def content_hash(
    data: Any,
    *,
    include: Iterable[str] | None = None,
    exclude: Iterable[str] | None = None,
) -> str:
    """Return ``sha256:<hex>`` of the canonical JSON of ``data``.

    For a model, ``include``/``exclude`` select top-level fields before hashing.
    """
    if isinstance(data, BaseModel):
        data = data.model_dump(
            mode="json",
            include=set(include) if include is not None else None,
            exclude=set(exclude) if exclude is not None else None,
        )
    return HASH_PREFIX + hashlib.sha256(canonical_json(data)).hexdigest()


def ensure_finite(value: Any, where: str = "value") -> None:
    """Raise ``ValueError`` if a JSON value contains NaN or infinity."""
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{where} must be a finite number")
    if isinstance(value, dict):
        for key, item in value.items():
            ensure_finite(item, f"{where}.{key}")
    elif isinstance(value, list):
        for i, item in enumerate(value):
            ensure_finite(item, f"{where}[{i}]")


def ensure_unique_ids(items: Iterable[Any], what: str) -> None:
    """Raise ``ValueError`` naming the first duplicated ``id`` in ``items``."""
    seen: set[str] = set()
    for i, item in enumerate(items):
        ident = item.id
        if ident in seen:
            raise ValueError(f"duplicate {what} id {ident!r} at index {i}")
        seen.add(ident)


class HashedModel(StrictModel):
    """A production-graph record with a content hash and dirty state.

    ``dirty`` marks a record whose inputs changed since it was last built. It is
    bookkeeping, so it never contributes to the content hash.
    """

    HASH_EXCLUDE: ClassVar[frozenset[str]] = frozenset({"dirty"})

    dirty: bool = False

    @property
    def content_hash(self) -> str:
        return content_hash(self, exclude=self.HASH_EXCLUDE)
