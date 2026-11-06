# SPDX-License-Identifier: AGPL-3.0-or-later
"""The film contract: numbered requirements frozen from the approved brief.

Rows are ``R1..Rn``: the message, each proof point, the CTA, the length, the
aspects, the tone and every brand rule. A contract is immutable. A brief that
changes after approval freezes a new version; earlier versions stay as they were.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Annotated, Literal

from pydantic import Field, JsonValue, StringConstraints

from spectracut.spec._base import ContentHash, NonEmptyStr, Slug, StrictModel, content_hash

RowId = Annotated[str, StringConstraints(pattern=r"^R[1-9][0-9]*$")]
RowKind = Literal["message", "proof_point", "cta", "length", "aspect", "tone", "brand_rule"]


class ContractRow(StrictModel):
    """One requirement the finished film must deliver."""

    id: RowId
    kind: RowKind
    text: NonEmptyStr
    source_id: Slug | None = None
    """The brief source a proof point cites."""


class FilmContract(StrictModel):
    """Numbered rows frozen from one approved brief."""

    version: Annotated[int, Field(ge=1)]
    brief_hash: ContentHash
    rows: tuple[ContractRow, ...] = Field(min_length=1)

    def row_ids(self) -> tuple[str, ...]:
        return tuple(r.id for r in self.rows)


def freeze_contract(
    brief: Mapping[str, JsonValue],
    *,
    length_s: float | None = None,
    aspects: Iterable[str] = (),
    brand_rules: Iterable[str] = (),
    prior: FilmContract | None = None,
) -> FilmContract:
    """Freeze ``brief`` (a dumped Brief or CreativeBrief) into a contract.

    Returns ``prior`` unchanged when the brief has the same hash; otherwise a
    new contract one version above it. ``prior`` is never mutated.
    """
    digest = content_hash(dict(brief))
    if prior is not None and prior.brief_hash == digest:
        return prior
    texts: list[tuple[RowKind, str, str | None]] = []
    message = brief.get("message") or brief.get("product")
    if message:
        texts.append(("message", str(message), None))
    points = brief.get("proof_points")
    for point in points if isinstance(points, list) else []:
        if isinstance(point, dict):
            texts.append(("proof_point", str(point["claim"]), str(point["source_id"])))
    if brief.get("cta"):
        texts.append(("cta", str(brief["cta"]), None))
    if length_s is not None:
        texts.append(("length", f"runs {length_s:g} seconds", None))
    texts.extend(("aspect", f"delivered in {a}", None) for a in aspects)
    if brief.get("tone"):
        texts.append(("tone", str(brief["tone"]), None))
    texts.extend(("brand_rule", f"never uses {w!r}", None) for w in brand_rules)
    rows = tuple(
        ContractRow(id=f"R{i}", kind=kind, text=text, source_id=src)
        for i, (kind, text, src) in enumerate(texts, 1)
    )
    return FilmContract(
        version=prior.version + 1 if prior is not None else 1, brief_hash=digest, rows=rows
    )
