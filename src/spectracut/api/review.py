# SPDX-License-Identifier: AGPL-3.0-or-later
"""The local review API and page served by ``spectracut serve`` (#35).

Routes (all JSON unless noted)::

    GET  /                                                     the review page (HTML)
    GET  /review/{asset}                                       page assets
    GET  /v1/review/queue                                      nodes awaiting a person
    GET  /v1/review/productions/{pid}/nodes/{nid}              one node: output, notes, media
    GET  /v1/review/productions/{pid}/media/{path}             a media file (Range supported)
    POST /v1/review/productions/{pid}/nodes/{nid}/approve      {"comment"}
    POST /v1/review/productions/{pid}/nodes/{nid}/reject       {"comment", "revise": true}
    POST /v1/review/productions/{pid}/nodes/{nid}/comments     {"frame", "text", "timecode"}

The page only reviews: productions are created, run and steered from the CLI and
the MCP server. There are no accounts; ``serve`` binds to loopback and the app
rejects non-local ``Host`` headers. Mutations need a JSON body and an ``Origin``
that is absent or loopback, so another site open in the browser cannot post here.

A node shows media when its output (pending or accepted) carries ``review_media``::

    {"kind": "board|style_frame|animatic|render", "video": "media/board.webm",
     "audio": "media/board.ogg", "fps": 24}

Paths are relative to the production directory and never leave it.
"""

from __future__ import annotations

import inspect
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, urlsplit

from pydantic import Field, ValidationError
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import BaseRoute, Mount, Route
from starlette.staticfiles import StaticFiles

from spectracut.spec._base import StrictModel
from spectracut.studio.graph import ProductionGraph, RevisionNote
from spectracut.studio.node import NodeStatus
from spectracut.studio.store import ProductionError, ProductionStore

MEDIA_KEY = "review_media"
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]", "::1"})

_PACKAGED_STATIC = Path(__file__).resolve().parent / "review_static"
_REPO_STATIC = Path(__file__).resolve().parents[3] / "apps" / "review" / "static"


def static_dir() -> Path:
    """The review page directory: packaged in the wheel, else the repo checkout."""
    return _PACKAGED_STATIC if _PACKAGED_STATIC.is_dir() else _REPO_STATIC


class ReviewMedia(StrictModel):
    """What the player plays for a node, declared in its output."""

    kind: Literal["board", "style_frame", "animatic", "render"]
    video: str = Field(min_length=1)
    """Video or still image, relative to the production directory."""
    audio: str | None = None
    """Separate audio track, relative to the production directory."""
    fps: float = Field(gt=0, le=240)


class _Decision(StrictModel):
    comment: str = Field(default="", max_length=4000)


class _Reject(_Decision):
    revise: bool = True
    """Mark the node dirty so the next ``studio run`` rebuilds it with its notes."""


class _Comment(StrictModel):
    frame: int = Field(ge=0)
    text: str = Field(min_length=1, max_length=4000)
    timecode: str | None = Field(default=None, max_length=32)


class _HttpError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _contained(base: Path, relative: str) -> Path | None:
    """``base / relative`` if it resolves inside ``base``, else ``None``."""
    if not relative or relative.startswith(("/", "\\")) or "\\" in relative:
        return None
    root = base.resolve()
    target = (root / relative).resolve()
    return target if target.is_relative_to(root) and target != root else None


def is_loopback_origin(origin: str | None) -> bool:
    """True when ``origin`` is absent or a loopback ``http(s)`` origin."""
    if origin is None:
        return True
    parts = urlsplit(origin)
    if parts.scheme not in ("http", "https"):
        return False
    host = parts.hostname or ""
    return host in LOOPBACK_HOSTS or f"[{host}]" in LOOPBACK_HOSTS


def _output(graph: ProductionGraph, node_id: str) -> Any:
    status = graph.status(node_id)
    if status is NodeStatus.AWAITING_APPROVAL:
        pending = graph.snapshot().pending.get(node_id)
        return pending.output if pending is not None else None
    if status is NodeStatus.CLEAN:
        return graph.output(node_id)
    return None


def _media(production_id: str, output: Any) -> tuple[dict[str, Any] | None, str | None]:
    """The player descriptor for an output, or an error explaining why there is none."""
    if not isinstance(output, dict) or MEDIA_KEY not in output:
        return None, None
    try:
        media = ReviewMedia.model_validate(output[MEDIA_KEY])
    except ValidationError as exc:
        return None, f"{MEDIA_KEY} is invalid: {exc.error_count()} error(s)"
    base = f"/v1/review/productions/{production_id}/media/"
    return {
        "kind": media.kind,
        "fps": media.fps,
        "video": base + quote(media.video),
        "audio": base + quote(media.audio) if media.audio else None,
    }, None


def _note_json(note: RevisionNote) -> dict[str, Any]:
    return note.model_dump(mode="json")


def review_routes(store: ProductionStore) -> list[BaseRoute]:
    """The review routes and page over the productions in ``store``."""
    lock = threading.Lock()

    def load(production_id: str) -> ProductionGraph:
        try:
            if not store.exists(production_id):
                raise _HttpError(404, f"no production {production_id!r}")
            graph, _ = store.load(production_id)
        except ProductionError as exc:
            status = 400 if "must be a slug" in str(exc) else 404
            raise _HttpError(status, str(exc)) from exc
        return graph

    def node_of(graph: ProductionGraph, node_id: str) -> None:
        try:
            graph.spec(node_id)
        except KeyError:
            raise _HttpError(404, f"no node {node_id!r} in {graph.production_id!r}") from None

    def queue_entry(production_id: str, graph: ProductionGraph, node_id: str) -> dict[str, Any]:
        spec = graph.spec(node_id)
        media, media_error = _media(production_id, _output(graph, node_id))
        return {
            "production": production_id,
            "node": node_id,
            "kind": spec.kind,
            "department": spec.department,
            "status": graph.status(node_id).value,
            "media": media,
            "media_error": media_error,
            "notes": len(graph.notes(node_id)),
        }

    def queue(request: Request) -> Response:
        items: list[dict[str, Any]] = []
        errors: list[str] = []
        for pid in store.list():
            try:
                graph, _ = store.load(pid)
            except ProductionError as exc:
                errors.append(str(exc))
                continue
            for nid, status in graph.statuses().items():
                if status is NodeStatus.AWAITING_APPROVAL:
                    items.append(queue_entry(pid, graph, nid))
        return JSONResponse({"items": items, "errors": errors})

    def node(request: Request) -> Response:
        pid, nid = request.path_params["pid"], request.path_params["nid"]
        graph = load(pid)
        node_of(graph, nid)
        spec = graph.spec(nid)
        output = _output(graph, nid)
        decision = graph.decision(nid)
        entry = queue_entry(pid, graph, nid)
        entry.update(
            {
                "spec": spec.model_dump(mode="json"),
                "output": output,
                "decision": decision.model_dump(mode="json") if decision else None,
                "notes": [_note_json(n) for n in graph.notes(nid)],
                "dependants": [d for d in graph.order() if nid in graph.spec(d).inputs],
            }
        )
        return JSONResponse(entry)

    def media(request: Request) -> Response:
        pid, rel = request.path_params["pid"], request.path_params["path"]
        try:
            base = store.path(pid)
        except ProductionError as exc:
            raise _HttpError(400, str(exc)) from exc
        target = _contained(base, rel)
        if target is None or not target.is_file():
            raise _HttpError(404, f"no media {rel!r} in production {pid!r}")
        return FileResponse(target)

    async def body[M: StrictModel](request: Request, model: type[M]) -> M:
        if not is_loopback_origin(request.headers.get("origin")):
            raise _HttpError(403, "cross-origin requests are not allowed")
        ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise _HttpError(415, "send a JSON body (Content-Type: application/json)")
        raw = await request.body()
        try:
            return model.model_validate_json(raw or b"{}")
        except ValidationError as exc:
            raise _HttpError(400, f"invalid body: {exc.error_count()} error(s)") from exc

    def mutate(pid: str, nid: str, change: Callable[[ProductionGraph], None]) -> ProductionGraph:
        with lock:
            graph = load(pid)
            node_of(graph, nid)
            try:
                change(graph)
            except ValueError as exc:
                raise _HttpError(409, str(exc)) from exc
            store.save(pid, graph)
            return graph

    def result(graph: ProductionGraph, nid: str) -> Response:
        return JSONResponse(
            {
                "production": graph.production_id,
                "node": nid,
                "status": graph.status(nid).value,
                "notes": [_note_json(n) for n in graph.notes(nid)],
            }
        )

    async def approve(request: Request) -> Response:
        pid, nid = request.path_params["pid"], request.path_params["nid"]
        data = await body(request, _Decision)
        graph = mutate(pid, nid, lambda g: g.approve(nid, data.comment))
        return result(graph, nid)

    async def reject(request: Request) -> Response:
        pid, nid = request.path_params["pid"], request.path_params["nid"]
        data = await body(request, _Reject)

        def change(g: ProductionGraph) -> None:
            g.reject(nid, data.comment)
            if data.revise:
                if data.comment.strip():
                    g.add_note(
                        nid,
                        RevisionNote(node_id=nid, text=data.comment, created_at=datetime.now(UTC)),
                    )
                g.invalidate(nid)

        return result(mutate(pid, nid, change), nid)

    async def comment(request: Request) -> Response:
        pid, nid = request.path_params["pid"], request.path_params["nid"]
        data = await body(request, _Comment)
        note = RevisionNote(
            node_id=nid,
            frame=data.frame,
            timecode=data.timecode,
            text=data.text,
            created_at=datetime.now(UTC),
        )
        graph = mutate(pid, nid, lambda g: g.add_note(nid, note))
        return result(graph, nid)

    def page(request: Request) -> Response:
        index = static_dir() / "index.html"
        if not index.is_file():
            return JSONResponse({"error": "review page is not installed"}, status_code=404)
        return FileResponse(index, media_type="text/html")

    def guarded(handler: Callable[[Request], Any]) -> Callable[[Request], Any]:
        if inspect.iscoroutinefunction(handler):

            async def run_async(request: Request) -> Response:
                try:
                    response: Response = await handler(request)
                    return response
                except _HttpError as exc:
                    return JSONResponse({"error": exc.message}, status_code=exc.status)

            return run_async

        def run_sync(request: Request) -> Response:
            try:
                response: Response = handler(request)
                return response
            except _HttpError as exc:
                return JSONResponse({"error": exc.message}, status_code=exc.status)

        return run_sync

    prefix = "/v1/review/productions/{pid}"
    return [
        Route("/", page, methods=["GET"]),
        Mount("/review", app=StaticFiles(directory=static_dir(), check_dir=False)),
        Route("/v1/review/queue", guarded(queue), methods=["GET"]),
        Route(prefix + "/nodes/{nid}", guarded(node), methods=["GET"]),
        Route(prefix + "/media/{path:path}", guarded(media), methods=["GET"]),
        Route(prefix + "/nodes/{nid}/approve", guarded(approve), methods=["POST"]),
        Route(prefix + "/nodes/{nid}/reject", guarded(reject), methods=["POST"]),
        Route(prefix + "/nodes/{nid}/comments", guarded(comment), methods=["POST"]),
    ]
