# SPDX-License-Identifier: AGPL-3.0-or-later
"""Gates: the review a node passes before its dependants may use it.

Modes:

- ``auto``: accepted as soon as the department produces it.
- ``critic``: the Critic scores it; a fail asks the department for a revision
  until the stage's revision budget is spent, then the node is rejected.
- ``human``: the Critic (when one is configured) reviews first, then the node
  waits for a person to approve it; its dependants do not run until then.
- ``auto_delegated``: a ``human`` gate the autonomy policy
  (:mod:`spectracut.studio.autonomy`) hands to a delegate. It passes on its own
  when the panel has no confirmed HIGH, the score clears the threshold and, for
  edit and delivery, conformance passed; otherwise it waits for the owner with
  the reasons, like ``human``.

A node's mode resolves as: per-node override, then per-production override for
its kind, then the kind default (from the quality profile), then ``auto``.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import Field, JsonValue

from spectracut.spec._base import Slug, StrictModel

if TYPE_CHECKING:
    from spectracut.studio.node import NodeSpec


class GateMode(StrEnum):
    AUTO = "auto"
    CRITIC = "critic"
    HUMAN = "human"
    AUTO_DELEGATED = "auto_delegated"


class Severity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Finding(StrictModel):
    """One issue a reviewer found: how bad, how sure, and whether a second check agrees."""

    reviewer: Slug
    severity: Severity
    confidence: Annotated[float, Field(ge=0.0, le=1.0)] = 1.0
    text: str = Field(min_length=1, max_length=4000)
    frame: int | None = Field(default=None, ge=0)
    confirmed: bool = False
    """A HIGH finding a second independent check upheld; only these block a node."""
    key: Slug | None = None
    """Stable slug for the problem; recurrence across productions feeds the failure corpus."""
    reviewers: list[Slug] = Field(default_factory=list)
    """Every reviewer that raised this finding, once duplicates are merged."""


class CriticVerdict(StrictModel):
    """The Critic's review of one output against its rubric."""

    passed: bool
    scores: dict[Slug, float] = Field(default_factory=dict)
    comment: str = ""
    findings: list[Finding] = Field(default_factory=list)
    """Severity-graded findings; empty for a score-only Critic."""
    usage: dict[Slug, Annotated[int, Field(ge=0)]] = Field(default_factory=dict)
    """Judge tokens this review used, by metric; the graph adds them to the node's ledger entry."""


Critic = Callable[["NodeSpec", JsonValue], CriticVerdict]
"""Reviews a node's output: ``critic(spec, output) -> CriticVerdict``."""


class GateDecision(StrictModel):
    """Outcome of a node's gate, kept on the graph for review and the ledger."""

    mode: GateMode
    critic: CriticVerdict | None = None
    human: Literal["approved", "rejected"] | None = None
    comment: str = ""
    approver: Literal["owner", "delegate", "auto"] | None = None
    """Who passed the gate: the owner (a person), the delegate (on evidence) or auto."""
    evidence: list[str] = Field(default_factory=list)
    """What the gate passed on."""
    escalated: list[str] = Field(default_factory=list)
    """Why a delegated gate was handed to the owner."""


class GatePolicy(StrictModel):
    """Gate modes per node kind, with per-production and per-node overrides."""

    defaults: dict[Slug, GateMode] = Field(default_factory=dict)
    """Kind defaults, normally the quality profile's ``gates``."""
    production: dict[Slug, GateMode] = Field(default_factory=dict)
    """This production's overrides, by node kind."""
    nodes: dict[Slug, GateMode] = Field(default_factory=dict)
    """This production's overrides, by node id."""

    def resolve(self, node_id: str, kind: str) -> GateMode:
        if node_id in self.nodes:
            return self.nodes[node_id]
        if kind in self.production:
            return self.production[kind]
        return self.defaults.get(kind, GateMode.AUTO)
