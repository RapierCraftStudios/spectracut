# SPDX-License-Identifier: AGPL-3.0-or-later
"""Claude through the official Claude Code sign-in (``claude`` OAuth).

The vision judge and the text provider run ``claude -p`` headless with the user's
signed-in session.
The command is configurable (``SPECTRACUT_CLAUDE_BIN`` or ``[ai] claude_bin``),
so a multi-account wrapper such as ``tokenbaton-exec`` works. Every call uses a
minimal isolated config: no user plugins, hooks, MCP servers or slash commands,
only the Read tool, and a short system prompt. ``--bare`` is deliberately not
used: it disables OAuth and accepts only API keys.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from pydantic import JsonValue

from spectracut.providers.base import (
    Completion,
    ProviderAuthError,
    ProviderError,
    SignInState,
    VisionScore,
    looks_like_auth_failure,
    run_cli,
    subscription_env,
)
from spectracut.providers.config import configured_claude_bin, resolve_claude_bin

CLI = "claude"

JUDGE_SYSTEM_PROMPT = (
    "You are a strict vision judge for video frames. Read the image file you are given, "
    "score it against the rubric from 0.0 (fails) to 1.0 (fully meets it), and answer only "
    "with the requested JSON. Be concise."
)

SCORE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "score": {"type": "number", "minimum": 0, "maximum": 1},
        "notes": {"type": "string"},
    },
    "required": ["score", "notes"],
    "additionalProperties": False,
}

#: Flags that strip user plugins, hooks, MCP servers and skills from every call.
ISOLATION_FLAGS = [
    "--strict-mcp-config",
    "--mcp-config",
    '{"mcpServers":{}}',
    "--setting-sources",
    "",
    "--disable-slash-commands",
    "--tools",
    "Read",
    "--allowedTools",
    "Read",
    "--no-session-persistence",
]


def claude_judge_argv(claude_bin: str, prompt: str) -> list[str]:
    """The isolated headless ``claude -p`` command line for one judge call."""
    return [
        claude_bin,
        "-p",
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(SCORE_SCHEMA, separators=(",", ":")),
        *ISOLATION_FLAGS,
        "--system-prompt",
        JUDGE_SYSTEM_PROMPT,
        prompt,
    ]


def _parse_json_text(text: str) -> Any:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[1] if "\n" in text else text
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end < start:
        raise ValueError("no JSON object")
    return json.loads(text[start : end + 1])


def parse_judge_output(stdout: str) -> tuple[VisionScore, dict[str, Any]]:
    """Parse ``claude -p --output-format json`` output into a score and usage."""
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ProviderError(CLI, "claude returned output that is not JSON") from exc
    if not isinstance(envelope, dict):
        raise ProviderError(CLI, "claude returned an unexpected JSON value")
    result_text = str(envelope.get("result") or "")
    if envelope.get("is_error"):
        if looks_like_auth_failure(result_text):
            raise ProviderAuthError(CLI, result_text or "claude reported an error")
        raise ProviderError(CLI, result_text or "claude reported an error")
    payload = envelope.get("structured_output")
    if not isinstance(payload, dict):
        if not result_text.strip():
            raise ProviderError(CLI, "claude returned an empty result")
        try:
            payload = _parse_json_text(result_text)
        except (ValueError, json.JSONDecodeError) as exc:
            raise ProviderError(CLI, "claude did not return a score") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("score"), int | float):
        raise ProviderError(CLI, "claude did not return a numeric score")
    score = min(1.0, max(0.0, float(payload["score"])))
    usage = envelope.get("usage")
    return VisionScore(score=score, notes=str(payload.get("notes", ""))), (
        usage if isinstance(usage, dict) else {}
    )


def context_tokens(usage: dict[str, Any]) -> int:
    """Input-side tokens of one call (fresh + cache read + cache creation)."""
    keys = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    return sum(int(usage.get(k) or 0) for k in keys)


class ClaudeCodeVisionJudge:
    """Scores frames through headless Claude Code and the Claude sign-in."""

    name = "claude-code"

    def __init__(self, claude_bin: str | None = None, *, timeout: float = 300.0) -> None:
        self.claude_bin = claude_bin or resolve_claude_bin() or configured_claude_bin()
        self.timeout = timeout
        self.last_usage: dict[str, Any] = {}

    def score(self, frame_path: Path, rubric: str) -> VisionScore:
        frame_path = Path(frame_path)
        if not frame_path.is_file():
            raise ProviderError(CLI, f"frame not found: {frame_path}")
        with tempfile.TemporaryDirectory(prefix="spectracut-claude-") as tmp:
            workdir = Path(tmp)
            local = workdir / f"frame{frame_path.suffix or '.png'}"
            shutil.copyfile(frame_path, local)
            prompt = (
                f"Read the image ./{local.name} and score it against this rubric.\nRubric: {rubric}"
            )
            stdout = run_cli(
                claude_judge_argv(self.claude_bin, prompt),
                cli=CLI,
                cwd=workdir,
                env=subscription_env(),
                timeout=self.timeout,
            )
        verdict, self.last_usage = parse_judge_output(stdout)
        return verdict


#: Flags for text calls: the judge's isolation, but no tools at all.
TEXT_ISOLATION_FLAGS = [
    "--strict-mcp-config",
    "--mcp-config",
    '{"mcpServers":{}}',
    "--setting-sources",
    "",
    "--disable-slash-commands",
    "--tools",
    "",
    "--no-session-persistence",
]


def claude_text_argv(
    claude_bin: str, system: str, prompt: str, schema: dict[str, Any]
) -> list[str]:
    """The isolated headless ``claude -p`` command line for one text completion."""
    return [
        claude_bin,
        "-p",
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(schema, separators=(",", ":")),
        *TEXT_ISOLATION_FLAGS,
        "--system-prompt",
        system,
        prompt,
    ]


def parse_text_output(stdout: str) -> tuple[JsonValue, dict[str, Any]]:
    """Parse ``claude -p --output-format json`` output into a JSON object and usage."""
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ProviderError(CLI, "claude returned output that is not JSON") from exc
    if not isinstance(envelope, dict):
        raise ProviderError(CLI, "claude returned an unexpected JSON value")
    result_text = str(envelope.get("result") or "")
    if envelope.get("is_error"):
        if looks_like_auth_failure(result_text):
            raise ProviderAuthError(CLI, result_text or "claude reported an error")
        raise ProviderError(CLI, result_text or "claude reported an error")
    payload = envelope.get("structured_output")
    if not isinstance(payload, dict):
        if not result_text.strip():
            raise ProviderError(CLI, "claude returned an empty result")
        try:
            payload = _parse_json_text(result_text)
        except (ValueError, json.JSONDecodeError) as exc:
            raise ProviderError(CLI, "claude did not return a JSON object") from exc
    if not isinstance(payload, dict):
        raise ProviderError(CLI, "claude did not return a JSON object")
    usage = envelope.get("usage")
    return payload, (usage if isinstance(usage, dict) else {})


class ClaudeCodeTextProvider:
    """A :class:`~spectracut.providers.base.TextProvider` through headless Claude Code.

    Each call runs in an empty scratch directory with no tools, plugins, hooks or
    MCP servers, and with API keys removed so only the subscription sign-in works.
    """

    name = "claude-code-text"

    def __init__(self, claude_bin: str | None = None, *, timeout: float = 600.0) -> None:
        self.claude_bin = claude_bin or resolve_claude_bin() or configured_claude_bin()
        self.timeout = timeout

    def complete(self, system: str, prompt: str, *, schema: dict[str, Any]) -> Completion:
        with tempfile.TemporaryDirectory(prefix="spectracut-claude-") as tmp:
            stdout = run_cli(
                claude_text_argv(self.claude_bin, system, prompt, schema),
                cli=CLI,
                cwd=Path(tmp),
                env=subscription_env(),
                timeout=self.timeout,
            )
        output, usage = parse_text_output(stdout)
        return Completion(
            output=output,
            usage={
                "claude_requests": 1,
                "claude_input_tokens": context_tokens(usage),
                "claude_output_tokens": int(usage.get("output_tokens") or 0),
            },
        )


def claude_status(claude_bin: str | None = None) -> SignInState:
    """Claude sign-in state from ``claude auth status --json``.

    Only ``loggedIn``, ``authMethod`` and ``subscriptionType`` are kept; account
    details and tokens are never returned.
    """
    try:
        binary = claude_bin or resolve_claude_bin()
    except ProviderError as exc:
        return SignInState(CLI, installed=False, signed_in=False, detail=exc.detail)
    if binary is None:
        return SignInState(
            CLI, installed=False, signed_in=False, detail=f"{configured_claude_bin()} not found"
        )
    try:
        out = run_cli([binary, "auth", "status", "--json"], cli=CLI, timeout=30)
    except ProviderAuthError:
        return SignInState(CLI, installed=True, signed_in=False)
    except ProviderError as exc:
        return SignInState(CLI, installed=True, signed_in=False, detail=exc.detail)
    try:
        data = _parse_json_text(out)
    except (ValueError, json.JSONDecodeError):
        return SignInState(CLI, installed=True, signed_in=False, detail="unrecognised status")
    if not isinstance(data, dict):
        return SignInState(CLI, installed=True, signed_in=False, detail="unrecognised status")
    method_parts = [
        str(data[k]) for k in ("authMethod", "subscriptionType") if isinstance(data.get(k), str)
    ]
    return SignInState(
        CLI,
        installed=True,
        signed_in=bool(data.get("loggedIn")),
        method=", ".join(method_parts) or None,
    )
