# SPDX-License-Identifier: AGPL-3.0-or-later
"""The AI provider layer: protocols, errors, redaction and the CLI runner.

Every AI call goes through a provider defined here. Subscription providers
drive the official CLIs (``codex``, ``claude``) with the user's own signed-in
session; the CLIs keep the tokens, SpectraCut never reads or copies them.
API-key providers are an optional fallback, used only when a CLI is missing.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import JsonValue

log = logging.getLogger(__name__)

#: Env vars that hold API keys. They are removed from subscription-CLI children
#: so a signed-in CLI never silently switches to key billing.
API_KEY_ENV_VARS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY")

#: How to sign in again, per CLI.
LOGIN_COMMANDS = {"codex": "codex login", "claude": "claude auth login"}

_AUTH_PHRASES = (
    "not logged in",
    "please log in",
    "please login",
    "log in again",
    "login required",
    "run /login",
    "expired",
    "unauthorized",
    "unauthenticated",
    "invalid_grant",
    "invalid api key",
    "authentication",
    "401",
)

_TOKEN_PATTERNS = [
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]+){0,2}"),
    re.compile(r"\b[A-Fa-f0-9]{32,}\b"),
    re.compile(r"[A-Za-z0-9+_-]{40,}={0,2}"),
]

REDACTED = "[REDACTED]"


def redact(text: str) -> str:
    """Mask token-like substrings (API keys, OAuth/JWT tokens, long secrets)."""
    for pattern in _TOKEN_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


class ProviderError(RuntimeError):
    """A provider call failed. The message names the CLI or service."""

    def __init__(self, cli: str, message: str) -> None:
        self.cli = cli
        self.detail = redact(message)
        super().__init__(f"{cli}: {self.detail}")


class ProviderAuthError(ProviderError):
    """The CLI's sign-in (or the API key) is missing, expired or rejected."""

    def __init__(self, cli: str, message: str) -> None:
        hint = LOGIN_COMMANDS.get(cli)
        if hint and hint not in message:
            message = f"{message} (sign in with `{hint}`)"
        super().__init__(cli, f"sign-in failed: {message}")


class ProviderExhausted(ProviderError):
    """The provider is out of capacity (rate limit) until ``reset_at``.

    The queue parks a job that raises this in ``waiting_for_ai`` and resumes it
    once ``reset_at`` has passed, instead of failing it.
    """

    def __init__(self, cli: str, reset_at: datetime, message: str = "rate limit reached") -> None:
        if reset_at.tzinfo is None or reset_at.utcoffset() is None:
            raise ValueError("reset_at must be timezone-aware")
        self.reset_at = reset_at
        super().__init__(cli, f"{message}; capacity resets at {reset_at.isoformat()}")


@dataclass(frozen=True)
class VisionScore:
    """A vision judge's verdict on one frame: ``score`` in [0, 1]."""

    score: float
    notes: str = ""


@dataclass(frozen=True)
class SignInState:
    """Sign-in state reported by a local CLI. Never carries tokens."""

    cli: str
    installed: bool
    signed_in: bool
    method: str | None = None
    detail: str | None = None


@runtime_checkable
class ImageProvider(Protocol):
    name: str

    def generate(self, prompt: str, out_path: Path, *, size: str = "1536x1024") -> Path:
        """Generate one image (a board frame) as PNG at ``out_path``."""
        ...


@runtime_checkable
class VisionJudge(Protocol):
    name: str

    def score(self, frame_path: Path, rubric: str) -> VisionScore:
        """Score ``frame_path`` against ``rubric``."""
        ...


@dataclass(frozen=True)
class Completion:
    """One structured text call: the JSON output and what it used, by metric slug."""

    output: JsonValue
    usage: dict[str, int]


@runtime_checkable
class TextProvider(Protocol):
    name: str

    def complete(self, system: str, prompt: str, *, schema: dict[str, Any]) -> Completion:
        """Answer ``prompt`` under ``system`` with JSON that conforms to ``schema``."""
        ...


def subscription_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """The child env for a subscription CLI: the caller's env without API keys."""
    env = dict(os.environ if base is None else base)
    for name in API_KEY_ENV_VARS:
        env.pop(name, None)
    return env


def looks_like_auth_failure(text: str) -> bool:
    lowered = text.lower()
    return any(phrase in lowered for phrase in _AUTH_PHRASES)


def run_cli(
    argv: Sequence[str],
    *,
    cli: str,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float = 600.0,
    allow_empty: bool = False,
    include_stderr: bool = False,
) -> str:
    """Run a provider CLI and return its stdout.

    A non-zero exit becomes :class:`ProviderAuthError` when the output looks like
    a sign-in problem, else :class:`ProviderError`. Empty stdout is an error too
    (unless ``allow_empty``), so a broken sign-in never reads as an empty result.
    Only redacted output ever reaches errors or logs. ``include_stderr`` returns
    stderr followed by stdout (for CLIs that print status on stderr).
    """
    child_env = dict(env) if env is not None else subscription_env()
    try:
        proc = subprocess.run(
            list(argv),
            cwd=cwd,
            env=child_env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ProviderError(cli, f"`{argv[0]}` not found; install the {cli} CLI") from exc
    except subprocess.TimeoutExpired as exc:
        raise ProviderError(cli, f"timed out after {timeout:.0f}s") from exc

    combined = f"{proc.stderr}\n{proc.stdout}".strip()
    if proc.returncode != 0:
        detail = _tail(combined) or f"exit code {proc.returncode}"
        log.warning("%s exited %s: %s", cli, proc.returncode, redact(detail))
        if looks_like_auth_failure(combined):
            raise ProviderAuthError(cli, detail)
        raise ProviderError(cli, f"exit code {proc.returncode}: {detail}")
    if not proc.stdout.strip() and not allow_empty:
        if looks_like_auth_failure(combined):
            raise ProviderAuthError(cli, _tail(combined))
        raise ProviderError(cli, "returned an empty result")
    return f"{proc.stderr}\n{proc.stdout}" if include_stderr else proc.stdout


def _tail(text: str, limit: int = 500) -> str:
    text = text.strip()
    return text[-limit:] if len(text) > limit else text


def _mock_mode() -> bool:
    return os.environ.get("SPECTRACUT_PROVIDERS", "").strip().lower() == "mock"


def select_image_provider() -> ImageProvider:
    """Pick the image provider: mock, then Codex sign-in, then ``OPENAI_API_KEY``."""
    from spectracut.providers.config import resolve_codex_bin

    if _mock_mode():
        from spectracut.providers.mock_image import MockImageProvider

        return MockImageProvider()
    codex = resolve_codex_bin()
    if codex is not None:
        from spectracut.providers.codex_cli import CodexCliImageProvider

        return CodexCliImageProvider(codex)
    if os.environ.get("OPENAI_API_KEY"):
        from spectracut.providers.api_key import OpenAIKeyImageProvider

        return OpenAIKeyImageProvider()
    raise ProviderAuthError(
        "codex", "no image provider: install the Codex CLI and sign in, or set OPENAI_API_KEY"
    )


def select_vision_judge() -> VisionJudge:
    """Pick the vision judge: mock, then Claude sign-in, then ``ANTHROPIC_API_KEY``."""
    from spectracut.providers.config import resolve_claude_bin

    if _mock_mode():
        from spectracut.providers.mock_vision import MockVisionJudge

        return MockVisionJudge()
    claude = resolve_claude_bin()
    if claude is not None:
        from spectracut.providers.claude_code import ClaudeCodeVisionJudge

        return ClaudeCodeVisionJudge(claude)
    if os.environ.get("ANTHROPIC_API_KEY"):
        from spectracut.providers.api_key import AnthropicKeyVisionJudge

        return AnthropicKeyVisionJudge()
    raise ProviderAuthError(
        "claude", "no vision judge: install Claude Code and sign in, or set ANTHROPIC_API_KEY"
    )
