# SPDX-License-Identifier: AGPL-3.0-or-later
"""Production graph, departments (subagents), Producer, gates, quality profiles and step ledger.

The public names are resolved lazily from their defining modules, so each name
has exactly one definition site and importing the package stays cheap.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS: dict[str, str] = {
    "Critic": "gates",
    "CriticVerdict": "gates",
    "GateDecision": "gates",
    "GateMode": "gates",
    "GatePolicy": "gates",
    "ProductionGraph": "graph",
    "RunContext": "graph",
    "RunReport": "graph",
    "RunResult": "graph",
    "Runner": "graph",
    "Ledger": "ledger",
    "LedgerEntry": "ledger",
    "LedgerTotals": "ledger",
    "Usage": "ledger",
    "NodeSpec": "node",
    "NodeState": "node",
    "NodeStatus": "node",
    "Tier": "node",
    "node_key": "node",
    "QualityProfile": "profiles",
    "available_profiles": "profiles",
    "load_profile": "profiles",
}


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(f"spectracut.studio.{module}"), name)


def __dir__() -> list[str]:
    return sorted(_EXPORTS)
