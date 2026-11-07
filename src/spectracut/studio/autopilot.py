# SPDX-License-Identifier: AGPL-3.0-or-later
"""Autopilot: drive a stored production to delivery, or to one named stop.

:func:`run_to_delivery` runs :meth:`~spectracut.studio.producer.Producer.run`
pass after pass, saving the graph after every settled node, until a pass builds
nothing. It then names exactly one :class:`AutopilotStop`:

- ``delivered``: every node is clean;
- ``missing_prerequisite``: a node failed for a missing credential or external
  prerequisite (a ``ProviderAuthError`` or ``LookupError``), named with the mock
  that stands in for it;
- ``spend_above_ceiling``: a budget hold the policy's ceiling does not cover;
- ``owner_gate``: a gate the owner kept is waiting for them;
- ``contract_failed``: a delegated gate escalated, or edit/delivery was rejected
  by the conformance review after the supervisor's tier escalation;
- ``breaker``: the supervisor's breaker tripped;
- ``failed``: anything else.

Every run writes ``runs/<n>/report.json`` and ``report.md`` in the production
dir (:class:`AutopilotReport`). Only names of missing prerequisites go in the report,
never credential values.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import JsonValue, ValidationError

from spectracut.providers.base import ProviderAuthError, ProviderError, TextProvider, redact
from spectracut.studio.autonomy import AutonomyPolicy, BudgetCeiling
from spectracut.studio.conformance import (
    CONFORMANCE_KINDS,
    Conformance,
    ConformanceReview,
    blockers,
    conformance_verdict,
)
from spectracut.studio.departments.finishing import DeliveryReport
from spectracut.studio.gates import Critic, GateMode, Severity
from spectracut.studio.graph import ProductionGraph, RunContext, Runner, RunResult
from spectracut.studio.node import NodeStatus
from spectracut.studio.producer import BudgetCaps, Producer
from spectracut.studio.store import ProductionStore, write_atomic

if TYPE_CHECKING:
    from spectracut.studio.contract import FilmContract
    from spectracut.studio.department import Department
    from spectracut.studio.node import NodeSpec
    from spectracut.studio.profiles import QualityProfile
    from spectracut.studio.supervisor import Supervisor

MOCK_ENV = "SPECTRACUT_PROVIDERS=mock"
"""The switch that runs every provider on its mock."""

FARM_ENV = ("SPECTRACUT_FLAMENCO_URL", "SPECTRACUT_FLAMENCO_SHARED", "SPECTRACUT_FLAMENCO_TAG")
ELEVENLABS_ENV = "ELEVENLABS_API_KEY"
MODEL_HOST_ENV = "SPECTRACUT_MODEL_HOST"


class AutopilotStop(StrEnum):
    DELIVERED = "delivered"
    MISSING_PREREQUISITE = "missing_prerequisite"
    SPEND_ABOVE_CEILING = "spend_above_ceiling"
    OWNER_GATE = "owner_gate"
    CONTRACT_FAILED = "contract_failed"
    BREAKER = "breaker"
    FAILED = "failed"


def default_policy(
    profile: QualityProfile | None,
    keep: Iterable[str] = (),
    budget_ceiling: BudgetCeiling | None = None,
) -> AutonomyPolicy:
    """Delegate every gate kind ``profile`` marks ``human``, except the ``keep`` kinds."""
    kept = set(keep)
    gates = profile.gates if profile is not None else {}
    kinds = tuple(k for k, m in gates.items() if m is GateMode.HUMAN and k not in kept)
    return AutonomyPolicy(delegate_kinds=kinds, budget_ceiling=budget_ceiling or BudgetCeiling())


# ---- preflight --------------------------------------------------------------


@dataclass(frozen=True)
class Prerequisite:
    """One external prerequisite: present or missing, and the mock that stands in for it."""

    name: str
    ok: bool
    detail: str
    mock: str

    def line(self) -> str:
        state = "ok" if self.ok else "missing"
        text = f"{self.name}: {state} ({self.detail})"
        return text if self.ok else f"{text}; mock in use: {self.mock}"


@dataclass
class Preflight:
    items: list[Prerequisite]
    text: TextProvider | None
    """The text provider, or ``None`` when its sign-in is missing."""

    def lines(self) -> list[str]:
        return [p.line() for p in self.items]

    def missing(self) -> list[Prerequisite]:
        return [p for p in self.items if not p.ok]


def preflight(
    text_provider: Callable[[], TextProvider],
    *,
    env: Mapping[str, str] | None = None,
    model_host: str | None = None,
) -> Preflight:
    """Name each external prerequisite as ok or missing, with the mock in use when missing."""
    env = os.environ if env is None else env
    mock = env.get("SPECTRACUT_PROVIDERS", "").strip().lower() == "mock"
    items: list[Prerequisite] = []
    text: TextProvider | None = None
    try:
        text = text_provider()
        items.append(
            Prerequisite("text provider sign-in", True, text.name, f"{MOCK_ENV} (MockTextProvider)")
        )
    except ProviderAuthError as exc:
        items.append(
            Prerequisite(
                f"text provider sign-in ({exc.cli})",
                False,
                exc.detail,
                f"{MOCK_ENV} (MockTextProvider)",
            )
        )
    voice = mock or bool(env.get(ELEVENLABS_ENV))
    items.append(
        Prerequisite(
            "ElevenLabs final VO",
            voice,
            "MockElevenLabs" if mock else (f"{ELEVENLABS_ENV} set" if voice else ELEVENLABS_ENV),
            f"local temp VO ({MOCK_ENV} uses MockElevenLabs)",
        )
    )
    unset = [n for n in FARM_ENV if not env.get(n)]
    items.append(
        Prerequisite(
            "Flamenco farm env",
            not unset,
            "configured" if not unset else ", ".join(unset) + " unset",
            "template shots (no farm)",
        )
    )
    host = model_host or env.get(MODEL_HOST_ENV, "").strip() or None
    items.append(
        Prerequisite(
            "model host",
            mock or host is not None,
            "mock" if mock else (host or f"{MODEL_HOST_ENV} unset"),
            f"{MOCK_ENV} (mock local models)",
        )
    )
    return Preflight(items, text)


def mock_for(exc: BaseException) -> str:
    """The mock that stands in for the prerequisite ``exc`` names."""
    if isinstance(exc, ProviderError):
        if exc.cli in ("claude", "codex"):
            return f"{MOCK_ENV} (MockTextProvider)"
        if exc.cli == "elevenlabs":
            return f"local temp VO ({MOCK_ENV} uses MockElevenLabs)"
        return MOCK_ENV
    if "FLAMENCO" in str(exc):
        return "template shots (profile standard, no farm)"
    return MOCK_ENV


# ---- the run report ---------------------------------------------------------


@dataclass
class AutopilotReport:
    """What one ``make`` run did, written as ``runs/<n>/report.json`` and ``report.md``."""

    production_id: str
    stop: AutopilotStop
    detail: str
    started_at: str
    finished_at: str
    passes: int = 0
    executed: list[str] = field(default_factory=list)
    preflight: list[str] = field(default_factory=list)
    contract: dict[str, JsonValue] | None = None
    gates: list[dict[str, JsonValue]] = field(default_factory=list)
    polish: list[dict[str, JsonValue]] = field(default_factory=list)
    ledger: dict[str, JsonValue] = field(default_factory=dict)
    budget: list[str] = field(default_factory=list)
    delivered: list[dict[str, JsonValue]] = field(default_factory=list)

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "production_id": self.production_id,
            "stop": self.stop.value,
            "detail": self.detail,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "passes": self.passes,
            "executed": list(self.executed),
            "preflight": list(self.preflight),
            "contract": self.contract,
            "gates": list(self.gates),
            "polish": list(self.polish),
            "ledger": self.ledger,
            "budget": list(self.budget),
            "delivered": list(self.delivered),
        }

    def markdown(self) -> str:
        out = [
            f"# Run report: {self.production_id}",
            "",
            f"- stop: **{self.stop.value}**: {self.detail}",
            f"- passes: {self.passes}; executed: {', '.join(self.executed) or 'nothing'}",
            "",
            "## Preflight",
            *(f"- {line}" for line in self.preflight),
            "",
            "## Contract",
        ]
        if self.contract is None:
            out.append("- not frozen")
        else:
            out.append(f"- version {self.contract['version']}")
            rows = self.contract.get("rows")
            for row in rows if isinstance(rows, list) else []:
                if isinstance(row, dict):
                    out.append(f"- {row['id']} [{row['kind']}] {row['text']}: {row['verdict']}")
            verdicts = self.contract.get("verdicts")
            for v in verdicts if isinstance(verdicts, list) else []:
                if isinstance(v, dict):
                    out.append(f"- review of {v['node']}: {v['verdict']}")
        out += ["", "## Gates"]
        for g in self.gates:
            line = f"- {g['node']} ({g['mode']}): {g['status']}, approver={g['approver']}"
            if g.get("evidence"):
                line += f", evidence: {'; '.join(map(str, _list(g['evidence'])))}"
            if g.get("escalated"):
                line += f", escalated: {'; '.join(map(str, _list(g['escalated'])))}"
            out.append(line)
        out += ["", "## Polish notes"]
        out += [f"- {p['node']} ({p['source']}): {p['text']}" for p in self.polish] or ["- none"]
        totals = self.ledger.get("totals")
        out += ["", "## Cost ledger", f"- totals: {json.dumps(totals, sort_keys=True)}"]
        per_node = self.ledger.get("nodes")
        for n in per_node if isinstance(per_node, list) else []:
            if isinstance(n, dict):
                out.append(f"- {n['node']}: {json.dumps(n['totals'], sort_keys=True)}")
        out += [f"- budget: {b}" for b in self.budget]
        out += ["", "## Delivered files"]
        out += [
            f"- {d['id']} ({d['kind']}, {d['aspect']}): {d['otio_ref']}" for d in self.delivered
        ] or ["- none"]
        return "\n".join(out) + "\n"

    def write(self, production_dir: Path) -> Path:
        """Write the report under the next ``runs/<n>`` dir; returns the ``report.json`` path."""
        runs = production_dir / "runs"
        taken = [int(p.name) for p in runs.iterdir() if p.name.isdigit()] if runs.is_dir() else []
        out = runs / str(max(taken, default=0) + 1)
        data = json.dumps(self.to_json(), indent=2, sort_keys=True) + "\n"
        write_atomic(out / "report.json", data.encode())
        write_atomic(out / "report.md", self.markdown().encode())
        return out / "report.json"


def _list(value: JsonValue) -> list[JsonValue]:
    return value if isinstance(value, list) else []


# ---- running ----------------------------------------------------------------


@dataclass
class AutopilotResult:
    stop: AutopilotStop
    detail: str
    report: AutopilotReport
    report_path: Path
    executed: list[str] = field(default_factory=list)


class _ConformanceLog:
    """A conformance reviewer that records each review and its code verdict, per node."""

    def __init__(self, inner: Conformance) -> None:
        self.inner = inner
        self.reviews: dict[str, tuple[ConformanceReview, FilmContract]] = {}

    def __call__(
        self, spec: NodeSpec, output: JsonValue, contract: FilmContract
    ) -> ConformanceReview:
        review = self.inner(spec, output, contract)
        self.reviews[spec.id] = (review, contract)
        return review


def _record_failures(runner: Runner, failures: dict[str, BaseException]) -> Runner:
    def run(ctx: RunContext) -> RunResult:
        try:
            return runner(ctx)
        except Exception as exc:
            failures[ctx.spec.id] = exc
            raise

    return run


def stopped_report(
    production_id: str, stop: AutopilotStop, detail: str, preflight_lines: Sequence[str]
) -> AutopilotReport:
    """A report for a run that stopped before the graph ran."""
    now = datetime.now(UTC).isoformat()
    return AutopilotReport(production_id, stop, detail, now, now, preflight=list(preflight_lines))


def run_to_delivery(
    store: ProductionStore,
    production_id: str,
    runner: Runner,
    *,
    critic: Critic | None,
    conformance: Conformance | None,
    policy: AutonomyPolicy,
    caps: BudgetCaps | None = None,
    workers: int = 1,
    supervisor: Supervisor | None = None,
    departments: Mapping[str, type[Department]] | None = None,
    preflight_lines: Sequence[str] = (),
    max_passes: int = 100,
) -> AutopilotResult:
    """Run the stored production pass after pass until it builds nothing, then name the stop.

    Nodes left ``running`` by an interruption, and failed nodes, are built again;
    every other finished node is kept. The graph is saved after every settled node,
    and on ``KeyboardInterrupt`` before it is re-raised.
    """
    started = datetime.now(UTC).isoformat()
    graph, meta = store.load(production_id)
    for nid in graph.order():
        if graph.status(nid) in (NodeStatus.RUNNING, NodeStatus.FAILED):
            graph.invalidate(nid)
    graph.autonomy = policy
    if meta.autonomy != policy or (caps is not None and meta.caps != caps):
        meta = meta.model_copy(update={"autonomy": policy, "caps": caps or meta.caps})
        store.update_meta(production_id, meta)
    failures: dict[str, BaseException] = {}
    reviewed = _ConformanceLog(conformance) if conformance is not None else None
    wrapped = _record_failures(runner, failures)
    producer = Producer(departments)
    executed: list[str] = []
    passes = 0
    needs: list[str] = []
    budget: list[str] = []
    breaker: str | None = None

    def save(_nid: str) -> None:
        store.save(production_id, graph)

    try:
        while passes < max_passes:
            passes += 1
            result = producer.run(
                graph,
                wrapped,
                caps=meta.caps,
                approved=meta.budget_approvals,
                critic=critic,
                max_workers=workers,
                supervisor=supervisor,
                autonomy=policy,
                conformance=reviewed,
                settled=save,
            )
            executed.extend(result.run.executed)
            if result.auto_approved:
                meta = meta.model_copy(
                    update={"budget_log": (*meta.budget_log, *result.auto_approved)}
                )
                store.update_meta(production_id, meta)
                budget.extend(f"auto-approved: {a.text()}" for a in result.auto_approved)
            store.save(production_id, graph)
            needs = [h.text() for h in result.needs_approval]
            if result.supervisor_hold is not None:
                breaker = result.supervisor_hold.reason
            if not result.run.executed or result.run.failed or needs or breaker:
                break
    except KeyboardInterrupt:
        store.save(production_id, graph)
        raise
    budget.extend(f"needs approval: {n}" for n in needs)
    stop, detail = _classify(graph, failures, needs, breaker)
    report = AutopilotReport(
        production_id=production_id,
        stop=stop,
        detail=detail,
        started_at=started,
        finished_at=datetime.now(UTC).isoformat(),
        passes=passes,
        executed=executed,
        preflight=list(preflight_lines),
        contract=_contract(graph, reviewed),
        gates=_gates(graph),
        polish=_polish(graph),
        ledger=_ledger(graph),
        budget=budget,
        delivered=_delivered(graph),
    )
    path = report.write(store.path(production_id))
    return AutopilotResult(stop, detail, report, path, executed)


def _classify(
    graph: ProductionGraph,
    failures: Mapping[str, BaseException],
    needs: Sequence[str],
    breaker: str | None,
) -> tuple[AutopilotStop, str]:
    order = graph.order()
    status = graph.statuses()
    failed = [n for n in order if status[n] is NodeStatus.FAILED]
    missing: list[str] = []
    for nid in failed:
        exc = failures.get(nid)
        if isinstance(exc, ProviderAuthError | LookupError):
            missing.append(f"{nid}: {redact(str(exc))}; mock in use: {mock_for(exc)}")
    if missing:
        return AutopilotStop.MISSING_PREREQUISITE, " | ".join(missing)
    if needs:
        return AutopilotStop.SPEND_ABOVE_CEILING, " | ".join(needs)
    awaiting = [n for n in order if status[n] is NodeStatus.AWAITING_APPROVAL]
    kept = [n for n in awaiting if graph.gate_for(graph.spec(n)) is GateMode.HUMAN]
    if kept:
        return AutopilotStop.OWNER_GATE, "waiting for the owner: " + ", ".join(kept)
    contract: list[str] = []
    for nid in awaiting:
        decision = graph.decision(nid)
        if decision is not None and decision.escalated:
            contract.append(f"{nid} escalated: {'; '.join(decision.escalated)}")
    for nid in order:
        decision = graph.decision(nid)
        if (
            status[nid] is NodeStatus.REJECTED
            and graph.spec(nid).kind in CONFORMANCE_KINDS
            and decision is not None
            and decision.critic is not None
        ):
            cited = [f.text for f in decision.critic.findings if f.reviewer == "conformance"]
            if cited:
                contract.append(f"{nid} rejected after escalation: {cited[-1]}")
    if contract:
        return AutopilotStop.CONTRACT_FAILED, " | ".join(contract)
    if breaker is not None:
        return AutopilotStop.BREAKER, breaker
    if failed:
        return AutopilotStop.FAILED, " | ".join(
            f"{n}: {redact(graph.state(n).error or 'failed')}" for n in failed
        )
    rest = [n for n in order if status[n] is not NodeStatus.CLEAN]
    if rest:
        return AutopilotStop.FAILED, "not clean: " + ", ".join(
            f"{n}={status[n].value}" for n in rest
        )
    return AutopilotStop.DELIVERED, f"{len(order)} nodes clean"


def _contract(
    graph: ProductionGraph, reviewed: _ConformanceLog | None
) -> dict[str, JsonValue] | None:
    contract = graph.contract
    if contract is None:
        return None
    reviews = reviewed.reviews if reviewed is not None else {}
    verdicts: list[JsonValue] = []
    judged: dict[str, str] = {}
    for nid in graph.order():
        if nid not in reviews:
            continue
        review, against = reviews[nid]
        verdicts.append(
            {
                "node": nid,
                "contract_version": against.version,
                "verdict": conformance_verdict(against, review),
                "blockers": list(blockers(against, review)),
            }
        )
        judged.update({j.row_id: j.status for j in review.rows})
    rows: list[JsonValue] = [
        {"id": r.id, "kind": r.kind, "text": r.text, "verdict": judged.get(r.id, "not reviewed")}
        for r in contract.rows
    ]
    return {"version": contract.version, "rows": rows, "verdicts": verdicts}


def _gates(graph: ProductionGraph) -> list[dict[str, JsonValue]]:
    out: list[dict[str, JsonValue]] = []
    for nid in graph.order():
        spec = graph.spec(nid)
        mode = graph.gate_for(spec)
        if mode in (GateMode.AUTO, GateMode.CRITIC):
            continue
        decision = graph.decision(nid)
        approver = None if decision is None else decision.approver
        if decision is not None and decision.human == "approved":
            approver = "owner"
        out.append(
            {
                "node": nid,
                "kind": spec.kind,
                "mode": mode.value,
                "status": graph.status(nid).value,
                "approver": approver,
                "evidence": list(decision.evidence) if decision is not None else [],
                "escalated": list(decision.escalated) if decision is not None else [],
            }
        )
    return out


def _polish(graph: ProductionGraph) -> list[dict[str, JsonValue]]:
    out: list[dict[str, JsonValue]] = []
    for nid in graph.order():
        out += [
            {"node": nid, "source": "pass", "text": f"attempt {e.attempt} sent back for revision"}
            for e in graph.ledger.entries_for(nid)
            if e.outcome == "revise"
        ]
        decision = graph.decision(nid)
        if decision is not None and decision.critic is not None:
            for f in decision.critic.findings:
                if f.severity is not Severity.HIGH:
                    text = f"[{f.severity.value}] {f.reviewer}: {f.text}"
                    out.append({"node": nid, "source": "finding", "text": text})
        out += [{"node": nid, "source": "note", "text": n.text} for n in graph.notes(nid)]
        out += [
            {"node": nid, "source": "supervisor", "text": d} for d in graph.state(nid).decisions
        ]
    return out


def _ledger(graph: ProductionGraph) -> dict[str, JsonValue]:
    nodes: list[JsonValue] = []
    for nid in graph.order():
        entries = graph.ledger.entries_for(nid)
        if entries:
            nodes.append(
                {
                    "node": nid,
                    "totals": graph.ledger.totals(nid).model_dump(mode="json"),
                    "entries": [e.model_dump(mode="json") for e in entries],
                }
            )
    return {"totals": graph.ledger.totals().model_dump(mode="json"), "nodes": nodes}


def _delivered(graph: ProductionGraph) -> list[dict[str, JsonValue]]:
    nodes = [n for n in graph.order() if graph.spec(n).kind == "delivery"]
    out: list[dict[str, JsonValue]] = []
    for nid in nodes:
        if graph.status(nid) is not NodeStatus.CLEAN:
            continue
        try:
            report = DeliveryReport.model_validate(graph.output(nid))
        except (ValidationError, KeyError, LookupError):
            continue
        if not report.passed:
            continue
        for d in (report.master, *report.cutdowns):
            out.append(
                {
                    "id": d.id,
                    "kind": d.kind,
                    "aspect": str(d.aspect),
                    "platform": d.platform,
                    "otio_ref": d.otio_ref,
                    "frames": dict(d.frames),
                }
            )
    return out
