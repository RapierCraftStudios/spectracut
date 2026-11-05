# SPDX-License-Identifier: AGPL-3.0-or-later
"""The SpectraCut HTTP app.

Built on Starlette (already locked through ``mcp``); no CLI command serves it
yet (#35 adds ``spectracut serve``) and it carries no auth (#37).
"""

from __future__ import annotations

from starlette.applications import Starlette

from spectracut.api import renders
from spectracut.qa.report import QAReportStore


def create_app(store: QAReportStore | None = None) -> Starlette:
    """The app with the render routes, reading QA reports from ``store``."""
    return Starlette(routes=renders.routes(store or QAReportStore()))
