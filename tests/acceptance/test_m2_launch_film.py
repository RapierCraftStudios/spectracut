# SPDX-License-Identifier: AGPL-3.0-or-later
"""M2 (#63): AlterLab's launch film at the ``standard`` profile and its ads.

The always-run cases build the real :class:`ProductionGraph` from
``productions/alterlab-launch/production.json`` with the ``standard`` profile's
gates; only the departments are counting fakes. They check that the whole stage
list builds behind the owner's gates, that the ledger shows the total cost with
external usage limited to Claude, ChatGPT/Codex and ElevenLabs voice-over, that
the 15 s and 30 s cut-downs are valid for every aspect, and that a late change
(one VO line, one grade) re-runs only its dependants.

The ``e2e`` case checks the stored farm production (override the id with
``SPECTRACUT_LAUNCH_PRODUCTION``) after the real run: every node is clean, the
QA gate and audio QA passed, and a further ``studio run`` executes nothing
(post-deploy check ``m2-launch-film-farm-e2e``).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from pydantic import JsonValue
from typer.testing import CliRunner

from spectracut.cli.app import app
from spectracut.formats.cutdowns import CutDownSpec
from spectracut.spec._base import content_hash
from spectracut.studio.gates import GateMode
from spectracut.studio.graph import ProductionGraph, RunContext, RunResult
from spectracut.studio.ledger import Usage
from spectracut.studio.node import NodeSpec, NodeStatus
from spectracut.studio.profiles import load_profile
from spectracut.studio.store import ProductionStore

ROOT = Path(__file__).resolve().parents[2]
PRODUCTION = json.loads((ROOT / "productions" / "alterlab-launch" / "production.json").read_text())
ASPECTS = {"16:9", "9:16", "1:1", "4:5"}


def _graph() -> ProductionGraph:
    graph = ProductionGraph(PRODUCTION["id"], profile=load_profile(PRODUCTION["profile"]))
    graph.add_nodes(NodeSpec.model_validate(n) for n in PRODUCTION["nodes"])
    return graph


class FakeStudio:
    """Departments: each node's output is a hash of its params and inputs, usage by kind."""

    def __init__(self) -> None:
        self.built: list[str] = []

    def __call__(self, ctx: RunContext) -> RunResult:
        self.built.append(ctx.spec.id)
        kind, p = ctx.spec.kind, ctx.spec.params
        usage = Usage()
        if kind in {"brief", "concept", "script"}:
            usage = Usage(ai_usage={"claude_requests": 1, "claude_input_tokens": 1200})
        elif kind == "voiceover":
            usage = Usage(elevenlabs_characters=len(str(p["text"])))
        elif kind in {"shots", "render"}:
            usage = Usage(gpu_minutes=2.0)
        elif kind in {"qa", "audio_qa"}:
            return RunResult(output={"passed": True, "kind": kind}, usage=usage)
        out: JsonValue = {"id": ctx.spec.id, "digest": content_hash([p, ctx.inputs])}
        return RunResult(output=out, usage=usage)


def _finish(graph: ProductionGraph, studio: FakeStudio) -> None:
    """Run the film, passing each owner gate as it is reached, until nothing is waiting."""
    for _ in range(len(PRODUCTION["nodes"])):
        graph.run(studio)
        waiting = [n for n, s in graph.statuses().items() if s is NodeStatus.AWAITING_APPROVAL]
        if not waiting:
            return
        for nid in waiting:
            graph.approve(nid, "owner approved")
    raise AssertionError("film never settled")


def test_production_file_declares_every_standard_stage_and_the_owner_gates() -> None:
    kinds = {n["kind"] for n in PRODUCTION["nodes"]}
    assert {"brief", "concept", "script", "style_frames", "storyboard", "animatic"} <= kinds
    assert {"shots", "edit", "voiceover", "sound_design", "mix", "render", "delivery"} <= kinds
    graph = _graph()
    for nid in PRODUCTION["owner_gates"]:
        assert graph.gate_for(graph.spec(nid)) is GateMode.HUMAN, nid


def test_film_builds_behind_owner_gates_and_ledger_shows_cost_with_only_allowed_services() -> None:
    graph, studio = _graph(), FakeStudio()
    assert graph.run(studio).awaiting_approval == ["brief"]
    _finish(graph, studio)
    assert all(s is NodeStatus.CLEAN for s in graph.statuses().values())

    totals = graph.ledger.totals()
    assert totals.runs == len(PRODUCTION["nodes"])
    assert totals.gpu_minutes > 0
    assert totals.elevenlabs_characters == sum(
        len(n["params"]["text"]) for n in PRODUCTION["nodes"] if n["kind"] == "voiceover"
    )
    prefixes = tuple(PRODUCTION["external_usage"]["ai_metric_prefixes"])
    assert totals.ai_usage and all(k.startswith(prefixes) for k in totals.ai_usage)
    for entry in graph.ledger.entries():
        if entry.elevenlabs_characters:
            assert entry.kind == PRODUCTION["external_usage"]["elevenlabs"]
        assert all(k.startswith(prefixes) for k in entry.ai_usage)
    assert not graph.run(studio).did_work


def test_cutdowns_are_15s_and_30s_in_every_aspect_from_the_films_shots() -> None:
    shots = {n["id"] for n in PRODUCTION["nodes"] if n["kind"] == "shots"}
    cuts = [CutDownSpec.model_validate(c) for c in PRODUCTION["cutdowns"]]
    assert {(c.length_s, c.aspect) for c in cuts} == {(s, a) for s in (15, 30) for a in ASPECTS}
    assert all(set(c.shots) <= shots for c in cuts)


@pytest.mark.parametrize(
    ("node", "params", "expected"),
    [
        (
            "vo-02",
            {"text": "Every page, one clean API."},
            {"vo-02", "edit", "mix", "render", "qa", "audio-qa", "delivery"},
        ),
        (
            "grade",
            {"look": "warm-ui"},
            {"grade", "edit", "mix", "render", "qa", "audio-qa", "delivery"},
        ),
    ],
)
def test_late_change_reruns_only_its_dependants(
    node: str, params: dict[str, JsonValue], expected: set[str]
) -> None:
    graph, studio = _graph(), FakeStudio()
    _finish(graph, studio)
    runs = graph.ledger.totals().runs
    studio.built.clear()

    assert set(graph.set_params(node, params)) == expected
    _finish(graph, studio)
    assert set(studio.built) == expected
    assert graph.ledger.totals().runs == runs + len(expected)


E2E = os.environ.get("SPECTRACUT_E2E") == "1" and bool(os.environ.get("SPECTRACUT_FLAMENCO_URL"))


@pytest.mark.e2e
@pytest.mark.skipif(not E2E, reason="set SPECTRACUT_E2E=1 and SPECTRACUT_FLAMENCO_URL")
def test_e2e_stored_launch_film_passes_qa_and_audio_qa_and_reruns_nothing() -> None:
    home = os.environ.get("SPECTRACUT_HOME") or str(Path.home() / ".local/share/spectracut")
    pid = os.environ.get("SPECTRACUT_LAUNCH_PRODUCTION", PRODUCTION["id"])
    store = ProductionStore(Path(home))
    assert store.exists(pid), f"run the film on the farm host first ({pid})"
    graph, _ = store.load(pid)
    assert all(graph.status(n) is NodeStatus.CLEAN for n in graph.order())
    for kind in ("qa", "audio_qa"):
        nodes = [n for n in graph.order() if graph.spec(n).kind == kind]
        assert nodes, f"no {kind} node in {pid}"
        for nid in nodes:
            out = graph.output(nid)
            assert isinstance(out, dict) and out.get("passed") is True, nid
    prefixes = tuple(PRODUCTION["external_usage"]["ai_metric_prefixes"])
    assert all(k.startswith(prefixes) for k in graph.ledger.totals().ai_usage)
    before = len(graph.ledger)

    run = CliRunner().invoke(app, ["studio", "run", pid, "--home", home])
    assert run.exit_code == 0, run.output
    assert "executed" not in run.output, run.output
    reloaded, _ = ProductionStore(Path(home)).load(pid)
    assert len(reloaded.ledger) == before
