# SPDX-License-Identifier: AGPL-3.0-or-later
"""Real end-to-end provider calls through the signed-in official CLIs.

Run with ``SPECTRACUT_E2E=1 uv run pytest -m e2e tests/providers/test_providers_e2e.py``.
Without ``SPECTRACUT_E2E=1`` these tests skip (CI stays offline). With it, a
missing or signed-out CLI is a failure, not a skip. API keys are removed from
the environment so only the subscription sign-in can succeed.
"""

from __future__ import annotations

import json
import os
import struct
from pathlib import Path

import pytest

from spectracut.providers.claude_code import (
    ClaudeCodeTextProvider,
    ClaudeCodeVisionJudge,
    claude_status,
    context_tokens,
)
from spectracut.providers.codex_cli import CodexCliImageProvider, codex_status
from spectracut.providers.mock_image import MockImageProvider
from spectracut.spec.brand import load_brand_kit
from spectracut.studio.departments.art import ArtDirector, StyleFrameArtist

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        os.environ.get("SPECTRACUT_E2E") != "1", reason="set SPECTRACUT_E2E=1 to run"
    ),
]

#: A bare ``claude -p`` loaded about 28k tokens of context; isolated calls must be far smaller.
MAX_CONTEXT_TOKENS = 10_000


@pytest.fixture(autouse=True)
def only_subscription_sign_in(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "SPECTRACUT_PROVIDERS"):
        monkeypatch.delenv(name, raising=False)


def test_codex_sign_in_generates_board_frame(tmp_path: Path) -> None:
    state = codex_status()
    assert state.installed and state.signed_in, f"codex not signed in: {state}"
    out = CodexCliImageProvider().generate(
        "Storyboard frame: a laptop on a clean desk showing a simple dashboard, soft daylight.",
        tmp_path / "board.png",
    )
    data = out.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", data[16:24])
    assert width >= 256 and height >= 256


def test_claude_sign_in_scores_frame(tmp_path: Path) -> None:
    state = claude_status()
    assert state.installed and state.signed_in, f"claude not signed in: {state}"
    frame = MockImageProvider().generate("solid", tmp_path / "frame.png", size="256x160")
    judge = ClaudeCodeVisionJudge()
    verdict = judge.score(frame, "The frame is one solid, flat colour with no objects.")
    assert 0.0 <= verdict.score <= 1.0
    assert verdict.score >= 0.5, verdict
    tokens = context_tokens(judge.last_usage)
    assert 0 < tokens < MAX_CONTEXT_TOKENS, f"per-call context {tokens} tokens"


def test_claude_sign_in_builds_art_direction() -> None:
    """Live: art direction and style frames from the AlterLab kit and concept (#265, #270).

    The Art Director's tuple-bearing output schema is accepted by the CLI, its
    palette and Look come from the brand kit, and the planned style frames each
    reference a beat or signature visual of the Glass Stack concept.
    """
    state = claude_status()
    assert state.installed and state.signed_in, f"claude not signed in: {state}"
    kit = load_brand_kit(Path(__file__).parents[2] / "brands" / "alterlab" / "brand-kit.yaml")
    concept = {
        "title": "Glass Stack",
        "logline": "AlterLab's scraping pipeline assembles itself as a stack of glass panels.",
        "structure": [
            "A raw web page dissolves into frosted glass panels",
            "The panels slide into a clean stack of structured data",
            "The AlterLab dashboard lights up on the top panel",
        ],
        "fit": "Engineers see the product turn messy pages into clean data.",
        "visuals": [
            "Frosted glass panels catching a cool rim light",
            "Structured JSON etched into the glass",
        ],
    }
    inputs = {"brand_kit": kit.model_dump(mode="json"), "concept": concept}
    kinds = {k: k for k in inputs}
    context = f"Brand kit:\n{json.dumps(inputs['brand_kit'])}\n\nApproved concept:\n" + (
        json.dumps(concept)
    )
    prompt = (
        "Write the art direction for AlterLab's launch film: a short product film for a "
        "web-scraping API. Give a mood board of three entries, a palette and a Look with "
        f"a CDL grade.\n\n{context}"
    )
    completion = ClaudeCodeTextProvider().complete(
        ArtDirector.pack().prompt, prompt, schema=ArtDirector.output_schema()
    )
    output = ArtDirector.validate_output("art_direction", completion.output)
    assert isinstance(output, dict) and output["mood_board"]
    ArtDirector.check_output("art_direction", output, inputs, kinds)

    frames_prompt = (
        "Plan two style frames for AlterLab's launch film.\n\n"
        f"{context}\n\nArt direction:\n{json.dumps(output)}"
    )
    frames = ClaudeCodeTextProvider().complete(
        StyleFrameArtist.pack().prompt, frames_prompt, schema=StyleFrameArtist.output_schema()
    )
    planned = StyleFrameArtist.validate_output("style_frames", frames.output)
    StyleFrameArtist.check_output("style_frames", planned, inputs, kinds)
