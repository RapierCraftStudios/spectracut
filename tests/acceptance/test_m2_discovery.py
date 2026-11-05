# SPDX-License-Identifier: AGPL-3.0-or-later
"""M2 (#24): Discovery writes an AlterLab brief; three concepts in parallel; a person picks one.

Real end to end: every department runs through :class:`ClaudeCodeTextProvider`
(headless ``claude -p`` with the subscription sign-in), on the real graph and
department runner with the copy gate. The brief must cite a source for every
proof point, three concept nodes must be written at the same time and wait at
their human gate, :func:`pick_concept` passes it, and the script for the chosen
concept must pass the copy gate.

Runs only with ``SPECTRACUT_E2E=1`` (post-deploy check
``m2-discovery-concepts-claude-e2e``); a missing or signed-out ``claude`` is a
failure then, not a skip. The mock-provider version is
``tests/studio/test_discovery.py``.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from spectracut.providers.base import Completion
from spectracut.providers.claude_code import ClaudeCodeTextProvider, claude_status
from spectracut.spec.brief import CreativeBrief
from spectracut.studio import (
    CapacityLimits,
    DepartmentRunner,
    GateMode,
    GatePolicy,
    NodeStatus,
    ProductionGraph,
)
from spectracut.studio.departments.discovery import DISCOVERY_DEPARTMENTS, discovery_nodes
from spectracut.studio.departments.writer import (
    DEVELOPMENT_DEPARTMENTS,
    Concept,
    Script,
    development_nodes,
    pick_concept,
)
from spectracut.writer.copy_gate import guarded

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        os.environ.get("SPECTRACUT_E2E") != "1", reason="set SPECTRACUT_E2E=1 to run"
    ),
]

FIXTURE = json.loads(
    (
        Path(__file__).resolve().parents[1] / "studio" / "fixtures" / "alterlab" / "discovery.json"
    ).read_text()
)
CONCEPTS = ["concept-1", "concept-2", "concept-3"]


class Overlap:
    """Wraps a provider and records the most calls in flight at once."""

    def __init__(self, inner: ClaudeCodeTextProvider) -> None:
        self.inner = inner
        self.name = inner.name
        self._lock = threading.Lock()
        self._live = 0
        self.peak = 0

    def complete(self, system: str, prompt: str, *, schema: dict[str, Any]) -> Completion:
        with self._lock:
            self._live += 1
            self.peak = max(self.peak, self._live)
        try:
            return self.inner.complete(system, prompt, schema=schema)
        finally:
            with self._lock:
                self._live -= 1


@pytest.fixture(autouse=True)
def only_subscription_sign_in(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "SPECTRACUT_PROVIDERS"):
        monkeypatch.delenv(name, raising=False)


def test_alterlab_brief_three_parallel_concepts_human_gate_picks_one() -> None:
    state = claude_status()
    assert state.installed and state.signed_in, f"claude not signed in: {state}"

    graph = ProductionGraph(
        "alterlab-discovery-e2e", gates=GatePolicy(defaults={"concept": GateMode.HUMAN})
    )
    graph.add_nodes(discovery_nodes(FIXTURE["answers"], FIXTURE["pages"]))
    graph.add_nodes(development_nodes(n=3))
    provider = Overlap(ClaudeCodeTextProvider())
    inner = DepartmentRunner(
        (*DISCOVERY_DEPARTMENTS, *DEVELOPMENT_DEPARTMENTS),
        provider,
        limits=CapacityLimits(ai=3),
        graph=graph,
    )
    runner = guarded(inner, graph=graph, forbidden_words=FIXTURE["forbidden_words"])

    started = time.monotonic()
    report = graph.run(runner, max_workers=3)
    errors = {n: graph.state(n).error for n in report.failed}
    assert report.failed == [], errors

    brief = CreativeBrief.model_validate(graph.output("brief"))
    declared = {s.id for s in brief.sources}
    assert brief.proof_points and all(p.source_id in declared for p in brief.proof_points)

    assert sorted(report.awaiting_approval) == CONCEPTS
    assert provider.peak >= 2, "concepts were not written in parallel"
    pending = graph.snapshot().pending
    concepts = [Concept.model_validate(pending[c].output) for c in CONCEPTS]
    assert len({c.logline for c in concepts}) == 3

    pick_concept(graph, "concept-1", comment="e2e pick")
    assert graph.status("concept-2") is NodeStatus.REJECTED
    report = graph.run(runner, max_workers=3)
    errors = {n: graph.state(n).error for n in report.failed}
    assert report.executed == ["script"] and report.failed == [], errors
    script = Script.model_validate(graph.output("script"))
    assert script.lines
    assert time.monotonic() - started < 1500
