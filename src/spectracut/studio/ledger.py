# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cost ledger: one entry per department run of a node.

Cache hits are not runs and write nothing, so the ledger sums to the real cost
of a production: wall time, GPU minutes, AI usage and ElevenLabs characters.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field

from spectracut.spec._base import ContentHash, Slug, StrictModel, canonical_json
from spectracut.studio.node import Tier

NonNegFloat = Annotated[float, Field(ge=0.0)]
NonNegInt = Annotated[int, Field(ge=0)]

Outcome = Literal["accepted", "revise", "rejected", "awaiting_approval", "failed"]


class Usage(StrictModel):
    """Resources one department run consumed, reported by the department."""

    gpu_minutes: NonNegFloat = 0.0
    ai_usage: dict[Slug, NonNegInt] = Field(default_factory=dict)
    """AI usage by metric, e.g. ``{"claude_input_tokens": 1200, "claude_requests": 1}``."""
    elevenlabs_characters: NonNegInt = 0


class LedgerEntry(StrictModel):
    """One department run of one node."""

    production_id: Slug
    node_id: Slug
    kind: Slug
    department: Slug
    node_key: ContentHash
    tier: Tier
    attempt: int = Field(ge=1)
    started_at: datetime
    duration_s: NonNegFloat
    gpu_minutes: NonNegFloat = 0.0
    ai_usage: dict[Slug, NonNegInt] = Field(default_factory=dict)
    elevenlabs_characters: NonNegInt = 0
    outcome: Outcome


class LedgerTotals(StrictModel):
    runs: NonNegInt = 0
    duration_s: NonNegFloat = 0.0
    gpu_minutes: NonNegFloat = 0.0
    ai_usage: dict[Slug, NonNegInt] = Field(default_factory=dict)
    elevenlabs_characters: NonNegInt = 0


class Ledger:
    """Append-only list of :class:`LedgerEntry`."""

    def __init__(self, entries: Iterable[LedgerEntry] = ()) -> None:
        self._entries: list[LedgerEntry] = list(entries)

    def append(self, entry: LedgerEntry) -> None:
        self._entries.append(entry)

    def __len__(self) -> int:
        return len(self._entries)

    def entries(self) -> tuple[LedgerEntry, ...]:
        return tuple(self._entries)

    def entries_for(self, node_id: str) -> tuple[LedgerEntry, ...]:
        return tuple(e for e in self._entries if e.node_id == node_id)

    def totals(self, node_id: str | None = None) -> LedgerTotals:
        """Sum every entry, or only ``node_id``'s entries."""
        entries = self._entries if node_id is None else self.entries_for(node_id)
        ai: dict[str, int] = {}
        for e in entries:
            for metric, amount in e.ai_usage.items():
                ai[metric] = ai.get(metric, 0) + amount
        return LedgerTotals(
            runs=len(entries),
            duration_s=sum(e.duration_s for e in entries),
            gpu_minutes=sum(e.gpu_minutes for e in entries),
            ai_usage=ai,
            elevenlabs_characters=sum(e.elevenlabs_characters for e in entries),
        )

    def to_jsonl(self) -> str:
        """Canonical JSON, one entry per line."""
        return "".join(canonical_json(e).decode("utf-8") + "\n" for e in self._entries)
