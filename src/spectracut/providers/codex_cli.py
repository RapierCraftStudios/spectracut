# SPDX-License-Identifier: AGPL-3.0-or-later
"""ChatGPT through the official Codex CLI sign-in (``codex login``).

The provider runs ``codex exec`` with the user's signed-in session and the
built-in image generation tool. The Codex CLI keeps its tokens; SpectraCut never
reads them. Each call runs ephemerally, without the user's config, in a fresh
temporary directory.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from spectracut.providers.base import (
    Completion,
    ProviderAuthError,
    ProviderError,
    SignInState,
    run_cli,
    subscription_env,
)
from spectracut.providers.config import configured_codex_bin, resolve_codex_bin

CLI = "codex"

IMAGE_PROMPT = (
    "Use your image generation tool to create exactly one image for this storyboard frame.\n"
    "Frame: {prompt}\n"
    "Size: {size} (landscape unless the size says otherwise).\n"
    "Save the generated image as PNG at ./frame.png in the current working directory. "
    "Do not run other commands, write other files or ask questions. "
    "Reply with the single word DONE when frame.png is written."
)


def codex_exec_argv(codex_bin: str, workdir: Path, prompt: str) -> list[str]:
    """The isolated, non-interactive ``codex exec`` command line."""
    return [
        codex_bin,
        "exec",
        "--ephemeral",
        "--skip-git-repo-check",
        "--ignore-user-config",
        "--sandbox",
        "workspace-write",
        "-C",
        str(workdir),
        "-o",
        str(workdir / "last-message.txt"),
        prompt,
    ]


def _generated_images_dir() -> Path:
    home = os.environ.get("CODEX_HOME")
    return (Path(home).expanduser() if home else Path.home() / ".codex") / "generated_images"


def _newest_png(roots: list[Path], since: float) -> Path | None:
    """Newest PNG from the first root that has one (the call's own workdir first)."""
    for root in roots:
        if not root.is_dir():
            continue
        found = [p for p in root.rglob("*.png") if p.stat().st_mtime >= since]
        if found:
            return max(found, key=lambda p: p.stat().st_mtime)
    return None


class CodexCliImageProvider:
    """Generates board frames through ``codex exec`` and the ChatGPT sign-in."""

    name = "codex-cli"

    def __init__(self, codex_bin: str | None = None, *, timeout: float = 900.0) -> None:
        self.codex_bin = codex_bin or resolve_codex_bin() or configured_codex_bin()
        self.timeout = timeout

    def generate(self, prompt: str, out_path: Path, *, size: str = "1536x1024") -> Path:
        out_path = Path(out_path)
        started = time.time() - 1
        with tempfile.TemporaryDirectory(prefix="spectracut-codex-") as tmp:
            workdir = Path(tmp)
            argv = codex_exec_argv(
                self.codex_bin, workdir, IMAGE_PROMPT.format(prompt=prompt, size=size)
            )
            run_cli(
                argv,
                cli=CLI,
                cwd=workdir,
                env=subscription_env(),
                timeout=self.timeout,
                allow_empty=True,
            )
            image = _newest_png([workdir, _generated_images_dir()], started)
            if image is None:
                raise ProviderError(CLI, "codex exec finished but produced no PNG image")
            out_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(image, out_path)
        return out_path


def codex_text_argv(codex_bin: str, workdir: Path, schema_path: Path, prompt: str) -> list[str]:
    """The isolated, read-only ``codex exec`` command line for one structured text completion."""
    return [
        codex_bin,
        "exec",
        "--ephemeral",
        "--skip-git-repo-check",
        "--ignore-user-config",
        "--sandbox",
        "read-only",
        "-C",
        str(workdir),
        "--output-schema",
        str(schema_path),
        "-o",
        str(workdir / "last-message.txt"),
        prompt,
    ]


def _parse_text_message(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[1] if "\n" in text else text
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end < start:
        raise ProviderError(CLI, "codex did not return a JSON object")
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ProviderError(CLI, "codex returned output that is not JSON") from exc
    if not isinstance(payload, dict):
        raise ProviderError(CLI, "codex did not return a JSON object")
    return payload


class CodexCliTextProvider:
    """A :class:`~spectracut.providers.base.TextProvider` through the signed-in Codex CLI.

    Each call is ephemeral, read-only and in an empty scratch directory, with API
    keys removed so only the ChatGPT sign-in works.
    """

    name = "codex-text"

    def __init__(self, codex_bin: str | None = None, *, timeout: float = 600.0) -> None:
        self.codex_bin = codex_bin or resolve_codex_bin() or configured_codex_bin()
        self.timeout = timeout

    def complete(self, system: str, prompt: str, *, schema: dict[str, Any]) -> Completion:
        full = f"{system}\n\n{prompt}\n\nReply with only the JSON object that matches the schema."
        with tempfile.TemporaryDirectory(prefix="spectracut-codex-") as tmp:
            workdir = Path(tmp)
            schema_path = workdir / "schema.json"
            schema_path.write_text(json.dumps(schema), encoding="utf-8")
            run_cli(
                codex_text_argv(self.codex_bin, workdir, schema_path, full),
                cli=CLI,
                cwd=workdir,
                env=subscription_env(),
                timeout=self.timeout,
                allow_empty=True,
            )
            message = workdir / "last-message.txt"
            if not message.is_file():
                raise ProviderError(CLI, "codex exec finished but wrote no message")
            output = _parse_text_message(message.read_text(encoding="utf-8"))
        return Completion(
            output=output, usage={"codex_requests": 1, "codex_input_chars": len(full)}
        )


def codex_status(codex_bin: str | None = None) -> SignInState:
    """ChatGPT sign-in state from ``codex login status``. Never returns tokens."""
    binary = codex_bin or resolve_codex_bin()
    if binary is None:
        return SignInState(CLI, installed=False, signed_in=False, detail="codex not found")
    try:
        out = run_cli(
            [binary, "login", "status"], cli=CLI, timeout=30, allow_empty=True, include_stderr=True
        )
    except ProviderAuthError:
        return SignInState(CLI, installed=True, signed_in=False)
    except ProviderError as exc:
        if "not logged in" in exc.detail.lower():
            return SignInState(CLI, installed=True, signed_in=False)
        return SignInState(CLI, installed=True, signed_in=False, detail=exc.detail)
    for line in out.splitlines():
        text = line.strip()
        lowered = text.lower()
        if lowered.startswith("not logged in"):
            return SignInState(CLI, installed=True, signed_in=False)
        if lowered.startswith("logged in using "):
            if "chatgpt" in lowered:
                method = "ChatGPT"
            elif "api key" in lowered:
                method = "API key"
            else:
                method = "other"
            return SignInState(CLI, installed=True, signed_in=True, method=method)
    return SignInState(CLI, installed=True, signed_in=False, detail="unrecognised status output")
