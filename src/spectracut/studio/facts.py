# SPDX-License-Identifier: AGPL-3.0-or-later
"""Film facts: what each built node decided, rejected and drew critique for.

Every built node leaves a bounded :class:`FactsRecord`. Decisions anchored to a
shot, look or asset id come back to later nodes that touch that id as
:class:`PreserveRow` constraints, which the gate checks. Critique findings that
recur across productions are proposed into the Critic's failure corpus.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterable
from pathlib import Path

from pydantic import Field, JsonValue, ValidationError

from spectracut.spec._base import Slug, StrictModel, canonical_json

MAX_ENTRIES = 20
MAX_TEXT = 300
RECURRENCE_THRESHOLD = 3
MAX_EXAMPLES = 5


class Decision(StrictModel):
    text: str = Field(min_length=1)
    anchors: tuple[Slug, ...] = ()
    """Shot, look or asset ids the decision is pinned to."""
    rejected: tuple[str, ...] = ()
    """Phrases a conforming output must not contain."""


class RejectedApproach(StrictModel):
    approach: str = Field(min_length=1)
    why: str


class Finding(StrictModel):
    key: Slug
    example: str


class FactsDraft(StrictModel):
    """What a department reports about its own run."""

    decisions: tuple[Decision, ...] = ()
    rejected: tuple[RejectedApproach, ...] = ()


class FactsRecord(StrictModel):
    production_id: Slug
    node_id: Slug
    decisions: tuple[Decision, ...] = ()
    rejected: tuple[RejectedApproach, ...] = ()
    findings: tuple[Finding, ...] = ()


class PreserveRow(StrictModel):
    """A decision from an earlier node that a later node must keep."""

    source: Slug
    anchor: Slug
    decision: str
    rejected: tuple[str, ...] = ()


def _clip(text: str) -> str:
    return text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 1] + "…"


def bounded(record: FactsRecord) -> FactsRecord:
    """Cap ``record`` to :data:`MAX_ENTRIES` rows of at most :data:`MAX_TEXT` characters."""
    return record.model_copy(
        update={
            "decisions": tuple(
                Decision(
                    text=_clip(d.text),
                    anchors=d.anchors[:MAX_ENTRIES],
                    rejected=tuple(_clip(r) for r in d.rejected[:MAX_ENTRIES]),
                )
                for d in record.decisions[:MAX_ENTRIES]
            ),
            "rejected": tuple(
                RejectedApproach(approach=_clip(r.approach), why=_clip(r.why))
                for r in record.rejected[:MAX_ENTRIES]
            ),
            "findings": tuple(
                Finding(key=f.key, example=_clip(f.example)) for f in record.findings[:MAX_ENTRIES]
            ),
        }
    )


def touched_ids(node_id: str, inputs: Iterable[str], params: JsonValue) -> frozenset[str]:
    """Ids a node touches: itself, its inputs and every string in its params."""
    found = {node_id, *inputs}

    def walk(value: JsonValue) -> None:
        if isinstance(value, str):
            found.add(value)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)

    walk(params)
    return frozenset(found)


class FactsStore:
    """Facts records of one production; saved as ``<root>/<node_id>.json`` when ``root`` is set."""

    def __init__(self, production_id: str, root: Path | None = None) -> None:
        self.production_id = production_id
        self.root = root
        self._records: dict[str, FactsRecord] = {}
        self._lock = threading.Lock()

    def write(self, record: FactsRecord) -> FactsRecord:
        record = bounded(record)
        with self._lock:
            self._records[record.node_id] = record
            if self.root is not None:
                self.root.mkdir(parents=True, exist_ok=True)
                path = self.root / f"{record.node_id}.json"
                tmp = path.with_suffix(".json.tmp")
                tmp.write_bytes(canonical_json(record))
                tmp.replace(path)
        return record

    def get(self, node_id: str) -> FactsRecord | None:
        with self._lock:
            return self._records.get(node_id)

    def preserve_for(self, node_id: str, touched: frozenset[str]) -> tuple[PreserveRow, ...]:
        """Anchored decisions of other nodes whose anchor this node touches."""
        with self._lock:
            records = [r for n, r in sorted(self._records.items()) if n != node_id]
        return tuple(
            PreserveRow(source=r.node_id, anchor=a, decision=d.text, rejected=d.rejected)
            for r in records
            for d in r.decisions
            for a in d.anchors
            if a in touched
        )


def check_preserve(rows: Iterable[PreserveRow], output: JsonValue) -> list[str]:
    """Violations of ``rows`` by ``output``: a rejected phrase that the output contains."""
    text = json.dumps(output, sort_keys=True, default=str).lower()
    return [
        f"{row.anchor}: output uses {phrase!r}, rejected by {row.source} ({row.decision})"
        for row in rows
        for phrase in row.rejected
        if phrase.lower() in text
    ]


def recurring_findings(
    productions_root: Path, threshold: int = RECURRENCE_THRESHOLD
) -> dict[str, list[str]]:
    """Finding keys seen in at least ``threshold`` productions, with examples."""
    seen: dict[str, dict[str, str]] = {}
    if not productions_root.is_dir():
        return {}
    for prod in sorted(p for p in productions_root.iterdir() if (p / "facts").is_dir()):
        for path in sorted((prod / "facts").glob("*.json")):
            try:
                record = FactsRecord.model_validate_json(path.read_bytes())
            except (OSError, ValidationError):
                continue
            for f in record.findings:
                seen.setdefault(f.key, {}).setdefault(
                    prod.name, f"{prod.name}/{record.node_id}: {f.example}"
                )
    return {
        key: list(by_prod.values())[:MAX_EXAMPLES]
        for key, by_prod in sorted(seen.items())
        if len(by_prod) >= threshold
    }


def propose_failures(
    productions_root: Path, failures_dir: Path, threshold: int = RECURRENCE_THRESHOLD
) -> list[Path]:
    """Write a failure-corpus proposal for each recurring finding; never overwrite a file."""
    written: list[Path] = []
    for key, examples in recurring_findings(productions_root, threshold).items():
        path = failures_dir / f"{key}.md"
        if path.exists():
            continue
        body = "\n".join(f"- {e}" for e in examples)
        failures_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"# Failure: {key}\n\n"
            f"**Seen in:** {threshold}+ productions (proposed from recurring Critic findings)\n\n"
            f"## What went wrong\n\nThe Critic raised `{key}` in {threshold}+ productions.\n\n"
            f"## Examples\n\n{body}\n\n"
            "## How to detect it\n\nTODO: curator to describe the signal.\n\n"
            "## The fix\n\nTODO: curator to describe the fix.\n",
            encoding="utf-8",
        )
        written.append(path)
    return written
