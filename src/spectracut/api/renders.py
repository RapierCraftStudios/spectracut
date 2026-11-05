# SPDX-License-Identifier: AGPL-3.0-or-later
"""Render routes: ``GET /v1/renders/{id}/qa`` serves a render's stored QA report."""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from spectracut.qa.report import InvalidRenderId, QAReportStore, validate_render_id


def routes(store: QAReportStore) -> list[Route]:
    """The render routes, reading reports from ``store``."""

    async def get_qa(request: Request) -> Response:
        render_id = str(request.path_params["id"])
        try:
            validate_render_id(render_id)
        except InvalidRenderId:
            return JSONResponse({"error": f"invalid render id {render_id!r}"}, status_code=400)
        report = store.load(render_id)
        if report is None:
            return JSONResponse({"error": f"no QA report for render {render_id}"}, status_code=404)
        return Response(report.model_dump_json(), media_type="application/json")

    return [Route("/v1/renders/{id}/qa", get_qa, methods=["GET"])]
