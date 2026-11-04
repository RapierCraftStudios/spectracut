# SPDX-License-Identifier: AGPL-3.0-or-later
"""Production graph, departments (subagents), Producer, gates, quality profiles and step ledger."""

from __future__ import annotations

from importlib import import_module
from typing import Any

from spectracut.studio.gates import Critic, CriticVerdict, GateDecision, GateMode, GatePolicy
from spectracut.studio.graph import ProductionGraph, RunContext, Runner, RunReport, RunResult
from spectracut.studio.ledger import Ledger, LedgerEntry, LedgerTotals, Usage
from spectracut.studio.node import NodeSpec, NodeState, NodeStatus, Tier, node_key
from spectracut.studio.profiles import QualityProfile, available_profiles, load_profile

_LAZY: dict[str, str] = {
    "CapacityLimits": "runner",
    "DepartmentRunner": "runner",
    "TaskContext": "runner",
    "CostEstimate": "department",
    "Department": "department",
    "DepartmentOutputError": "department",
    "SkillPack": "department",
}


def __getattr__(name: str) -> Any:
    """Resolve the department names from their defining modules (one definition site)."""
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f"{__name__}.{module}"), name)
    globals()[name] = value
    return value


__all__ = [
    "CapacityLimits",
    "CostEstimate",
    "Critic",
    "CriticVerdict",
    "Department",
    "DepartmentOutputError",
    "DepartmentRunner",
    "GateDecision",
    "GateMode",
    "GatePolicy",
    "Ledger",
    "LedgerEntry",
    "LedgerTotals",
    "NodeSpec",
    "NodeState",
    "NodeStatus",
    "ProductionGraph",
    "QualityProfile",
    "RunContext",
    "RunReport",
    "RunResult",
    "Runner",
    "SkillPack",
    "TaskContext",
    "Tier",
    "Usage",
    "available_profiles",
    "load_profile",
    "node_key",
]
