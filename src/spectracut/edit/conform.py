# SPDX-License-Identifier: AGPL-3.0-or-later
"""Conform: apply a shot change through every level of a nested edit.

Edits are frozen, so conforming returns a new :class:`Edit`. Scene, sequence and
episode durations are derived from their clips, so they follow the changed shot.
"""

from __future__ import annotations

from typing import Any

from spectracut.edit.timeline import Clip, Edit, Track

ShotPath = tuple[str, ...]


def find_shot(edit: Edit, shot_id: str) -> list[ShotPath]:
    """Paths (clip ids from the outermost edit down) of every clip showing ``shot_id``."""
    found: list[ShotPath] = []
    for track in edit.tracks:
        for clip in track.clips():
            if clip.shot_id == shot_id:
                found.append((clip.id,))
            if clip.nested is not None:
                found.extend((clip.id, *path) for path in find_shot(clip.nested, shot_id))
    return found


def _rebuilt(model: Any, **updates: Any) -> Any:
    """A validated copy of ``model`` with ``updates`` applied."""
    fields = {name: getattr(model, name) for name in type(model).model_fields}
    return type(model).model_validate({**fields, **updates})


def _conform(edit: Edit, shot_id: str, updates: dict[str, Any]) -> tuple[Edit, int]:
    hits = 0
    tracks: list[Track] = []
    for track in edit.tracks:
        items: list[Any] = []
        for item in track.items:
            if isinstance(item, Clip):
                changes: dict[str, Any] = {}
                if item.nested is not None:
                    nested, n = _conform(item.nested, shot_id, updates)
                    if n:
                        hits += n
                        changes["nested"] = nested
                if item.shot_id == shot_id:
                    hits += 1
                    changes.update(updates)
                if changes:
                    item = _rebuilt(item, **changes)
            items.append(item)
        tracks.append(_rebuilt(track, items=items))
    return _rebuilt(edit, tracks=tracks), hits


def replace_shot(
    edit: Edit,
    shot_id: str,
    *,
    source_in: int | None = None,
    source_out: int | None = None,
    shot_hash: str | None = None,
    media_ref: str | None = None,
) -> Edit:
    """Return ``edit`` with every clip of ``shot_id``, at any depth, updated.

    Only the arguments given are changed. Raises ``KeyError`` if the shot is not used.
    """
    given = {
        "source_in": source_in,
        "source_out": source_out,
        "shot_hash": shot_hash,
        "media_ref": media_ref,
    }
    updates = {k: v for k, v in given.items() if v is not None}
    conformed, hits = _conform(edit, shot_id, updates)
    if not hits:
        raise KeyError(f"shot {shot_id!r} is not in edit {edit.id!r}")
    return conformed
