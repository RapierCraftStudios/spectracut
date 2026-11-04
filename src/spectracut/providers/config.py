# SPDX-License-Identifier: AGPL-3.0-or-later
"""Local AI provider config: which ``claude`` and ``codex`` binaries to run.

Resolution order for the Claude command:

1. ``SPECTRACUT_CLAUDE_BIN``
2. ``[ai] claude_bin`` in the local config file
3. ``claude`` on ``PATH``

The local config file is ``$SPECTRACUT_CONFIG`` if set, else
``$XDG_CONFIG_HOME/spectracut/config.toml`` (default ``~/.config/spectracut/config.toml``).
Headless wrappers that exec the real ``claude`` (e.g. ``tokenbaton-exec``) are
fine; the interactive ``claude-go`` launcher is not, since it waits for a
keypress after ``claude`` exits.
"""

from __future__ import annotations

import os
import shutil
import tomllib
from pathlib import Path
from typing import Any

from spectracut.providers.base import ProviderError

#: Interactive launchers that never return on their own in a headless call.
UNSUPPORTED_CLAUDE_BINS = frozenset({"claude-go", "tokenbaton"})


def config_path() -> Path:
    explicit = os.environ.get("SPECTRACUT_CONFIG")
    if explicit:
        return Path(explicit).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "spectracut" / "config.toml"


def load_ai_config(path: Path | None = None) -> dict[str, Any]:
    """Return the ``[ai]`` table of the local config, or ``{}``."""
    path = path or config_path()
    if not path.is_file():
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ProviderError("config", f"cannot read {path}: {exc}") from exc
    ai = data.get("ai", {})
    if not isinstance(ai, dict):
        raise ProviderError("config", f"{path}: [ai] must be a table")
    return ai


def configured_claude_bin() -> str:
    """The configured Claude command (unresolved), validated."""
    value = os.environ.get("SPECTRACUT_CLAUDE_BIN") or load_ai_config().get("claude_bin")
    command = str(value).strip() if value else "claude"
    name = Path(command).name
    if name in UNSUPPORTED_CLAUDE_BINS:
        raise ProviderError(
            "claude",
            f"`{command}` is an interactive launcher and is not supported as the Claude "
            "command: it waits for a keypress after `claude` exits. Use `claude` or a "
            "headless wrapper such as `tokenbaton-exec`.",
        )
    return command


def configured_codex_bin() -> str:
    value = os.environ.get("SPECTRACUT_CODEX_BIN") or load_ai_config().get("codex_bin")
    return str(value).strip() if value else "codex"


def _which(command: str) -> str | None:
    if os.sep in command or (os.altsep and os.altsep in command):
        path = Path(command).expanduser()
        return str(path) if path.is_file() and os.access(path, os.X_OK) else None
    return shutil.which(command)


def resolve_claude_bin() -> str | None:
    """Absolute path of the Claude command, or ``None`` when it is not installed."""
    return _which(configured_claude_bin())


def resolve_codex_bin() -> str | None:
    """Absolute path of the Codex command, or ``None`` when it is not installed."""
    return _which(configured_codex_bin())
