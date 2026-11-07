# SPDX-License-Identifier: AGPL-3.0-or-later
"""Optional API-key fallback (``OPENAI_API_KEY``, ``ANTHROPIC_API_KEY``).

Used only when the subscription CLI is not available. Keys are read from the
environment at call time, sent only in the request header, and never stored,
logged or included in errors.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from spectracut.providers.base import ProviderAuthError, ProviderError, VisionScore, redact
from spectracut.providers.claude_code import JUDGE_SYSTEM_PROMPT, _parse_json_text

OPENAI_IMAGES_URL = "https://api.openai.com/v1/images/generations"
ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"


class _Response(Protocol):
    def read(self) -> bytes: ...

    def __enter__(self) -> _Response: ...

    def __exit__(self, *args: object) -> None: ...


Opener = Callable[..., _Response]


def _key(env_var: str, service: str) -> str:
    value = os.environ.get(env_var, "").strip()
    if not value:
        raise ProviderAuthError(service, f"{env_var} is not set")
    return value


def _post_json(
    opener: Opener,
    url: str,
    body: dict[str, Any],
    headers: dict[str, str],
    *,
    service: str,
    env_var: str,
    timeout: float,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json", **headers},
        method="POST",
    )
    try:
        with opener(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = redact(exc.read().decode("utf-8", "replace")[:300])
        if exc.code in (401, 403):
            raise ProviderAuthError(service, f"{env_var} was rejected (HTTP {exc.code})") from None
        raise ProviderError(service, f"HTTP {exc.code}: {detail}") from None
    except urllib.error.URLError as exc:
        raise ProviderError(service, f"request failed: {redact(str(exc.reason))}") from None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProviderError(service, "response is not JSON") from exc
    if not isinstance(data, dict):
        raise ProviderError(service, "unexpected response")
    return data


class OpenAIKeyImageProvider:
    """Image generation with ``OPENAI_API_KEY`` (fallback for the Codex sign-in)."""

    name = "openai-api-key"

    def __init__(
        self, *, model: str = "gpt-image-1", opener: Opener | None = None, timeout: float = 300
    ) -> None:
        self.model = model
        self.opener: Opener = opener or urllib.request.urlopen
        self.timeout = timeout

    def generate(self, prompt: str, out_path: Path, *, size: str = "1536x1024") -> Path:
        key = _key("OPENAI_API_KEY", "openai")
        data = _post_json(
            self.opener,
            OPENAI_IMAGES_URL,
            {"model": self.model, "prompt": prompt, "size": size, "n": 1},
            {"authorization": f"Bearer {key}"},
            service="openai",
            env_var="OPENAI_API_KEY",
            timeout=self.timeout,
        )
        items = data.get("data")
        b64 = items[0].get("b64_json") if isinstance(items, list) and items else None
        if not isinstance(b64, str) or not b64:
            raise ProviderError("openai", "response contained no image")
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(base64.b64decode(b64))
        return out_path


_MEDIA_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}


class AnthropicKeyVisionJudge:
    """Vision judging with ``ANTHROPIC_API_KEY`` (fallback for the Claude sign-in)."""

    name = "anthropic-api-key"

    def __init__(
        self,
        *,
        model: str = "claude-sonnet-5-5",
        opener: Opener | None = None,
        timeout: float = 120,
    ) -> None:
        self.model = model
        self.opener: Opener = opener or urllib.request.urlopen
        self.timeout = timeout
        self.last_usage: dict[str, Any] = {}

    def score(self, frame_path: Path, rubric: str) -> VisionScore:
        return self._judge([Path(frame_path)], f"Rubric: {rubric}")

    def compare(self, frame_path: Path, reference_path: Path, rubric: str) -> VisionScore:
        """Score the first image against the bar the second (a reference keyframe) sets."""
        return self._judge(
            [Path(frame_path), Path(reference_path)],
            "The first image is a frame from our film; the second is a reference keyframe "
            "that sets the quality bar. Score how well the frame meets that bar on the rubric "
            f"(craft, not similarity of content).\nRubric: {rubric}",
        )

    def _judge(self, frames: list[Path], text: str) -> VisionScore:
        self.last_usage = {}
        for frame_path in frames:
            if not frame_path.is_file():
                raise ProviderError("anthropic", f"frame not found: {frame_path}")
        key = _key("ANTHROPIC_API_KEY", "anthropic")
        content: list[dict[str, Any]] = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": _MEDIA_TYPES.get(f.suffix.lower(), "image/png"),
                    "data": base64.b64encode(f.read_bytes()).decode(),
                },
            }
            for f in frames
        ]
        content.append(
            {
                "type": "text",
                "text": f'{text}\nAnswer only with JSON: {{"score": <0..1>, "notes": "<short>"}}',
            }
        )
        body = {
            "model": self.model,
            "max_tokens": 400,
            "system": JUDGE_SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": content}],
        }
        data = _post_json(
            self.opener,
            ANTHROPIC_MESSAGES_URL,
            body,
            {"x-api-key": key, "anthropic-version": "2023-06-01"},
            service="anthropic",
            env_var="ANTHROPIC_API_KEY",
            timeout=self.timeout,
        )
        usage = data.get("usage")
        self.last_usage = dict(usage) if isinstance(usage, dict) else {}
        blocks = data.get("content")
        text = "".join(
            str(b.get("text", ""))
            for b in (blocks if isinstance(blocks, list) else [])
            if isinstance(b, dict) and b.get("type") == "text"
        )
        try:
            payload = _parse_json_text(text)
        except (ValueError, json.JSONDecodeError) as exc:
            raise ProviderError("anthropic", "response did not contain a score") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("score"), int | float):
            raise ProviderError("anthropic", "response did not contain a numeric score")
        return VisionScore(
            score=min(1.0, max(0.0, float(payload["score"]))), notes=str(payload.get("notes", ""))
        )
