# SPDX-License-Identifier: AGPL-3.0-or-later
"""Discovery and Development departments and the copy gate (#24).

Runs the departments through the real graph and department runner with
``MockTextProvider`` answering as each department; the real-model version of
the concepts flow is ``tests/acceptance/test_m2_discovery.py``.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest
from pydantic import JsonValue

from spectracut.providers.mock_text import MockTextProvider
from spectracut.spec.brief import CreativeBrief
from spectracut.studio import (
    CapacityLimits,
    DepartmentRunner,
    GateMode,
    GatePolicy,
    NodeSpec,
    NodeStatus,
    ProductionGraph,
    SkillPack,
)
from spectracut.studio.departments.discovery import (
    DISCOVERY_DEPARTMENTS,
    STORY_SKILLS,
    discovery_nodes,
)
from spectracut.studio.departments.writer import (
    DEVELOPMENT_DEPARTMENTS,
    Concept,
    Script,
    development_nodes,
    pick_concept,
)
from spectracut.writer.copy_gate import ScriptCopyGateError, check_brief, check_script, guarded

FIXTURE = json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "alterlab" / "discovery.json").read_text()
)
DEPARTMENTS = (*DISCOVERY_DEPARTMENTS, *DEVELOPMENT_DEPARTMENTS)

SOURCES = [
    {"id": "home", "title": "AlterLab home page", "ref": "https://alterlab.io/"},
    {"id": "docs", "title": "AlterLab docs", "ref": "https://alterlab.io/docs"},
]
PRODUCT = {
    "summary": "AlterLab is a web scraping API for developers.",
    "sources": SOURCES,
    "features": [
        {"text": "Returns pages as HTML, markdown or JSON", "source_id": "home"},
        {"text": "Renders JavaScript and rotates proxies", "source_id": "docs"},
    ],
}
BRIEF: dict[str, Any] = {
    "id": "alterlab-launch",
    "message": "Send a URL, get clean data back.",
    "audience": {"personas": [{"name": "Backend developer", "description": "Builds pipelines"}]},
    "sources": SOURCES,
    "proof_points": [
        {"claim": "Pages come back as HTML, markdown or JSON", "source_id": "home"},
        {"claim": "JavaScript pages render and proxies rotate", "source_id": "docs"},
    ],
    "tone": "plain, technical",
    "success_measure": "API sign-ups from the film's link",
    "cta": "Get an API key",
}


def _context(prompt: str) -> dict[str, Any]:
    return json.loads(prompt.split("\n\n", 1)[1])  # type: ignore[no-any-return]


def _script(concept_id: str, **line: Any) -> dict[str, Any]:
    base = {
        "beat": "Hook",
        "vo": "Send a URL. Get clean data back.",
        "on_screen": "One request",
        "claims": [{"text": "Pages come back as markdown", "source_id": "home"}],
    }
    return {"concept_id": concept_id, "lines": [{**base, **line}]}


class Studio:
    """A responder that answers as each story department, keyed by its system prompt."""

    def __init__(self, **overrides: Any) -> None:
        self.overrides = overrides
        self.concept_barrier: threading.Barrier | None = None

    def __call__(self, system: str, prompt: str, schema: dict[str, Any]) -> JsonValue:
        ctx = _context(prompt)
        head = system.splitlines()[0]
        if head == "# Intake":
            answers = ctx["params"]["answers"]
            return self.overrides.get("intake", answers)  # type: ignore[no-any-return]
        if head == "# Product understanding":
            return self.overrides.get("product", PRODUCT)  # type: ignore[no-any-return]
        if head == "# Strategist":
            return self.overrides.get("brief", BRIEF)  # type: ignore[no-any-return]
        if head == "# Concept writer":
            if self.concept_barrier is not None:
                self.concept_barrier.wait()  # all three must be in flight at once
            angle = ctx["params"]["angle"]
            return {
                "title": f"The {angle} film",
                "logline": f"A {angle} take on clean data from one request.",
                "structure": ["Hook", "Proof", "CTA"],
                "fit": "Developers distrust hype; this shows the request.",
            }
        if head == "# Script writer":
            if "script" in self.overrides:
                return self.overrides["script"]  # type: ignore[no-any-return]
            return _script(str(ctx["params"]["concept_id"]))
        raise AssertionError(f"unexpected department prompt {head!r}")


def _studio(
    responder: Studio, *, gates: dict[str, GateMode] | None = None, ai: int = 1
) -> tuple[ProductionGraph, Any]:
    graph = ProductionGraph("alterlab-launch", gates=GatePolicy(defaults=gates or {}))
    graph.add_nodes(discovery_nodes(FIXTURE["answers"], FIXTURE["pages"]))
    inner = DepartmentRunner(
        DEPARTMENTS,
        MockTextProvider(respond=responder),
        limits=CapacityLimits(ai=ai),
        graph=graph,
    )
    return graph, guarded(inner, graph=graph, forbidden_words=FIXTURE["forbidden_words"])


# ---- departments ------------------------------------------------------------


def test_story_departments_load_their_packs_from_skills_story() -> None:
    for dept in DEPARTMENTS:
        assert dept.pack_path() == STORY_SKILLS / dept.name
        pack = dept.pack()
        assert isinstance(pack, SkillPack) and pack.prompt and pack.rubric


def test_discovery_nodes_need_pages_with_url_and_text() -> None:
    with pytest.raises(ValueError, match="at least one page"):
        discovery_nodes(FIXTURE["answers"], [])
    with pytest.raises(ValueError, match=r"pages\[0\]"):
        discovery_nodes(FIXTURE["answers"], [{"url": "https://alterlab.io/", "text": ""}])


def test_development_nodes_read_only_the_brief_with_distinct_angles() -> None:
    nodes = development_nodes(n=4)
    assert [n.id for n in nodes] == ["concept-1", "concept-2", "concept-3", "concept-4"]
    assert all(n.inputs == ("brief",) and n.kind == "concept" for n in nodes)
    assert len({n.params["angle"] for n in nodes}) == 4
    with pytest.raises(ValueError):
        development_nodes(n=0)


# ---- AC: a brief for AlterLab cites a source for every proof point ----------


def test_alterlab_brief_cites_a_source_for_every_proof_point() -> None:
    graph, runner = _studio(Studio())
    report = graph.run(runner)
    assert report.failed == [], {n: graph.state(n).error for n in report.failed}
    assert graph.status("brief") is NodeStatus.CLEAN
    brief = CreativeBrief.model_validate(graph.output("brief"))
    declared = {s.id for s in brief.sources}
    product_sources = {s["id"] for s in graph.output("product")["sources"]}  # type: ignore[index, union-attr, call-overload]
    assert brief.proof_points
    for point in brief.proof_points:
        assert point.source_id in declared
        assert point.source_id in product_sources


def test_brief_with_an_unsourced_proof_point_fails() -> None:
    bad = {**BRIEF, "proof_points": [{"claim": "Fastest scraper", "source_id": "blog"}]}
    graph, runner = _studio(Studio(brief=bad))
    report = graph.run(runner)
    assert report.failed == ["brief"]
    assert "not a declared source" in (graph.state("brief").error or "")


def test_brief_citing_a_source_product_understanding_never_read_fails_copy_gate() -> None:
    extra = {"id": "blog", "title": "A blog", "ref": "https://example.test/blog"}
    bad = {
        **BRIEF,
        "sources": [*SOURCES, extra],
        "proof_points": [{"claim": "Loved by teams", "source_id": "blog"}],
    }
    graph, runner = _studio(Studio(brief=bad))
    assert graph.run(runner).failed == ["brief"]
    error = graph.state("brief").error or ""
    assert "ScriptCopyGateError" in error and "untraced_source" in error and "'blog'" in error


# ---- AC: three concepts in parallel; the human gate picks one ---------------


def test_three_concepts_written_in_parallel_and_human_gate_picks_one() -> None:
    studio = Studio()
    studio.concept_barrier = threading.Barrier(3, timeout=10)
    graph, runner = _studio(studio, gates={"concept": GateMode.HUMAN}, ai=3)
    graph.add_nodes(development_nodes(n=3))

    report = graph.run(runner, max_workers=3)
    concepts = ["concept-1", "concept-2", "concept-3"]
    assert report.failed == [], {n: graph.state(n).error for n in report.failed}
    assert sorted(report.awaiting_approval) == concepts
    pending = graph.snapshot().pending
    titles = {Concept.model_validate(pending[c].output).title for c in concepts}
    assert len(titles) == 3
    assert "script" not in graph.order()

    script_spec = pick_concept(graph, "concept-2", comment="clearest demo")
    assert script_spec.inputs == ("brief", "concept-2")
    assert graph.status("concept-2") is NodeStatus.CLEAN
    assert graph.status("concept-1") is NodeStatus.REJECTED
    assert graph.status("concept-3") is NodeStatus.REJECTED
    decision = graph.decision("concept-2")
    assert decision is not None and decision.human == "approved"

    report = graph.run(runner, max_workers=3)
    assert report.executed == ["script"]
    script = Script.model_validate(graph.output("script"))
    assert script.concept_id == "concept-2"


def test_pick_concept_rejects_bad_choices() -> None:
    graph, runner = _studio(Studio(), gates={"concept": GateMode.HUMAN})
    graph.add_nodes(development_nodes(n=3))
    graph.run(runner)
    with pytest.raises(ValueError, match="not a concept"):
        pick_concept(graph, "brief")
    with pytest.raises(KeyError):
        pick_concept(graph, "concept-9")
    pick_concept(graph, "concept-1")
    with pytest.raises(ValueError, match="not awaiting approval"):
        pick_concept(graph, "concept-3")  # already rejected


# ---- AC: unsourced claim or forbidden word fails the copy gate --------------


def _check(**line: Any) -> list[str]:
    script = Script.model_validate(_script("concept-1", **line))
    brief = CreativeBrief.model_validate(BRIEF)
    return [v.kind for v in check_script(script, brief, FIXTURE["forbidden_words"])]


def test_clean_script_line_passes_the_copy_gate() -> None:
    assert _check() == []


def test_script_line_with_unsourced_claim_fails_the_copy_gate() -> None:
    assert _check(claims=[{"text": "The fastest scraper"}]) == ["unsourced"]
    assert _check(claims=[{"text": "The fastest scraper", "source_id": "blog"}]) == ["unsourced"]


@pytest.mark.parametrize(
    ("field", "text"),
    [
        ("vo", "A Revolutionary way to scrape"),
        ("on_screen", "best-in-class"),
        ("claims", "revolutionary rendering"),
    ],
)
def test_script_line_with_forbidden_word_fails_the_copy_gate(field: str, text: str) -> None:
    if field == "claims":
        kinds = _check(claims=[{"text": text, "source_id": "docs"}])
    else:
        kinds = _check(**{field: text})
    assert kinds == ["forbidden_word"]


def test_forbidden_word_matches_whole_words_only() -> None:
    script = Script.model_validate(_script("concept-1", vo="Freedom to scrape"))
    brief = CreativeBrief.model_validate(BRIEF)
    assert check_script(script, brief, ["free"]) == []


def test_check_brief_flags_sources_product_notes_did_not_declare() -> None:
    from spectracut.studio.departments.discovery import ProductNotes

    product = ProductNotes.model_validate(
        {**PRODUCT, "sources": SOURCES[:1], "features": PRODUCT["features"][:1]}
    )
    violations = check_brief(CreativeBrief.model_validate(BRIEF), product)
    assert [v.kind for v in violations] == ["untraced_source"]


def test_guarded_script_with_forbidden_word_fails_even_with_auto_gate() -> None:
    bad = _script("concept-1", vo="A revolutionary API")
    graph, runner = _studio(Studio(script=bad))  # every gate auto
    graph.add_nodes(development_nodes(n=1))
    graph.run(runner)
    pick = graph.status("concept-1")
    assert pick is NodeStatus.CLEAN
    graph.add_node(
        NodeSpec(
            id="script",
            kind="script",
            department="script-writer",
            inputs=("brief", "concept-1"),
            params={"concept_id": "concept-1"},
        )
    )
    report = graph.run(runner)
    assert report.failed == ["script"]
    error = graph.state("script").error or ""
    assert "ScriptCopyGateError" in error and "forbidden word 'revolutionary'" in error


def test_copy_gate_error_is_a_department_output_error() -> None:
    from spectracut.studio import DepartmentOutputError
    from spectracut.writer.copy_gate import Violation

    err = ScriptCopyGateError(
        "script-writer", "script", [Violation(line=0, kind="unsourced", detail="x")]
    )
    assert isinstance(err, DepartmentOutputError)
    assert "copy gate: line 0 unsourced: x" in str(err)


# ---- #337: screen map --------------------------------------------------------

ROUTES = [
    {"path": "/playground", "file": "src/app/playground/page.tsx"},
    {"path": "/dashboard/billing", "file": "src/app/dashboard/billing/page.tsx"},
    {"path": "/integrations/[platform]", "file": "src/app/integrations/[platform]/page.tsx"},
]
POINTS = [p["claim"] for p in BRIEF["proof_points"]]


def _screens(**extra: Any) -> list[dict[str, Any]]:
    visit = [{"action": "visit", "target": "/playground"}, {"action": "click", "target": "Send"}]
    return [
        {"proof_point": POINTS[0], "route": "/playground", "steps": visit, "source_id": "home"},
        {"proof_point": POINTS[1], "route": "/integrations/n8n", "steps": visit},
        {"beat": "Hook", "route": "/dashboard/billing", "steps": visit},
        {"beat": "Interlude", "interlude": True},
        *extra.get("more", []),
    ]


def _script_input() -> dict[str, Any]:
    script = _script("concept-1")
    script["lines"].append({**script["lines"][0], "beat": "Interlude"})
    return script


def test_screen_map_covers_every_proof_point_and_beat() -> None:
    from spectracut.studio.departments.discovery import ProductUnderstanding

    out = {**PRODUCT, "screens": _screens()}
    ProductUnderstanding.check_output(
        "product", out, {"script-1": _script_input()}, {"script-1": "script"}
    )
    ProductUnderstanding.check_params("product", out, {"routes": ROUTES, "proof_points": POINTS})
    # outputs without screens and nodes without screen-map params are unchanged
    ProductUnderstanding.check_output("product", PRODUCT, {"intake": {}}, {"intake": "intake"})
    ProductUnderstanding.check_params("product", PRODUCT, {"pages": []})


@pytest.mark.parametrize(
    ("screens", "match"),
    [
        (_screens()[1:], "proof point .* has no screen"),
        (
            _screens(
                more=[
                    {
                        "beat": "Hook",
                        "route": "/admin",
                        "steps": [{"action": "click", "target": "x"}],
                    }
                ]
            ),
            "is not a front-end route",
        ),
    ],
)
def test_screen_map_rejects_missing_proof_points_and_unknown_routes(
    screens: list[dict[str, Any]], match: str
) -> None:
    from spectracut.studio.department import DepartmentOutputError
    from spectracut.studio.departments.discovery import ProductUnderstanding

    with pytest.raises(DepartmentOutputError, match=match):
        ProductUnderstanding.check_params(
            "product", {**PRODUCT, "screens": screens}, {"routes": ROUTES, "proof_points": POINTS}
        )


def test_screen_map_rejects_an_unmapped_beat_and_bad_mappings() -> None:
    from spectracut.studio.department import DepartmentOutputError
    from spectracut.studio.departments.discovery import ProductNotes, ProductUnderstanding

    no_interlude = [s for s in _screens() if not s.get("interlude")]
    with pytest.raises(DepartmentOutputError, match="beat 'Interlude' has no screen"):
        ProductUnderstanding.check_output(
            "product", {**PRODUCT, "screens": no_interlude}, {"s": _script_input()}, {"s": "script"}
        )
    for bad in (
        {"proof_point": POINTS[0], "interlude": True},  # a proof point needs a screen
        {"beat": "Hook", "route": "/playground"},  # no steps
        {
            "beat": "Hook",
            "proof_point": POINTS[0],
            "route": "/",
            "steps": [{"action": "wait", "target": "1s"}],
        },
        {
            "beat": "Hook",
            "route": "/",
            "steps": [{"action": "click", "target": "x"}],
            "source_id": "nope",
        },
    ):
        with pytest.raises(ValueError):
            ProductNotes.model_validate({**PRODUCT, "screens": [bad]})


def test_runner_fails_the_product_node_on_an_unknown_route() -> None:
    from spectracut.spec.product import FrontendRoute, ProductSource
    from spectracut.studio.departments.discovery import product_screen_node

    source = ProductSource.model_validate(
        {"id": "alterlab", "repo": "/r", "run": {"url": "http://x"}}
    )
    routes = [FrontendRoute.model_validate(r) for r in ROUTES]
    pages = [{"url": f"repo:{r.file}", "text": r.path} for r in routes]

    def run(screens: list[dict[str, Any]]) -> ProductionGraph:
        graph = ProductionGraph("alterlab-launch", gates=GatePolicy(defaults={}))
        graph.add_nodes(discovery_nodes(FIXTURE["answers"], FIXTURE["pages"]))
        graph.add_nodes(development_nodes(n=1))
        graph.add_nodes(
            [
                NodeSpec(
                    id="script",
                    kind="script",
                    department="script-writer",
                    inputs=("brief", "concept-1"),
                    params={"concept_id": "concept-1"},
                ),
            ]
        )
        node = product_screen_node(source, routes, pages, POINTS, "script")
        graph.add_nodes([node.model_copy(update={"id": "screens"})])
        script = {"concept_id": "concept-1", "lines": _script_input()["lines"]}
        studio = Studio(script=script)
        product_calls = iter([PRODUCT, {**PRODUCT, "screens": screens}])
        studio.overrides["product"] = None

        def respond(system: str, prompt: str, schema: dict[str, Any]) -> JsonValue:
            if system.splitlines()[0] == "# Product understanding":
                return next(product_calls)  # type: ignore[return-value]
            return studio(system, prompt, schema)

        inner = DepartmentRunner(DEPARTMENTS, MockTextProvider(respond=respond), graph=graph)
        graph.run(inner)
        if graph.status("concept-1") is NodeStatus.AWAITING_APPROVAL:
            pick_concept(graph, "concept-1")
            graph.run(inner)
        return graph

    ok = run(_screens())
    assert ok.status("screens") is NodeStatus.CLEAN, ok.state("screens").error
    bad = run([{**s, "route": "/admin"} if s.get("beat") == "Hook" else s for s in _screens()])
    assert bad.status("screens") is NodeStatus.FAILED
    assert "/admin" in str(bad.state("screens").error)
