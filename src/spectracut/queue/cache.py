# SPDX-License-Identifier: AGPL-3.0-or-later
"""Content-addressed frame cache, beside Flamenco.

Entries are keyed by :func:`~spectracut.queue.profile.frame_cache_key`, i.e. the
recipe hash *and* the render profile, so frames rendered on different machines
are never mixed. A job whose key is cached completes without rendering.

Layout: ``<root>/<hex[:2]>/<hex>/manifest.json``, written atomically. An
unreadable or corrupt manifest is a miss, never an error.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from pydantic import Field, JsonValue

from spectracut.queue.executors.base import JobResult
from spectracut.queue.profile import RenderProfile, frame_cache_key
from spectracut.spec._base import ContentHash, StrictModel, canonical_json

MANIFEST = "manifest.json"


class CacheEntry(StrictModel):
    """What the cache keeps for one recipe rendered on one profile."""

    key: ContentHash
    recipe_hash: ContentHash
    render_profile: RenderProfile
    output: JsonValue = None
    frames: list[str] = Field(default_factory=list)

    def to_result(self) -> JobResult:
        return JobResult(
            output=self.output, frames=list(self.frames), render_profile=self.render_profile
        )


class FrameCache:
    """Directory-backed, content-addressed cache of rendered frames."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    @staticmethod
    def key(recipe_hash: str, profile: RenderProfile) -> str:
        return frame_cache_key(recipe_hash, profile)

    def _path(self, key: str) -> Path:
        hexdigest = key.removeprefix("sha256:")
        return self.root / hexdigest[:2] / hexdigest / MANIFEST

    def get(self, recipe_hash: str, profile: RenderProfile) -> CacheEntry | None:
        key = self.key(recipe_hash, profile)
        try:
            entry = CacheEntry.model_validate_json(self._path(key).read_bytes())
        except (OSError, ValueError):
            return None
        return entry if entry.key == key else None

    def put(self, recipe_hash: str, profile: RenderProfile, result: JobResult) -> CacheEntry:
        key = self.key(recipe_hash, profile)
        entry = CacheEntry(
            key=key,
            recipe_hash=recipe_hash,
            render_profile=profile,
            output=result.output,
            frames=list(result.frames),
        )
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".manifest-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(canonical_json(entry))
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return entry

    def __contains__(self, item: object) -> bool:
        if not (isinstance(item, tuple) and len(item) == 2):
            return False
        recipe_hash, profile = item
        if not isinstance(recipe_hash, str) or not isinstance(profile, RenderProfile):
            return False
        return self.get(recipe_hash, profile) is not None
