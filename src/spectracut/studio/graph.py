# SPDX-License-Identifier: AGPL-3.0-or-later
"""The production graph: a content-addressed build system run by departments.

- Every node has a key (:func:`~spectracut.studio.node.node_key`) over its kind,
  department, params, tier and the output hashes of its inputs.
- Changing a node marks it and exactly its transitive dependants dirty.
- :meth:`ProductionGraph.run` builds pending and dirty nodes in dependency
  order. An output already accepted under the same key is reused without
  calling the department, so re-running a clean graph does no work.
- Each built node passes its gate (:mod:`spectracut.studio.gates`); a node
  awaiting a person, rejected or failed blocks its dependants.
- Each department run appends one :class:`~spectracut.studio.ledger.LedgerEntry`.
- ``run(..., max_workers=N)`` builds independent nodes in parallel on a thread
  pool; resource limits (AI capacity, GPU leases) are enforced by the runner
  (:class:`~spectracut.studio.runner.DepartmentRunner`).

Departments and the Critic are injected callables: the engine itself is pure
Python and never touches Blender, the network or AI services.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import UTC, datetime

from pydantic import AwareDatetime, Field, JsonValue

from spectracut.spec._base import ContentHash, Slug, StrictModel, content_hash
from spectracut.studio.facts import (
    FactsDraft,
    FactsRecord,
    FactsStore,
    Finding,
    PreserveRow,
    RejectedApproach,
    check_preserve,
    touched_ids,
)
from spectracut.studio.gates import Critic, CriticVerdict, GateDecision, GateMode, GatePolicy
from spectracut.studio.ledger import Ledger, LedgerEntry, Outcome, Usage, usage_of
from spectracut.studio.node import NEEDS_BUILD, NodeSpec, NodeState, NodeStatus, Tier, node_key
from spectracut.studio.profiles import QualityProfile

DEFAULT_TIER = Tier.PREVIEW


class RevisionNote(StrictModel):
    """A reviewer's note on a node, optionally pinned to one frame of its media.

    Notes are kept on the graph, handed to the node's department on every
    rebuild (:attr:`RunContext.notes`) and cleared when a person approves it.
    """

    node_id: Slug
    frame: int | None = Field(default=None, ge=0)
    """Zero-based frame the note is pinned to; ``None`` for the whole node."""
    timecode: str | None = None
    """``HH:MM:SS:FF`` of ``frame`` as shown to the reviewer."""
    text: str = Field(min_length=1, max_length=4000)
    author: str = Field(default="reviewer", min_length=1, max_length=100)
    created_at: AwareDatetime


class RunContext(StrictModel):
    """What a department receives to build one node."""

    spec: NodeSpec
    tier: Tier
    key: str
    inputs: dict[str, JsonValue]
    """Accepted outputs of the node's inputs, by input id."""
    attempt: int
    feedback: CriticVerdict | None = None
    """The Critic's verdict on the previous attempt, when revising."""
    notes: tuple[RevisionNote, ...] = ()
    """Reviewer revision notes on this node (frame-pinned comments), oldest first."""
    preserve: tuple[PreserveRow, ...] = ()
    """Anchored decisions of other nodes this node must keep (not part of the node key)."""


class RunResult(StrictModel):
    """What a department returns: the artifact (JSON) and what it cost."""

    output: JsonValue
    usage: Usage = Usage()
    facts: FactsDraft | None = None
    """Decisions and rejected approaches the department reports for the facts record."""


Runner = Callable[[RunContext], RunResult]

Hold = Callable[[NodeSpec], str | None]
"""``hold(spec) -> reason``: a reason keeps a ready node from building this run."""


@dataclass
class RunReport:
    """Node ids by what happened to them in one :meth:`ProductionGraph.run`."""

    executed: list[str] = field(default_factory=list)
    reused: list[str] = field(default_factory=list)
    awaiting_approval: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    held: list[str] = field(default_factory=list)
    """Ready nodes the ``hold`` hook kept from building; they keep their status."""
    held_reasons: dict[str, str] = field(default_factory=dict)

    @property
    def did_work(self) -> bool:
        return bool(self.executed)


@dataclass(frozen=True)
class _Accepted:
    output: JsonValue
    output_hash: str


class AcceptedOutput(StrictModel):
    """An output accepted under a node key, as stored in a :class:`GraphSnapshot`."""

    output: JsonValue
    output_hash: ContentHash


class PendingApproval(StrictModel):
    """An output waiting on a human gate, as stored in a :class:`GraphSnapshot`."""

    key: ContentHash
    output: JsonValue
    output_hash: ContentHash


class GraphSnapshot(StrictModel):
    """Everything needed to rebuild a :class:`ProductionGraph` in another process."""

    production_id: Slug
    profile: QualityProfile | None = None
    gates: GatePolicy = GatePolicy()
    nodes: tuple[NodeSpec, ...] = ()
    """Node declarations in dependency order."""
    states: dict[str, NodeState] = Field(default_factory=dict)
    cache: dict[str, AcceptedOutput] = Field(default_factory=dict)
    """Accepted outputs by node key."""
    pending: dict[str, PendingApproval] = Field(default_factory=dict)
    """Outputs awaiting a person, by node id."""
    decisions: dict[str, GateDecision] = Field(default_factory=dict)
    ledger: tuple[LedgerEntry, ...] = ()
    notes: dict[str, tuple[RevisionNote, ...]] = Field(default_factory=dict)
    """Reviewer revision notes, by node id."""


class ProductionGraph:
    """Nodes, their build state, an output cache by key, gates and a cost ledger."""

    def __init__(
        self,
        production_id: str,
        *,
        profile: QualityProfile | None = None,
        gates: GatePolicy | None = None,
        ledger: Ledger | None = None,
        facts: FactsStore | None = None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.production_id = production_id
        self.profile = profile
        if gates is None:
            gates = profile.gate_policy() if profile is not None else GatePolicy()
        self.gates = gates
        self.ledger = ledger if ledger is not None else Ledger()
        self.facts = facts
        self._revised: dict[str, list[RejectedApproach]] = {}
        self._clock = clock
        self._now = now
        self._specs: dict[str, NodeSpec] = {}
        self._states: dict[str, NodeState] = {}
        self._dependants: dict[str, list[str]] = {}
        self._cache: dict[str, _Accepted] = {}
        self._pending: dict[str, tuple[str, _Accepted]] = {}
        self._decisions: dict[str, GateDecision] = {}
        self._notes: dict[str, tuple[RevisionNote, ...]] = {}
        self._lock = threading.RLock()

    # ---- persistence -------------------------------------------------------

    def snapshot(self) -> GraphSnapshot:
        """A JSON-safe copy of the whole graph: nodes, states, outputs, gates and ledger."""
        with self._lock:
            return GraphSnapshot(
                production_id=self.production_id,
                profile=self.profile,
                gates=self.gates,
                nodes=tuple(self._specs.values()),
                states={nid: self._states[nid] for nid in self._specs},
                cache={
                    key: AcceptedOutput(output=a.output, output_hash=a.output_hash)
                    for key, a in self._cache.items()
                },
                pending={
                    nid: PendingApproval(key=key, output=a.output, output_hash=a.output_hash)
                    for nid, (key, a) in self._pending.items()
                },
                decisions=dict(self._decisions),
                ledger=self.ledger.entries(),
                notes={nid: n for nid, n in self._notes.items() if n},
            )

    @classmethod
    def from_snapshot(
        cls,
        snap: GraphSnapshot,
        *,
        profile: QualityProfile | None = None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> ProductionGraph:
        """Rebuild a graph from :meth:`snapshot`; ``profile`` overrides the stored one."""
        graph = cls(
            snap.production_id,
            profile=profile if profile is not None else snap.profile,
            gates=snap.gates,
            ledger=Ledger(snap.ledger),
            clock=clock,
            now=now,
        )
        graph.add_nodes(snap.nodes)
        ids = set(graph._specs)
        for what, table in (
            ("states", snap.states),
            ("pending", snap.pending),
            ("decisions", snap.decisions),
            ("notes", snap.notes),
        ):
            unknown = sorted(set(table) - ids)
            if unknown:
                raise ValueError(f"snapshot {what} name unknown nodes {unknown}")
        graph._states.update(snap.states)
        graph._cache = {key: _Accepted(a.output, a.output_hash) for key, a in snap.cache.items()}
        graph._pending = {
            nid: (p.key, _Accepted(p.output, p.output_hash)) for nid, p in snap.pending.items()
        }
        graph._decisions = dict(snap.decisions)
        graph._notes = {nid: tuple(n) for nid, n in snap.notes.items() if n}
        return graph

    # ---- structure ---------------------------------------------------------

    def add_node(self, spec: NodeSpec) -> None:
        self.add_nodes([spec])

    def add_nodes(self, specs: Iterable[NodeSpec]) -> None:
        """Add nodes in any order; rejects duplicate ids, unknown inputs and cycles."""
        batch = list(specs)
        merged = dict(self._specs)
        for spec in batch:
            if spec.id in merged:
                raise ValueError(f"duplicate node id {spec.id!r}")
            merged[spec.id] = spec
        self._install(merged)
        for spec in batch:
            self._states[spec.id] = NodeState()

    def update_node(self, spec: NodeSpec) -> frozenset[str]:
        """Replace a node's declaration; returns the ids now dirty (empty if unchanged)."""
        old = self.spec(spec.id)
        if old == spec:
            return frozenset()
        self._install({**self._specs, spec.id: spec})
        return self.invalidate(spec.id)

    def set_params(self, node_id: str, params: Mapping[str, JsonValue]) -> frozenset[str]:
        """Change one node's params; marks it and its transitive dependants dirty."""
        old = self.spec(node_id)
        return self.update_node(old.model_copy(update={"params": dict(params)}))

    def invalidate(self, node_id: str) -> frozenset[str]:
        """Mark ``node_id`` and exactly its transitive dependants dirty; return them."""
        affected = frozenset({node_id}) | self.dependants(node_id)
        for nid in affected:
            state = self._states[nid]
            if state.status is not NodeStatus.PENDING:
                self._states[nid] = state.model_copy(
                    update={"status": NodeStatus.DIRTY, "output_hash": None, "error": None}
                )
            self._pending.pop(nid, None)
        return affected

    def remove_node(self, node_id: str) -> None:
        """Remove a node no other node reads; its state, pending output and decision go too."""
        self.spec(node_id)
        readers = sorted(self._dependants[node_id])
        if readers:
            raise ValueError(f"node {node_id!r} is an input of {readers}; rewire them first")
        self._install({nid: s for nid, s in self._specs.items() if nid != node_id})
        self._states.pop(node_id, None)
        self._pending.pop(node_id, None)
        self._decisions.pop(node_id, None)
        self._notes.pop(node_id, None)

    def _install(self, specs: dict[str, NodeSpec]) -> None:
        dependants: dict[str, list[str]] = {nid: [] for nid in specs}
        for spec in specs.values():
            for i, ident in enumerate(spec.inputs):
                if ident not in specs:
                    raise ValueError(f"node {spec.id!r} inputs[{i}]: unknown node {ident!r}")
                dependants[ident].append(spec.id)
        order = _toposort(specs, dependants)
        self._specs = {nid: specs[nid] for nid in order}
        self._dependants = dependants

    # ---- queries -----------------------------------------------------------

    def spec(self, node_id: str) -> NodeSpec:
        try:
            return self._specs[node_id]
        except KeyError:
            raise KeyError(f"unknown node {node_id!r}") from None

    def state(self, node_id: str) -> NodeState:
        self.spec(node_id)
        return self._states[node_id]

    def status(self, node_id: str) -> NodeStatus:
        return self.state(node_id).status

    def statuses(self) -> dict[str, NodeStatus]:
        return {nid: self._states[nid].status for nid in self._specs}

    def dirty(self) -> frozenset[str]:
        return frozenset(n for n, s in self._states.items() if s.status is NodeStatus.DIRTY)

    def order(self) -> tuple[str, ...]:
        """Node ids in dependency order (inputs first)."""
        return tuple(self._specs)

    def dependants(self, node_id: str) -> frozenset[str]:
        """Every node that reads ``node_id`` directly or transitively."""
        self.spec(node_id)
        seen: set[str] = set()
        stack = list(self._dependants[node_id])
        while stack:
            nid = stack.pop()
            if nid not in seen:
                seen.add(nid)
                stack.extend(self._dependants[nid])
        return frozenset(seen)

    def output(self, node_id: str) -> JsonValue:
        """The accepted output of a clean node."""
        state = self.state(node_id)
        if state.status is not NodeStatus.CLEAN or state.key is None:
            raise ValueError(f"node {node_id!r} has no accepted output ({state.status})")
        return self._cache[state.key].output

    def decision(self, node_id: str) -> GateDecision | None:
        self.spec(node_id)
        return self._decisions.get(node_id)

    def tier_for(self, spec: NodeSpec) -> Tier:
        if spec.tier is not None:
            return spec.tier
        if self.profile is not None and spec.kind in self.profile.tier:
            return self.profile.tier[spec.kind]
        return DEFAULT_TIER

    def gate_for(self, spec: NodeSpec) -> GateMode:
        return self.gates.resolve(spec.id, spec.kind)

    def revision_budget(self, spec: NodeSpec) -> int:
        if self.profile is None:
            return 0
        return self.profile.revision_budget.get(spec.kind, 0)

    # ---- revision notes ----------------------------------------------------

    def add_note(self, node_id: str, note: RevisionNote) -> None:
        """Pin a reviewer's note to ``node_id``; its department sees it on rebuild.

        Raises :class:`KeyError` for an unknown node and :class:`ValueError` when
        ``note.node_id`` names a different node.
        """
        self.spec(node_id)
        if note.node_id != node_id:
            raise ValueError(f"note is for {note.node_id!r}, not {node_id!r}")
        with self._lock:
            self._notes[node_id] = (*self._notes.get(node_id, ()), note)

    def notes(self, node_id: str) -> tuple[RevisionNote, ...]:
        """Reviewer notes on ``node_id``, oldest first."""
        self.spec(node_id)
        return self._notes.get(node_id, ())

    # ---- human gate --------------------------------------------------------

    def approve(self, node_id: str, comment: str = "") -> None:
        """Approve a node awaiting a person; its dependants run on the next run.

        The node's revision notes are cleared: the approved output answers them.
        """
        key, accepted = self._awaiting(node_id)
        self._cache[key] = accepted
        self._notes.pop(node_id, None)
        self._decide(node_id, human="approved", comment=comment)
        self._states[node_id] = self._states[node_id].model_copy(
            update={"status": NodeStatus.CLEAN, "output_hash": accepted.output_hash}
        )

    def reject(self, node_id: str, comment: str = "") -> None:
        """Reject a node awaiting a person; call :meth:`invalidate` or change it to rebuild."""
        self._awaiting(node_id)
        self._decide(node_id, human="rejected", comment=comment)
        self._states[node_id] = self._states[node_id].model_copy(
            update={"status": NodeStatus.REJECTED}
        )

    def _awaiting(self, node_id: str) -> tuple[str, _Accepted]:
        if self.status(node_id) is not NodeStatus.AWAITING_APPROVAL:
            raise ValueError(f"node {node_id!r} is not awaiting approval")
        return self._pending.pop(node_id)

    def _decide(self, node_id: str, *, human: str, comment: str) -> None:
        prior = self._decisions.get(node_id) or GateDecision(mode=GateMode.HUMAN)
        self._decisions[node_id] = prior.model_copy(update={"human": human, "comment": comment})

    # ---- running -----------------------------------------------------------

    def run(
        self,
        runner: Runner,
        *,
        critic: Critic | None = None,
        max_workers: int = 1,
        hold: Hold | None = None,
    ) -> RunReport:
        """Build every pending or dirty node whose inputs are clean, in dependency order.

        With ``max_workers`` above 1, nodes whose inputs are clean are built at the
        same time on a thread pool, and dependants start as their inputs finish.
        Report lists are in dependency order either way.

        ``hold`` is asked about each ready node that is not reused from the cache;
        a returned reason keeps that node unbuilt (listed in ``report.held``) and
        its dependants blocked.
        """
        if max_workers < 1:
            raise ValueError(f"max_workers must be at least 1, got {max_workers}")
        if max_workers > 1:
            return self._run_parallel(runner, critic, max_workers, hold)
        report = RunReport()
        for nid, spec in self._specs.items():
            if self._states[nid].status not in NEEDS_BUILD:
                continue
            if any(self._states[i].status is not NodeStatus.CLEAN for i in spec.inputs):
                report.blocked.append(nid)
                continue
            tier = self.tier_for(spec)
            input_hashes = {i: self._require_hash(i) for i in spec.inputs}
            key = node_key(spec, input_hashes, tier)
            hit = self._cache.get(key)
            if hit is not None:
                self._states[nid] = NodeState(
                    status=NodeStatus.CLEAN, tier=tier, key=key, output_hash=hit.output_hash
                )
                report.reused.append(nid)
                continue
            reason = hold(spec) if hold is not None else None
            if reason is not None:
                report.held.append(nid)
                report.held_reasons[nid] = reason
                continue
            report.executed.append(nid)
            status = self._build(spec, tier, key, runner, critic)
            if status is NodeStatus.AWAITING_APPROVAL:
                report.awaiting_approval.append(nid)
            elif status is NodeStatus.REJECTED:
                report.rejected.append(nid)
            elif status is NodeStatus.FAILED:
                report.failed.append(nid)
        return report

    def _run_parallel(
        self, runner: Runner, critic: Critic | None, workers: int, hold: Hold | None = None
    ) -> RunReport:
        report = RunReport()
        waiting = [nid for nid in self._specs if self._states[nid].status in NEEDS_BUILD]
        running: dict[Future[NodeStatus], str] = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            while True:
                with self._lock:
                    self._dispatch(waiting, running, report, pool, runner, critic, hold)
                if not running:
                    break
                done, _ = wait(running, return_when=FIRST_COMPLETED)
                for future in done:
                    nid = running.pop(future)
                    status = future.result()
                    if status is NodeStatus.AWAITING_APPROVAL:
                        report.awaiting_approval.append(nid)
                    elif status is NodeStatus.REJECTED:
                        report.rejected.append(nid)
                    elif status is NodeStatus.FAILED:
                        report.failed.append(nid)
        rank = {nid: i for i, nid in enumerate(self._specs)}
        for ids in (
            report.executed,
            report.reused,
            report.awaiting_approval,
            report.rejected,
            report.failed,
            report.blocked,
            report.held,
        ):
            ids.sort(key=rank.__getitem__)
        return report

    def _dispatch(
        self,
        waiting: list[str],
        running: dict[Future[NodeStatus], str],
        report: RunReport,
        pool: ThreadPoolExecutor,
        runner: Runner,
        critic: Critic | None,
        hold: Hold | None = None,
    ) -> None:
        """Start, reuse, hold or block every waiting node whose inputs are settled."""
        in_flight = set(running.values())
        progressed = True
        while progressed:
            progressed = False
            for nid in list(waiting):
                spec = self._specs[nid]
                if any(i in in_flight or i in waiting for i in spec.inputs):
                    continue
                waiting.remove(nid)
                progressed = True
                if any(self._states[i].status is not NodeStatus.CLEAN for i in spec.inputs):
                    report.blocked.append(nid)
                    continue
                tier = self.tier_for(spec)
                input_hashes = {i: self._require_hash(i) for i in spec.inputs}
                key = node_key(spec, input_hashes, tier)
                hit = self._cache.get(key)
                if hit is not None:
                    self._states[nid] = NodeState(
                        status=NodeStatus.CLEAN, tier=tier, key=key, output_hash=hit.output_hash
                    )
                    report.reused.append(nid)
                    continue
                reason = hold(spec) if hold is not None else None
                if reason is not None:
                    report.held.append(nid)
                    report.held_reasons[nid] = reason
                    continue
                report.executed.append(nid)
                self._states[nid] = NodeState(status=NodeStatus.RUNNING, tier=tier, key=key)
                running[pool.submit(self._build, spec, tier, key, runner, critic)] = nid
                in_flight.add(nid)

    def _require_hash(self, node_id: str) -> str:
        output_hash = self._states[node_id].output_hash
        assert output_hash is not None, node_id
        return output_hash

    def _build(
        self, spec: NodeSpec, tier: Tier, key: str, runner: Runner, critic: Critic | None
    ) -> NodeStatus:
        mode = self.gate_for(spec)
        reviewed = critic if mode is not GateMode.AUTO else None
        max_attempts = 1 + (self.revision_budget(spec) if reviewed is not None else 0)
        with self._lock:
            inputs = {i: self._cache[self._states[i].key or ""].output for i in spec.inputs}
            notes = self._notes.get(spec.id, ())
        preserve = (
            self.facts.preserve_for(spec.id, touched_ids(spec.id, spec.inputs, spec.params))
            if self.facts is not None
            else ()
        )
        feedback: CriticVerdict | None = None
        for attempt in range(1, max_attempts + 1):
            with self._lock:
                self._states[spec.id] = NodeState(
                    status=NodeStatus.RUNNING, tier=tier, key=key, attempts=attempt
                )
            ctx = RunContext(
                spec=spec,
                tier=tier,
                key=key,
                inputs=inputs,
                attempt=attempt,
                feedback=feedback,
                notes=notes,
                preserve=preserve,
            )
            started_at, start = self._now(), self._clock()
            try:
                result = runner(ctx)
            except Exception as exc:  # noqa: BLE001 - a department failure is recorded, not raised
                with self._lock:
                    self._record(
                        spec, tier, key, attempt, started_at, start, usage_of(exc), "failed"
                    )
                    self._set(spec.id, NodeStatus.FAILED, error=f"{type(exc).__name__}: {exc}")
                return NodeStatus.FAILED
            accepted = _Accepted(result.output, content_hash(result.output))
            verdict = reviewed(spec, result.output) if reviewed is not None else None
            if verdict is not None and preserve:
                verdict = _with_conformance(verdict, preserve, result.output)
            with self._lock:
                status = self._settle(
                    spec,
                    tier,
                    key,
                    attempt,
                    max_attempts,
                    mode,
                    started_at,
                    start,
                    result,
                    accepted,
                    verdict,
                )
            if status is None:
                feedback = verdict
                continue
            return status
        raise AssertionError("unreachable")  # pragma: no cover

    def _settle(
        self,
        spec: NodeSpec,
        tier: Tier,
        key: str,
        attempt: int,
        max_attempts: int,
        mode: GateMode,
        started_at: datetime,
        start: float,
        result: RunResult,
        accepted: _Accepted,
        verdict: CriticVerdict | None,
    ) -> NodeStatus | None:
        """Record one attempt's gate outcome; ``None`` means revise and try again."""
        usage = _with_critic_usage(result.usage, verdict)
        if verdict is not None and not verdict.passed:
            last = attempt == max_attempts
            self._revised.setdefault(spec.id, []).append(
                RejectedApproach(
                    approach=f"attempt {attempt} output {accepted.output_hash[:12]}",
                    why=verdict.comment[:300] or "failed the Critic",
                )
            )
            outcome: Outcome = "rejected" if last else "revise"
            self._record(spec, tier, key, attempt, started_at, start, usage, outcome)
            if not last:
                return None
            self._decisions[spec.id] = GateDecision(mode=mode, critic=verdict)
            self._write_facts(spec, result, verdict)
            self._set(spec.id, NodeStatus.REJECTED)
            return NodeStatus.REJECTED
        self._decisions[spec.id] = GateDecision(mode=mode, critic=verdict)
        self._write_facts(spec, result, verdict)
        if mode is GateMode.HUMAN:
            self._record(spec, tier, key, attempt, started_at, start, usage, "awaiting_approval")
            self._pending[spec.id] = (key, accepted)
            self._set(spec.id, NodeStatus.AWAITING_APPROVAL)
            return NodeStatus.AWAITING_APPROVAL
        self._record(spec, tier, key, attempt, started_at, start, usage, "accepted")
        self._cache[key] = accepted
        self._set(spec.id, NodeStatus.CLEAN, output_hash=accepted.output_hash)
        return NodeStatus.CLEAN

    def _write_facts(
        self, spec: NodeSpec, result: RunResult, verdict: CriticVerdict | None
    ) -> None:
        """Write the node's bounded facts record once its gate outcome is known."""
        revised = self._revised.pop(spec.id, [])
        if self.facts is None:
            return
        draft = result.facts or FactsDraft()
        findings = verdict.findings if verdict is not None else ()
        example = verdict.comment if verdict is not None else ""
        self.facts.write(
            FactsRecord(
                production_id=self.production_id,
                node_id=spec.id,
                decisions=draft.decisions,
                rejected=(*draft.rejected, *revised),
                findings=tuple(Finding(key=k, example=example) for k in findings),
            )
        )

    def _set(
        self,
        node_id: str,
        status: NodeStatus,
        *,
        output_hash: str | None = None,
        error: str | None = None,
    ) -> None:
        self._states[node_id] = self._states[node_id].model_copy(
            update={"status": status, "output_hash": output_hash, "error": error}
        )

    def _record(
        self,
        spec: NodeSpec,
        tier: Tier,
        key: str,
        attempt: int,
        started_at: datetime,
        start: float,
        usage: Usage,
        outcome: Outcome,
    ) -> None:
        self.ledger.append(
            LedgerEntry(
                production_id=self.production_id,
                node_id=spec.id,
                kind=spec.kind,
                department=spec.department,
                node_key=key,
                tier=tier,
                attempt=attempt,
                started_at=started_at,
                duration_s=max(0.0, self._clock() - start),
                gpu_minutes=usage.gpu_minutes,
                ai_usage=dict(usage.ai_usage),
                elevenlabs_characters=usage.elevenlabs_characters,
                outcome=outcome,
            )
        )


def _with_conformance(
    verdict: CriticVerdict, preserve: tuple[PreserveRow, ...], output: JsonValue
) -> CriticVerdict:
    """Fail ``verdict`` when ``output`` breaks an anchored decision it must preserve."""
    violations = check_preserve(preserve, output)
    if not violations:
        return verdict
    note = "; ".join(violations)
    return verdict.model_copy(
        update={
            "passed": False,
            "comment": f"{verdict.comment} [preserve] {note}".strip(),
            "findings": (*verdict.findings, "preserve-violation"),
        }
    )


def _with_critic_usage(usage: Usage, verdict: CriticVerdict | None) -> Usage:
    """``usage`` plus the Critic's judge tokens, so the ledger sees the whole attempt."""
    if verdict is None or not verdict.usage:
        return usage
    ai = dict(usage.ai_usage)
    for metric, amount in verdict.usage.items():
        ai[metric] = ai.get(metric, 0) + amount
    return usage.model_copy(update={"ai_usage": ai})


def _toposort(specs: Mapping[str, NodeSpec], dependants: Mapping[str, list[str]]) -> list[str]:
    """Kahn's algorithm, stable in declaration order; raises on a cycle."""
    indegree = {nid: len(spec.inputs) for nid, spec in specs.items()}
    ready = [nid for nid in specs if indegree[nid] == 0]
    order: list[str] = []
    while ready:
        nid = ready.pop(0)
        order.append(nid)
        for dep in dependants[nid]:
            indegree[dep] -= 1
            if indegree[dep] == 0:
                ready.append(dep)
    if len(order) != len(specs):
        cyclic = sorted(nid for nid in specs if indegree[nid] > 0)
        raise ValueError(f"cycle among nodes {cyclic}")
    return order
