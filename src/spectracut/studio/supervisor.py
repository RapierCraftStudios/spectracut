# SPDX-License-Identifier: AGPL-3.0-or-later
"""The run supervisor: typed failures, backoff, an error-burst breaker and tier escalation.

- :func:`classify` sorts a node failure into :attr:`FailureClass.TRANSIENT`
  (network, provider busy, worker offline) or :attr:`FailureClass.PERMANENT`
  (schema, recipe, policy and anything unknown).
- :meth:`Supervisor.wrap` retries a transient failure with capped exponential
  backoff; transient failures never count toward the breaker.
- The breaker trips after ``threshold`` permanent failures within ``window_s``
  seconds: :meth:`Supervisor.hold` then refuses new node admissions (running
  nodes finish) and :meth:`Supervisor.hold_record` gives a resumable
  :class:`SupervisorHold`; :meth:`Supervisor.resume` clears it.
- :meth:`Supervisor.escalate` names the next tier for a node that exhausted its
  revision budget, once per node, before it is rejected.
- Every decision is logged per node (:attr:`Supervisor.decisions`) for the run report.

The supervisor runs in the control plane: it never touches Blender or the network.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import ValidationError

from spectracut.providers.base import ProviderError, ProviderExhausted
from spectracut.spec._base import StrictModel
from spectracut.studio.graph import RunContext, Runner, RunResult
from spectracut.studio.node import NodeSpec, Tier

_TIER_ORDER = (Tier.PREVIEW, Tier.REVIEW, Tier.FINAL, Tier.HERO)


class FailureClass(StrEnum):
    TRANSIENT = "transient"
    PERMANENT = "permanent"


class WorkerOffline(RuntimeError):
    """A render host dropped off the farm; the work can be retried."""


def classify(exc: BaseException) -> FailureClass:
    """Transient for capacity, network and offline workers; permanent for everything else."""
    if isinstance(exc, ValidationError):
        return FailureClass.PERMANENT
    if isinstance(exc, ProviderExhausted | WorkerOffline | ConnectionError | TimeoutError):
        return FailureClass.TRANSIENT
    if isinstance(exc, ProviderError):
        busy = any(w in str(exc).lower() for w in ("busy", "overloaded", "unavailable", "timeout"))
        return FailureClass.TRANSIENT if busy else FailureClass.PERMANENT
    return FailureClass.PERMANENT


class SupervisorHold(StrictModel):
    """A resumable record of a tripped breaker."""

    tripped_at: datetime
    failures: int
    window_s: float
    nodes: list[str]
    """Nodes whose permanent failures tripped the breaker."""
    reason: str


class Supervisor:
    """Failure policy between a graph runner and the production graph."""

    def __init__(
        self,
        *,
        threshold: int = 3,
        window_s: float = 600.0,
        max_retries: int = 3,
        backoff_s: float = 2.0,
        backoff_cap_s: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if threshold < 1:
            raise ValueError(f"threshold must be at least 1, got {threshold}")
        self.threshold = threshold
        self.window_s = window_s
        self.max_retries = max_retries
        self.backoff_s = backoff_s
        self.backoff_cap_s = backoff_cap_s
        self._sleep = sleep
        self._clock = clock
        self._now = now
        self._lock = threading.Lock()
        self._failures: list[tuple[float, str]] = []
        self._hold: SupervisorHold | None = None
        self._escalated: set[str] = set()
        self.decisions: dict[str, list[str]] = {}

    def _log(self, node_id: str, text: str) -> None:
        with self._lock:
            self.decisions.setdefault(node_id, []).append(text)

    def delay(self, retry: int) -> float:
        """Backoff before retry number ``retry`` (1-based)."""
        return float(min(self.backoff_cap_s, self.backoff_s * 2 ** (retry - 1)))

    def _permanent(self, node_id: str) -> None:
        with self._lock:
            t = self._clock()
            self._failures = [f for f in self._failures if t - f[0] <= self.window_s]
            self._failures.append((t, node_id))
            if self._hold is None and len(self._failures) >= self.threshold:
                self._hold = SupervisorHold(
                    tripped_at=self._now(),
                    failures=len(self._failures),
                    window_s=self.window_s,
                    nodes=[n for _, n in self._failures],
                    reason=(
                        f"breaker: {len(self._failures)} permanent failures "
                        f"within {self.window_s:g}s"
                    ),
                )

    def wrap(self, runner: Runner) -> Runner:
        """Return ``runner`` with transient retry and permanent-failure accounting."""

        def run(ctx: RunContext) -> RunResult:
            nid = ctx.spec.id
            retries = 0
            while True:
                try:
                    return runner(ctx)
                except Exception as exc:
                    cls = classify(exc)
                    if cls is FailureClass.TRANSIENT and retries < self.max_retries:
                        retries += 1
                        wait = self.delay(retries)
                        self._log(
                            nid,
                            f"transient {type(exc).__name__}: retry {retries}/"
                            f"{self.max_retries} after {wait:g}s",
                        )
                        self._sleep(wait)
                        continue
                    if cls is FailureClass.TRANSIENT:
                        self._log(nid, f"transient {type(exc).__name__}: retries exhausted")
                    else:
                        self._log(nid, f"permanent {type(exc).__name__}: counted toward breaker")
                        self._permanent(nid)
                    raise

        return run

    def hold(self, spec: NodeSpec) -> str | None:
        """Admission hook: a reason while the breaker is tripped, else ``None``."""
        with self._lock:
            held = self._hold
        if held is None:
            return None
        self._log(spec.id, f"held: {held.reason}")
        return held.reason

    def hold_record(self) -> SupervisorHold | None:
        with self._lock:
            return self._hold

    def resume(self) -> None:
        """Clear a tripped breaker; held nodes are still pending and build on the next run."""
        with self._lock:
            self._hold = None
            self._failures = []

    def escalate(self, spec: NodeSpec, tier: Tier) -> Tier | None:
        """The next tier for a node that exhausted its revisions, once per node."""
        with self._lock:
            if spec.id in self._escalated or tier is _TIER_ORDER[-1]:
                return None
            self._escalated.add(spec.id)
        nxt = _TIER_ORDER[_TIER_ORDER.index(tier) + 1]
        self._log(spec.id, f"escalated {tier.value} -> {nxt.value} after revision budget")
        return nxt
