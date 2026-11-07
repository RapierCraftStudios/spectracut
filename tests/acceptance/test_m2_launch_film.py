# SPDX-License-Identifier: AGPL-3.0-or-later
"""M2 (#63, #253): AlterLab's launch film at the ``standard`` profile and its ads.

The CI case (the ``launch_film`` fixture in ``tests/acceptance/recorded``) runs the real pipeline:
``studio new productions/alterlab-launch/brief.json --profile standard``, then
``studio run`` and ``studio approve`` of each owner gate until delivery is clean. Every
node is built by ``default_runner`` and the real departments; only the external
dependencies are injected: recorded Claude responses (``tests/acceptance/recorded/``),
a fake Flamenco farm and the audio stack (offline ElevenLabs voice, fake Ardour). It
checks every node is clean, delivery wrote the master and the 8 cut-downs, the ledger
shows only Claude, ChatGPT/Codex and ElevenLabs voice-over as external usage, and a
further ``studio run`` executes nothing.

The ``e2e`` case (``SPECTRACUT_E2E=1`` and ``SPECTRACUT_FLAMENCO_URL``, post-deploy
check ``m2-launch-film-farm-e2e``) runs the same new/run/approve loop live with no
injection, under production id ``SPECTRACUT_LAUNCH_PRODUCTION`` (default
``alterlab-launch``).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
import recorded  # type: ignore[import-not-found]
from recorded import BRIEF, Film, external_ai_usage, invoke, run_film

from spectracut.formats.cutdowns import CutDownSpec
from spectracut.providers.local.registry import resolve_model
from spectracut.studio.departments import art as art_depts
from spectracut.studio.departments.finishing import DeliveryReport
from spectracut.studio.departments.sound import SoundDesign
from spectracut.studio.gates import GateMode
from spectracut.studio.graph import ProductionGraph
from spectracut.studio.ledger import Ledger
from spectracut.studio.node import NodeSpec, NodeStatus
from spectracut.studio.profiles import load_profile
from spectracut.studio.store import ProductionStore

launch_film = recorded.launch_film
"""The standard film, run once with recorded providers and a fake farm (session fixture)."""

ROOT = Path(__file__).resolve().parents[2]
PRODUCTION = json.loads((ROOT / "productions" / "alterlab-launch" / "production.json").read_text())
ASPECTS = {"16:9", "9:16", "1:1", "4:5"}
PREFIXES = tuple(PRODUCTION["external_usage"]["ai_metric_prefixes"])
ELEVENLABS_KIND = PRODUCTION["external_usage"]["elevenlabs"]


def _graph() -> ProductionGraph:
    graph = ProductionGraph(PRODUCTION["id"], profile=load_profile(PRODUCTION["profile"]))
    graph.add_nodes(NodeSpec.model_validate(n) for n in PRODUCTION["nodes"])
    return graph


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def assert_delivered(report: DeliveryReport) -> None:
    """The master and all 8 cut-downs are on disk and match their recorded digests."""
    assert report.master.kind == "master"
    assert {(c.length_s, c.aspect) for c in report.cutdowns} == {
        (s, a) for s in (15.0, 30.0) for a in ASPECTS
    }
    for item in (report.master, *report.cutdowns):
        path = Path(item.otio_ref)
        assert path.is_file(), item.id
        assert item.otio_sha256.removeprefix("sha256:") == _sha256(path), item.id
        assert item.qa_passed, (item.id, item.qa_failures)


def assert_only_allowed_services(ledger: Ledger) -> None:
    """External usage is Claude/ChatGPT/Codex (by metric prefix) and ElevenLabs VO only."""
    for entry in ledger.entries():
        external = external_ai_usage(entry.ai_usage)
        assert all(k.startswith(PREFIXES) for k in external), (entry.node_id, external)
        if entry.elevenlabs_characters:
            assert entry.kind == ELEVENLABS_KIND, entry.node_id


def assert_sound_design_on_real_models(graph: ProductionGraph) -> None:
    """the sound_design node was built by a real local model and its GPU usage is in the ledger."""
    node_id = next(n for n in graph.order() if graph.spec(n).kind == "sound_design")
    design = SoundDesign.model_validate(graph.output(node_id))
    model = resolve_model("sfx").id
    assert design.cues
    for cue in design.cues:
        assert "mock" not in cue.provenance.origin, cue.provenance.origin
        assert cue.provenance.origin.startswith(model), cue.provenance.origin
    entries = graph.ledger.entries_for(node_id)
    assert entries and sum(e.gpu_minutes for e in entries) > 0, entries


def test_production_file_declares_every_standard_stage_and_the_owner_gates() -> None:
    kinds = {n["kind"] for n in PRODUCTION["nodes"]}
    assert {"brief", "concept", "script", "style_frames", "storyboard", "animatic"} <= kinds
    assert {"shots", "edit", "voiceover", "sound_design", "mix", "render", "delivery"} <= kinds
    graph = _graph()
    for nid in PRODUCTION["owner_gates"]:
        assert graph.gate_for(graph.spec(nid)) is GateMode.HUMAN, nid


def test_real_model_check_rejects_the_recorded_films_mock_sound_design(launch_film: Film) -> None:
    """Regression: mock-origin cues fail the live assertion, so a mock-mode run cannot pass."""
    with pytest.raises(AssertionError, match="mock"):
        assert_sound_design_on_real_models(launch_film.graph)


def test_cutdowns_are_15s_and_30s_in_every_aspect_from_the_films_shots() -> None:
    shots = {n["id"] for n in PRODUCTION["nodes"] if n["kind"] == "shots"}
    cuts = [CutDownSpec.model_validate(c) for c in PRODUCTION["cutdowns"]]
    assert {(c.length_s, c.aspect) for c in cuts} == {(s, a) for s in (15, 30) for a in ASPECTS}
    assert all(set(c.shots) <= shots for c in cuts)


def test_standard_film_runs_every_node_with_the_real_departments(launch_film: Film) -> None:
    graph = launch_film.graph
    standard = load_profile("standard")
    # ``--brand-kit`` adds a brand_kit node (#270) and every film a references node (#342)
    # the art nodes read, beside the stages.
    assert {graph.spec(n).kind for n in graph.order()} == {
        *standard.stages,
        art_depts.BRAND_KIT,
        art_depts.REFERENCES,
    }
    assert {n: graph.status(n) for n in graph.order()} == {
        n: NodeStatus.CLEAN for n in graph.order()
    }
    # Every text department answered through the provider, from its recorded response.
    assert set(launch_film.provider.calls) >= {
        "CreativeBrief",
        "Concept",
        "Script",
        "ArtDirection",
        "StyleFrames",
        "Storyboard",
        "ShotPlan",
    }
    # Shots, the graded master and every aspect of the delivery rendered on the farm.
    assert launch_film.farm.submitted
    gated = {
        nid.strip()
        for out in launch_film.outputs
        for line in out.splitlines()
        if line.startswith("awaiting approval:")
        for nid in line.partition(":")[2].split(",")
    }
    assert gated == {"brief", "concept", "style_frames", "animatic", "delivery"}


def test_delivery_writes_the_master_and_all_cutdowns(launch_film: Film) -> None:
    report = DeliveryReport.model_validate(launch_film.graph.output("delivery"))
    assert_delivered(report)
    assert report.passed and not report.temp_items


def test_ledger_shows_only_claude_chatgpt_and_elevenlabs(launch_film: Film) -> None:
    ledger = launch_film.graph.ledger
    assert_only_allowed_services(ledger)
    totals = ledger.totals()
    assert any(k.startswith("claude_") for k in totals.ai_usage)
    spoken = launch_film.voice.calls
    assert spoken and totals.elevenlabs_characters == sum(len(t) for t in spoken)
    assert totals.gpu_minutes > 0


def test_a_further_run_executes_nothing(launch_film: Film) -> None:
    assert "executed" not in launch_film.rerun, launch_film.rerun
    assert len(launch_film.graph.ledger) == launch_film.ledger_entries


E2E = os.environ.get("SPECTRACUT_E2E") == "1" and bool(os.environ.get("SPECTRACUT_FLAMENCO_URL"))


@pytest.mark.e2e
@pytest.mark.skipif(not E2E, reason="set SPECTRACUT_E2E=1 and SPECTRACUT_FLAMENCO_URL")
def test_e2e_launch_film_reaches_delivery_with_only_allowed_services() -> None:
    """Live: ``studio new`` + run/approve through delivery on the farm, nothing injected."""
    home = Path(os.environ.get("SPECTRACUT_HOME") or Path.home() / ".local/share/spectracut")
    pid = os.environ.get("SPECTRACUT_LAUNCH_PRODUCTION", PRODUCTION["id"])
    store = ProductionStore(home)
    run_film(home, pid, brief=BRIEF)
    graph, _ = store.load(pid)
    assert all(graph.status(n) is NodeStatus.CLEAN for n in graph.order())
    assert_delivered(DeliveryReport.model_validate(graph.output("delivery")))
    assert_only_allowed_services(graph.ledger)
    assert_sound_design_on_real_models(graph)
    before = len(graph.ledger)
    again = invoke(["studio", "run", pid, "--home", str(home)])
    assert "executed" not in again, again
    reloaded, _ = store.load(pid)
    assert len(reloaded.ledger) == before
