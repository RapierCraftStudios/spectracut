# SPDX-License-Identifier: AGPL-3.0-or-later
"""Opt-in: the MCP stdio product-shot flow on a real Blender 4.5 render host.

    SPECTRACUT_E2E=1 uv run pytest -m e2e tests/harness/test_harness_e2e.py

needs ``blender`` (4.5) on PATH or ``SPECTRACUT_BLENDER``. The host agent and the
execution guard run for real; only the host registration is a temp file.
"""

from __future__ import annotations

import os
import shutil

import pytest
from test_server_stdio import check_product_shot, product_shot, run_client

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(os.environ.get("SPECTRACUT_E2E") != "1", reason="set SPECTRACUT_E2E=1"),
]

BLENDER = os.environ.get("SPECTRACUT_BLENDER") or shutil.which("blender")


@pytest.mark.skipif(BLENDER is None, reason="needs Blender 4.5 on PATH or SPECTRACUT_BLENDER")
def test_real_blender_product_shot_preview(server_env: dict[str, str]) -> None:
    assert BLENDER is not None
    env = {**server_env, "SPECTRACUT_BLENDER": BLENDER, "PATH": os.environ.get("PATH", "")}
    check_product_shot(run_client(env, product_shot))
