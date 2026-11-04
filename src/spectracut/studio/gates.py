# SPDX-License-Identifier: AGPL-3.0-or-later
"""Gates: the review a node passes before its dependants may use it.

Modes:

- ``auto``: accepted as soon as the department produces it.
- ``critic``: the Critic scores it; a fail asks the department for a revision
  until the stage's revision budget is spent, then the node is rejected.
- ``human``: the Critic (when one is configured) reviews first, then the node
  waits for a person to approve it; its dependants do not run until then.

A node's mode resolves as: per-node override, then per-production override for
its kind, then the kind default (from the quality profile), then ``auto``.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from pydantic import Field, JsonValue

from spectracut.spec._base import Slug, StrictModel

if TYPE_CHECKING:
    from spectracut.studio.node import NodeSpec


class GateMode(StrEnum):
    AUTO = "auto"
    CRITIC = "critic"
    HUMAN = "human"


class CriticVerdict(StrictModel):
    """The Critic's review of one output against its rubric."""

    passed: bool
    scores: dict[Slug, float] = Field(default_factory=dict)
    comment: str = ""


Critic = Callable[["NodeSpec", JsonValue], CriticVerdict]
"""Reviews a node's output: ``critic(spec, output) -> CriticVerdict``."""


class GateDecision(StrictModel):
    """Outcome of a node's gate, kept on the graph for review and the ledger."""

    mode: GateMode
    critic: CriticVerdict | None = None
    human: Literal["approved", "rejected"] | None = None
    comment: str = ""


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
