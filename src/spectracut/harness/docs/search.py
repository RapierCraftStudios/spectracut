# SPDX-License-Identifier: AGPL-3.0-or-later
"""A stdlib ranked search over the bpy API and manual docs indexes.

An index is a JSON list of entries ``{path, title, text, signature?, see?}``.
Hits score by query-term overlap, with extra weight for terms found in the
path or title; ties break by path, so results are deterministic.

``settable_via`` is attached here, in the control plane, for every API path
that maps to a ``render.set_property`` allowlist key
(:data:`spectracut.harness.tools.scene.RenderProperty`). It names the typed
tool and arguments that set it; every other path has none, since agents act
only through typed tools.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from functools import cache
from importlib import resources
from pathlib import Path
from typing import Any, Literal, get_args

from spectracut.harness.errors import HarnessError

DocsKind = Literal["api", "manual"]

BPY_DOCS_ENV = "SPECTRACUT_BPY_DOCS"
"""Path to a full API index (same schema) that replaces the API seed."""

SET_PROPERTY_TOOL = "render.set_property"

RNA_TO_PROPERTY: dict[str, str] = {
    "bpy.types.RenderSettings.film_transparent": "render.film_transparent",
    "bpy.types.RenderSettings.fps": "render.fps",
    "bpy.types.RenderSettings.use_motion_blur": "render.use_motion_blur",
    "bpy.types.RenderSettings.motion_blur_shutter": "render.motion_blur_shutter",
    "bpy.types.CyclesRenderSettings.samples": "cycles.samples",
    "bpy.types.SceneEEVEE.taa_render_samples": "eevee.taa_render_samples",
    "bpy.types.ColorManagedViewSettings.view_transform": "view_settings.view_transform",
    "bpy.types.ColorManagedViewSettings.look": "view_settings.look",
    "bpy.types.ColorManagedViewSettings.exposure": "view_settings.exposure",
    "bpy.types.ColorManagedViewSettings.gamma": "view_settings.gamma",
}
"""RNA path -> ``render.set_property`` key, one row per allowlisted property."""

_SEED_FILES: dict[DocsKind, str] = {"api": "bpy_api.json", "manual": "manual.json"}
_TOKEN = re.compile(r"[a-z0-9]+")
_FIELDS = ("path", "title", "text", "signature", "see")


def allowlisted_properties() -> tuple[str, ...]:
    """The ``render.set_property`` allowlist, from the control-plane tool model."""
    from spectracut.harness.tools.scene import RenderProperty

    return tuple(get_args(RenderProperty))


def settable_via(path: str) -> dict[str, Any] | None:
    """The typed tool call that sets ``path``, or None when no tool sets it."""
    key = RNA_TO_PROPERTY.get(path)
    if key is None or key not in allowlisted_properties():
        return None
    return {"tool": SET_PROPERTY_TOOL, "args": {"property": key}}


def tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def _entries(raw: Any, source: str) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise HarnessError("config.invalid", f"{source}: docs index must be a JSON list")
    out: list[dict[str, Any]] = []
    for n, item in enumerate(raw):
        ok = isinstance(item, dict) and all(
            isinstance(item.get(k), str) and item[k] for k in ("path", "title")
        )
        if not ok:
            raise HarnessError("config.invalid", f"{source}: entry {n} needs string path and title")
        entry = {k: item[k] for k in _FIELDS if isinstance(item.get(k), str | list)}
        entry.setdefault("text", "")
        out.append(entry)
    return out


@cache
def _seed(kind: DocsKind) -> tuple[dict[str, Any], ...]:
    text = resources.files(__package__).joinpath(_SEED_FILES[kind]).read_text("utf-8")
    return tuple(_entries(json.loads(text), _SEED_FILES[kind]))


def _load_file(path: str) -> list[dict[str, Any]]:
    try:
        raw = json.loads(Path(path).read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise HarnessError(
            "config.invalid", f"{BPY_DOCS_ENV}: cannot read docs index: {exc}"
        ) from None
    return _entries(raw, BPY_DOCS_ENV)


def load_index(kind: DocsKind, env: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """The entries of one index, with ``settable_via`` attached to API entries."""
    env = os.environ if env is None else env
    override = env.get(BPY_DOCS_ENV) if kind == "api" else None
    entries = _load_file(override) if override else [dict(e) for e in _seed(kind)]
    for entry in entries:
        entry.pop("settable_via", None)
        if kind == "api":
            via = settable_via(entry["path"])
            if via is not None:
                entry["settable_via"] = via
    return entries


def _score(entry: Mapping[str, Any], terms: list[str]) -> float:
    path = set(tokens(entry["path"]))
    title = set(tokens(entry["title"]))
    body = set(tokens(" ".join(str(entry.get(k, "")) for k in ("text", "signature"))))
    score = 0.0
    for term in terms:
        if term in path:
            score += 3.0
        if term in title:
            score += 2.0
        if term in body:
            score += 1.0
    matched = sum(1 for t in terms if t in path | title | body)
    return score * matched / len(terms)


def search(
    query: str, kind: DocsKind, limit: int = 5, env: Mapping[str, str] | None = None
) -> list[dict[str, Any]]:
    """The best ``limit`` entries of an index for ``query``, best first."""
    terms = list(dict.fromkeys(tokens(query)))
    if not terms:
        raise HarnessError("args.invalid", "docs search: query has no words")
    scored = [(_score(e, terms), e) for e in load_index(kind, env)]
    ranked = sorted((p for p in scored if p[0] > 0), key=lambda p: (-p[0], p[1]["path"]))
    return [{**e, "score": round(s, 3)} for s, e in ranked[:limit]]
