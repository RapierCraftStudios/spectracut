# SPDX-License-Identifier: AGPL-3.0-or-later
"""The SpectraCut HTTP app, served on localhost by ``spectracut serve`` (#35).

It carries the render QA route and the local review page and API
(:mod:`spectracut.api.review`). It has no accounts by design: ``serve`` binds to
loopback and the app answers only requests whose ``Host`` is a loopback name, so
a DNS-rebinding page cannot reach it. Hosted auth is #37 and stays out of here.
"""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from spectracut.api import renders, review
from spectracut.qa.report import QAReportStore
from spectracut.studio.store import ProductionStore

#: ``Host`` names the app answers; ``testserver`` is Starlette's TestClient.
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "[::1]", "::1", "testserver"]


def create_app(
    store: QAReportStore | None = None, *, productions: ProductionStore | None = None
) -> Starlette:
    """The app: render routes reading QA reports from ``store``, plus the review page."""
    return Starlette(
        routes=[
            *renders.routes(store or QAReportStore()),
            *review.review_routes(productions or ProductionStore()),
        ],
        middleware=[Middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)],
    )
