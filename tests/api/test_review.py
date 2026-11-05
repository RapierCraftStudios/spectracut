# SPDX-License-Identifier: AGPL-3.0-or-later
"""Local review API (#35): gate queue, approve/reject, frame notes, media, localhost only."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from spectracut.api.app import create_app
from spectracut.qa.report import QAReportStore
from spectracut.spec.brief import AudienceProfile, Brief, ClaimSource, Persona, ProofPoint
from spectracut.studio import (
    GateMode,
    GatePolicy,
    NodeSpec,
    NodeStatus,
    ProductionGraph,
    RunContext,
    RunResult,
)
from spectracut.studio.runner import assemble_context
from spectracut.studio.store import ProductionStore

PID = "film"
NODE = f"/v1/review/productions/{PID}/nodes"
LOCAL = {"Origin": "http://127.0.0.1:7420"}


class Department:
    """Stub department recording every context it is given."""

    def __init__(self) -> None:
        self.contexts: list[RunContext] = []

    def __call__(self, ctx: RunContext) -> RunResult:
        self.contexts.append(ctx)
        if ctx.spec.id == "board":
            media = {"kind": "board", "video": "media/board.webm", "fps": 24}
            return RunResult(output={"attempt": ctx.attempt, "review_media": media})
        return RunResult(output={"built": ctx.spec.id})


def _brief() -> Brief:
    return Brief(
        id=PID,
        product="Spellwise",
        audience=AudienceProfile(personas=[Persona(name="Dev", description="ships often")]),
        sources=[ClaimSource(id="docs", title="Docs", ref="https://example.test/docs")],
        proof_points=[ProofPoint(claim="Fast spellcheck", source_id="docs")],
        cta="Try it",
    )


@pytest.fixture
def store(tmp_path: Path) -> ProductionStore:
    store = ProductionStore(tmp_path / "home")
    g = ProductionGraph(PID, gates=GatePolicy(defaults={"board": GateMode.HUMAN}))
    g.add_nodes(
        [
            NodeSpec(id="board", kind="board", department="storyboard"),
            NodeSpec(id="render", kind="render", department="lighter", inputs=("board",)),
        ]
    )
    g.run(Department())
    path = store.create(PID, _brief(), g)
    (path / "media").mkdir()
    (path / "media" / "board.webm").write_bytes(b"0123456789")
    (tmp_path / "home" / "secret.txt").write_text("not media")
    return store


@pytest.fixture
def client(store: ProductionStore, tmp_path: Path) -> TestClient:
    return TestClient(create_app(QAReportStore(tmp_path / "qa"), productions=store))


def _graph(store: ProductionStore) -> ProductionGraph:
    return store.load(PID)[0]


def test_queue_lists_only_nodes_awaiting_a_person(client: TestClient) -> None:
    res = client.get("/v1/review/queue")
    assert res.status_code == 200
    items = res.json()["items"]
    assert [(i["production"], i["node"]) for i in items] == [(PID, "board")]
    media = items[0]["media"]
    assert media["fps"] == 24 and media["kind"] == "board"
    assert media["video"] == f"/v1/review/productions/{PID}/media/media/board.webm"


def test_node_detail_has_output_notes_and_dependants(client: TestClient) -> None:
    res = client.get(f"{NODE}/board")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "awaiting_approval"
    assert body["output"]["review_media"]["video"] == "media/board.webm"
    assert body["dependants"] == ["render"] and body["notes"] == []
    assert client.get(f"{NODE}/nope").status_code == 404
    assert client.get("/v1/review/productions/missing/nodes/board").status_code == 404
    assert client.get("/v1/review/productions/Bad!/nodes/board").status_code == 400


def test_approving_in_the_page_unblocks_dependants(
    client: TestClient, store: ProductionStore
) -> None:
    before = _graph(store).run(Department())
    assert before.blocked == ["render"]

    res = client.post(f"{NODE}/board/approve", json={"comment": "lovely"}, headers=LOCAL)
    assert res.status_code == 200 and res.json()["status"] == "clean"

    graph = _graph(store)
    assert graph.decision("board") is not None
    assert graph.decision("board").human == "approved"  # type: ignore[union-attr]
    after = graph.run(Department())
    assert after.executed == ["render"] and graph.status("render") is NodeStatus.CLEAN
    assert client.get("/v1/review/queue").json()["items"] == []
    again = client.post(f"{NODE}/board/approve", json={}, headers=LOCAL)
    assert again.status_code == 409


def test_frame_comment_reaches_the_department_as_a_revision_note(
    client: TestClient, store: ProductionStore
) -> None:
    pinned = client.post(
        f"{NODE}/board/comments",
        json={"frame": 12, "timecode": "00:00:00:12", "text": "logo too small"},
        headers=LOCAL,
    )
    assert pinned.status_code == 200
    assert pinned.json()["notes"][0]["frame"] == 12

    rejected = client.post(
        f"{NODE}/board/reject", json={"comment": "tighten the open"}, headers=LOCAL
    )
    assert rejected.status_code == 200 and rejected.json()["status"] == "dirty"

    department = Department()
    graph = _graph(store)
    graph.run(department)
    ctx = next(c for c in department.contexts if c.spec.id == "board")
    assert [(n.frame, n.text) for n in ctx.notes] == [
        (12, "logo too small"),
        (None, "tighten the open"),
    ]
    task = assemble_context(ctx, canon=None, brief=None)
    assert task.notes[0].frame == 12 and task.notes[0].timecode == "00:00:00:12"
    assert "logo too small" in task.model_dump_json()

    # The rebuilt board waits again; approving it clears the answered notes.
    assert graph.status("board") is NodeStatus.AWAITING_APPROVAL
    store.save(PID, graph)
    client.post(f"{NODE}/board/approve", json={}, headers=LOCAL)
    assert _graph(store).notes("board") == ()


def test_reject_without_revise_keeps_node_rejected(
    client: TestClient, store: ProductionStore
) -> None:
    res = client.post(f"{NODE}/board/reject", json={"revise": False}, headers=LOCAL)
    assert res.status_code == 200 and res.json()["status"] == "rejected"
    assert _graph(store).status("board") is NodeStatus.REJECTED


def test_bad_comment_bodies_are_rejected(client: TestClient) -> None:
    url = f"{NODE}/board/comments"
    assert client.post(url, json={"frame": -1, "text": "x"}, headers=LOCAL).status_code == 400
    assert client.post(url, json={"frame": 1, "text": ""}, headers=LOCAL).status_code == 400
    form = client.post(url, data={"frame": "1", "text": "x"}, headers=LOCAL)
    assert form.status_code == 415
    assert client.post(f"{NODE}/nope/comments", json={"frame": 1, "text": "x"}).status_code == 404


def test_media_is_served_with_ranges_and_never_leaves_the_production(
    client: TestClient,
) -> None:
    base = f"/v1/review/productions/{PID}/media"
    full = client.get(f"{base}/media/board.webm")
    assert full.status_code == 200 and full.content == b"0123456789"
    part = client.get(f"{base}/media/board.webm", headers={"Range": "bytes=2-4"})
    assert part.status_code == 206 and part.content == b"234"
    for bad in ("../../secret.txt", "..%2F..%2Fsecret.txt", "media/../../../secret.txt"):
        assert client.get(f"{base}/{bad}").status_code == 404, bad
    assert client.get(f"{base}/graph.json").status_code == 200  # inside the production


def test_page_binds_to_localhost_only_and_needs_no_account(client: TestClient) -> None:
    # No credentials anywhere: the page and API answer without an Authorization header.
    page = client.get("/", headers={"Host": "127.0.0.1:7420"})
    assert page.status_code == 200 and "SpectraCut review" in page.text
    assert client.get("/review/review.js", headers={"Host": "localhost:7420"}).status_code == 200
    assert client.get("/v1/review/queue", headers={"Host": "[::1]:7420"}).status_code == 200

    # A DNS-rebinding page reaches us under its own Host name: refused.
    assert client.get("/v1/review/queue", headers={"Host": "evil.example"}).status_code == 400
    assert client.get("/", headers={"Host": "192.168.1.5:7420"}).status_code == 400

    # Another site open in the browser cannot post decisions.
    cross = client.post(
        f"{NODE}/board/approve", json={}, headers={"Origin": "https://evil.example"}
    )
    assert cross.status_code == 403
    assert client.post(f"{NODE}/board/approve", json={}).status_code == 200


def test_render_qa_route_still_served(client: TestClient) -> None:
    assert client.get("/v1/renders/none/qa").status_code == 404
