# SPDX-License-Identifier: AGPL-3.0-or-later
"""Production graph, departments (subagents), Producer, gates, quality profiles and step ledger."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from spectracut.studio.department import (
    CostEstimate,
    Department,
    DepartmentOutputError,
    SkillPack,
)
from spectracut.studio.gates import (
    Critic,
    CriticVerdict,
    GateDecision,
    GateMode,
    GatePolicy,
)
from spectracut.studio.graph import (
    ProductionGraph,
    RevisionNote,
    RunContext,
    Runner,
    RunReport,
    RunResult,
)
from spectracut.studio.ledger import Ledger, LedgerEntry, LedgerTotals, Usage
from spectracut.studio.node import NodeSpec, NodeState, NodeStatus, Tier, node_key
from spectracut.studio.profiles import QualityProfile, available_profiles, load_profile
from spectracut.studio.runner import CapacityLimits, DepartmentRunner, TaskContext

if TYPE_CHECKING:
    from spectracut.studio.producer import BudgetCaps, Producer, ProducerReport, ProductionPlan

# The Producer pulls in every department, and the capture/screens departments import
# spectracut.queue, whose executors import spectracut.studio.ledger. Importing the
# producer eagerly here would close that cycle whenever spectracut.queue loads first,
# so its names load on first access instead (PEP 562).
_PRODUCER_NAMES = frozenset({"BudgetCaps", "Producer", "ProducerReport", "ProductionPlan"})


def __getattr__(name: str) -> Any:
    if name in _PRODUCER_NAMES:
        from spectracut.studio import producer

        value = getattr(producer, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "BudgetCaps",
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
    "Producer",
    "ProducerReport",
    "ProductionGraph",
    "ProductionPlan",
    "QualityProfile",
    "RevisionNote",
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
