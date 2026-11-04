# SPDX-License-Identifier: AGPL-3.0-or-later
"""Production-graph nodes: typed inputs, render tier, status and content keys.

A node is one artifact (brief, script, board, asset, shot, render, mix). Its
*key* is the content hash of everything that determines its output: kind,
department, params, tier and the output hashes of its inputs. Two runs with the
same key produce interchangeable outputs, so the graph reuses them by key.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from pydantic import Field, JsonValue, field_validator, model_validator

from spectracut.spec._base import ContentHash, Slug, StrictModel, content_hash, ensure_finite


class Tier(StrEnum):
    """Render quality tier (spike § Production pipeline, Quality tiers)."""

    PREVIEW = "preview"
    REVIEW = "review"
    FINAL = "final"
    HERO = "hero"


class NodeStatus(StrEnum):
    """Lifecycle of a node in the graph."""

    PENDING = "pending"
    """Added and never built."""
    DIRTY = "dirty"
    """Built before, but the node or one of its inputs changed since."""
    RUNNING = "running"
    """Its department is producing it now."""
    AWAITING_APPROVAL = "awaiting_approval"
    """Produced, waiting on a human gate; dependants do not run."""
    CLEAN = "clean"
    """Built, gate passed, output current."""
    REJECTED = "rejected"
    """The Critic (out of revisions) or a person rejected it; dependants do not run."""
    FAILED = "failed"
    """The department raised an error; dependants do not run."""


NEEDS_BUILD = frozenset({NodeStatus.PENDING, NodeStatus.DIRTY})


class NodeSpec(StrictModel):
    """Declaration of one node: what it is, who makes it, and what it reads."""

    id: Slug
    kind: Slug
    """Artifact kind, e.g. ``brief`` or ``render``; matches a quality-profile stage."""
    department: Slug
    """Department (subagent) that produces it, e.g. ``writer`` or ``lighter``."""
    inputs: tuple[Slug, ...] = ()
    """Ids of upstream nodes whose outputs this node reads."""
    params: dict[str, JsonValue] = Field(default_factory=dict)
    """The node's own declarative inputs."""
    tier: Tier | None = None
    """Explicit tier; ``None`` takes it from the production's quality profile."""

    @field_validator("params")
    @classmethod
    def _finite(cls, v: dict[str, JsonValue]) -> dict[str, JsonValue]:
        ensure_finite(v, "params")
        return v

    @model_validator(mode="after")
    def _inputs_valid(self) -> NodeSpec:
        seen: set[str] = set()
        for i, ident in enumerate(self.inputs):
            if ident == self.id:
                raise ValueError(f"inputs[{i}] {ident!r}: a node cannot read itself")
            if ident in seen:
                raise ValueError(f"inputs[{i}] {ident!r} is listed twice")
            seen.add(ident)
        return self


class NodeState(StrictModel):
    """Current build state of a node, replaced (never mutated) on each change."""

    status: NodeStatus = NodeStatus.PENDING
    tier: Tier | None = None
    key: ContentHash | None = None
    """Key of the last build attempt."""
    output_hash: ContentHash | None = None
    """Content hash of the accepted output; set only when ``status`` is clean."""
    attempts: int = Field(default=0, ge=0)
    """Department runs in the last build (1 plus revisions used)."""
    error: str | None = None


def node_key(spec: NodeSpec, input_hashes: Mapping[str, str], tier: Tier) -> str:
    """Return the content key of ``spec`` built at ``tier`` from the given input outputs.

    ``input_hashes`` maps each input id to that input's accepted output hash. The
    node's own id is not part of the key, so identical work is shared.
    """
    missing = [i for i in spec.inputs if i not in input_hashes]
    if missing:
        raise ValueError(f"node {spec.id!r}: no output hash for inputs {missing}")
    return content_hash(
        {
            "kind": spec.kind,
            "department": spec.department,
            "params": spec.params,
            "tier": tier.value,
            "inputs": {i: input_hashes[i] for i in sorted(spec.inputs)},
        }
    )
