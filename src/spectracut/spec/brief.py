# SPDX-License-Identifier: AGPL-3.0-or-later
"""Brief, CreativeBrief and AudienceProfile: what a production must say, and to whom.

Every proof point cites a declared source, so every on-screen claim traces to a
source in the brief.
"""

from __future__ import annotations

from pydantic import Field, ValidationInfo, field_validator

from spectracut.spec._base import NonEmptyStr, Slug, StrictModel, ensure_unique_ids


class Persona(StrictModel):
    name: NonEmptyStr
    description: NonEmptyStr


class AudienceProfile(StrictModel):
    """Personas, prior knowledge, objections and platform expectations."""

    personas: list[Persona] = Field(min_length=1)
    prior_knowledge: str = ""
    objections: list[NonEmptyStr] = Field(default_factory=list)
    platform_expectations: dict[Slug, NonEmptyStr] = Field(default_factory=dict)


class ClaimSource(StrictModel):
    """A citable source for claims: a URL, document or data reference."""

    id: Slug
    title: NonEmptyStr
    ref: NonEmptyStr


class ProofPoint(StrictModel):
    claim: NonEmptyStr
    source_id: Slug


def _check_proof_sources(points: list[ProofPoint], info: ValidationInfo) -> list[ProofPoint]:
    sources = info.data.get("sources")
    if sources is None:  # sources itself failed validation; reported there
        return points
    known = {s.id for s in sources}
    for i, point in enumerate(points):
        if point.source_id not in known:
            raise ValueError(
                f"proof_points[{i}].source_id {point.source_id!r} is not a declared source"
            )
    return points


class Brief(StrictModel):
    """Product, audience, proof points with sources, offer and CTA."""

    id: Slug
    product: NonEmptyStr
    audience: AudienceProfile
    sources: list[ClaimSource] = Field(min_length=1)
    proof_points: list[ProofPoint] = Field(min_length=1)
    offer: str = ""
    cta: NonEmptyStr

    @field_validator("sources")
    @classmethod
    def _unique_sources(cls, v: list[ClaimSource]) -> list[ClaimSource]:
        ensure_unique_ids(v, "source")
        return v

    @field_validator("proof_points")
    @classmethod
    def _proofs_cite_sources(cls, v: list[ProofPoint], info: ValidationInfo) -> list[ProofPoint]:
        return _check_proof_sources(v, info)


class Assumption(StrictModel):
    """A brief aspect nobody answered, filled with a default and a one-line rationale."""

    aspect: Slug
    value: NonEmptyStr
    rationale: NonEmptyStr


class CreativeBrief(StrictModel):
    """The approved output of Discovery."""

    id: Slug
    message: NonEmptyStr
    audience: AudienceProfile
    sources: list[ClaimSource] = Field(min_length=1)
    proof_points: list[ProofPoint] = Field(min_length=1)
    tone: NonEmptyStr
    success_measure: NonEmptyStr
    offer: str = ""
    cta: NonEmptyStr
    assumptions: list[Assumption] = Field(default_factory=list)
    """Defaults taken for aspects the idea and sources did not answer (intake autofill)."""
    dropped_claims: list[NonEmptyStr] = Field(default_factory=list)
    """Candidate claims dropped for lacking a source or using a forbidden word."""

    @field_validator("sources")
    @classmethod
    def _unique_sources(cls, v: list[ClaimSource]) -> list[ClaimSource]:
        ensure_unique_ids(v, "source")
        return v

    @field_validator("proof_points")
    @classmethod
    def _proofs_cite_sources(cls, v: list[ProofPoint], info: ValidationInfo) -> list[ProofPoint]:
        return _check_proof_sources(v, info)
