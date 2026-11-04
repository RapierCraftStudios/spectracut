# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pluggable executors: host pool (default), Flamenco for Blender jobs."""

from __future__ import annotations

from spectracut.queue.executors.base import (
    Executor,
    ExecutorBusy,
    GpuCard,
    JobResult,
    JobStatus,
    Lease,
    LeasePool,
    NoCapableExecutor,
    Resources,
)
from spectracut.queue.executors.flamenco import FlamencoExecutor, FlamencoJobFailed
from spectracut.queue.executors.hostpool import HostPoolExecutor, HostSlot

__all__ = [
    "Executor",
    "ExecutorBusy",
    "FlamencoExecutor",
    "FlamencoJobFailed",
    "GpuCard",
    "HostPoolExecutor",
    "HostSlot",
    "JobResult",
    "JobStatus",
    "Lease",
    "LeasePool",
    "NoCapableExecutor",
    "Resources",
]
