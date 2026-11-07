# SPDX-License-Identifier: AGPL-3.0-or-later
"""The harness error taxonomy: every failure an MCP client sees has a stable dotted code.

Codes from the bpy runtime (``ToolError.code``, printed by the runner as ``[code]``)
pass through unchanged. The harness adds its own codes for render hosts, timeouts and
framing checks. The set is closed: an unknown code is a programming error.
"""

from __future__ import annotations

import re
from typing import Any

RUNTIME_CODES: frozenset[str] = frozenset(
    {
        "args.invalid",
        "tool.unknown",
        "scene.no_camera",
        "scene.object_not_found",
        "asset.path_refused",
        "framing.subject_out_of_frame",
        "font.missing",
        "runtime.error",
    }
)
"""Codes raised inside Blender by the bpy runtime (``host/bpy_runtime``)."""

HARNESS_CODES: frozenset[str] = frozenset(
    {
        "framing.subject_too_small",
        "render.timeout",
        "render.failed",
        "host.refused",
        "host.unavailable",
        "recipe.empty",
        "config.invalid",
    }
)
"""Codes raised by the control-plane harness itself."""

ERROR_CODES: frozenset[str] = RUNTIME_CODES | HARNESS_CODES

_CODE_IN_OUTPUT = re.compile(r"\[([a-z]+(?:\.[a-z_]+)+)\]")


class HarnessError(Exception):
    """A tool call failed; ``code`` is one of :data:`ERROR_CODES`."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"unknown harness error code {code!r}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def to_payload(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            error["details"] = self.details
        return {"error": error}


def code_from_host_output(text: str, default: str = "render.failed") -> str:
    """Return the last known ``[code]`` in a host's output, or ``default``."""
    found = [c for c in _CODE_IN_OUTPUT.findall(text or "") if c in ERROR_CODES]
    return found[-1] if found else default
