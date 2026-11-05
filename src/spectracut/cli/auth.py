# SPDX-License-Identifier: AGPL-3.0-or-later
"""``spectracut auth``: AI provider sign-in state. Never prints tokens."""

from __future__ import annotations

import os

import typer

from spectracut.providers.base import API_KEY_ENV_VARS, LOGIN_COMMANDS, SignInState, redact
from spectracut.providers.claude_code import claude_status
from spectracut.providers.codex_cli import codex_status
from spectracut.providers.config import resolve_claude_bin

auth_app = typer.Typer(help="AI provider sign-in (ChatGPT via Codex CLI, Claude via Claude Code).")


def _describe(state: SignInState) -> str:
    if not state.installed:
        return f"not available ({state.detail or 'CLI not found'})"
    if state.signed_in:
        return f"signed in via {state.method}" if state.method else "signed in"
    text = f"not signed in — run `{LOGIN_COMMANDS[state.cli]}`"
    return f"{text} ({state.detail})" if state.detail else text


def status_lines() -> list[str]:
    try:
        claude_bin = resolve_claude_bin()
    except Exception:  # noqa: BLE001 - reported through claude_status below
        claude_bin = None
    lines = [
        f"ChatGPT (codex CLI): {_describe(codex_status())}",
        f"Claude (claude CLI{' at ' + claude_bin if claude_bin else ''}): "
        f"{_describe(claude_status())}",
    ]
    for name in API_KEY_ENV_VARS:
        lines.append(f"{name}: {'set' if os.environ.get(name) else 'not set'} (optional)")
    return [redact(line) for line in lines]


@auth_app.command("status")
def status() -> None:
    """Show ChatGPT and Claude sign-in state from the local CLIs."""
    for line in status_lines():
        typer.echo(line)
