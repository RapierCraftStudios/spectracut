# SPDX-License-Identifier: AGPL-3.0-or-later
"""The owner's reference films as a production input: craft notes and local keyframes.

A :class:`ReferenceSet` is what the studio's departments study: per reference film,
its measured pace (average shot length, cuts) and its craft points (pace, type, UI
treatment, palette rhythm, ending, ...) parsed from ``benchmarks/references.md``,
plus the paths of its keyframes in the owner's local store
(``~/.spectracut/references/keyframes/<id>.png``) when they are there.

The films are studied for craft only. The videos are never read, copied or
committed; keyframes are referenced by path and seen only by the reference judge
(:mod:`spectracut.critique.references`).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Literal

from pydantic import Field

from spectracut.spec._base import NonEmptyStr, StrictModel

DEFAULT_REFERENCE_SET = "references-v1"
"""The reference set every studio production studies unless told otherwise."""

BENCHMARKS_DIR_ENV = "SPECTRACUT_BENCHMARKS_DIR"
"""Same variable as :mod:`spectracut.critique.benchmarks` uses for its directory."""

REFERENCES_STORE_ENV = "SPECTRACUT_REFERENCES"
"""Overrides the owner's local reference store (default ``~/.spectracut/references``)."""

CraftAspect = Literal["pace", "type", "ui", "palette", "ending", "other"]


class ReferenceError(ValueError):
    """The reference set or its craft notes cannot be read."""


class CraftPoint(StrictModel):
    """One craft point of a reference film, cited by id (``<reference>:<slug>``)."""

    id: NonEmptyStr
    aspect: CraftAspect
    label: NonEmptyStr
    text: NonEmptyStr


class Reference(StrictModel):
    """One reference film: its measured pace, its craft points and local keyframes."""

    id: NonEmptyStr
    title: str = ""
    avg_shot_s: float = Field(gt=0)
    cuts: int = Field(ge=0)
    craft: list[CraftPoint] = Field(default_factory=list)
    keyframes: list[str] = Field(default_factory=list)
    """Absolute paths of local keyframes; empty when the store is not on this host."""


class ReferenceSet(StrictModel):
    """The reference films a production studies for craft (never copies)."""

    id: NonEmptyStr
    references: list[Reference] = Field(min_length=1)

    def point(self, point_id: str) -> CraftPoint | None:
        """The craft point ``point_id``, or ``None`` when the set has none by that id."""
        for ref in self.references:
            for point in ref.craft:
                if point.id == point_id:
                    return point
        return None

    def point_ids(self) -> set[str]:
        return {p.id for ref in self.references for p in ref.craft}

    def unknown(self, cited: list[str]) -> list[str]:
        """The ids in ``cited`` that are not craft points of this set."""
        known = self.point_ids()
        return [c for c in cited if c not in known]

    def cited(self, cited: list[str]) -> list[Reference]:
        """The references (in set order) that at least one id in ``cited`` belongs to."""
        refs = {c.split(":", 1)[0] for c in cited if c in self.point_ids()}
        return [r for r in self.references if r.id in refs]


def default_benchmarks_dir() -> Path:
    """``$SPECTRACUT_BENCHMARKS_DIR``, else ``./benchmarks``, else the source tree's."""
    env = os.environ.get(BENCHMARKS_DIR_ENV)
    if env:
        return Path(env)
    here = Path("benchmarks")
    if (here / "references.md").is_file():
        return here
    return Path(__file__).resolve().parents[3] / "benchmarks"


def default_store() -> Path:
    """The owner's local reference store (``$SPECTRACUT_REFERENCES`` overrides it)."""
    env = os.environ.get(REFERENCES_STORE_ENV)
    return Path(env) if env else Path.home() / ".spectracut" / "references"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-")


def _aspect(label: str) -> CraftAspect:
    low = label.casefold()
    if "ending" in low:
        return "ending"
    if "pace" in low or "shot" in low or "cut" in low:
        return "pace"
    if re.search(r"\bui\b", low):
        return "ui"
    if "type" in low or "tagline" in low or "overlay" in low:
        return "type"
    if any(w in low for w in ("light and dark", "gradient", "look", "palette", "colour")):
        return "palette"
    return "other"


_ROW = re.compile(r"^\|\s*`(?P<id>[^`]+)`\s*\|(?P<rest>.*)\|\s*$")
_SECTION = re.compile(r"^###\s+`(?P<id>[^`]+)`\s*:?\s*(?P<title>.*)$")
_BULLET = re.compile(r"^-\s+\*\*(?P<label>[^*]+?):?\*\*:?\s*(?P<text>.*)$")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def _table(markdown: str) -> dict[str, tuple[float, int]]:
    """Reference id -> (average shot seconds, cuts) from the measurements table."""
    out: dict[str, tuple[float, int]] = {}
    for line in markdown.splitlines():
        m = _ROW.match(line.strip())
        if m is None:
            continue
        cells = [c.strip() for c in m["rest"].split("|")]
        # Source | Format | Length | Cuts | Avg shot | Loudness | SHA
        if len(cells) < 5:
            continue
        cuts = _NUMBER.search(cells[3])
        avg = _NUMBER.search(cells[4])
        if cuts is None or avg is None:
            continue
        out[m["id"]] = (float(avg.group()), int(float(cuts.group())))
    return out


def _sections(markdown: str) -> dict[str, tuple[str, list[CraftPoint]]]:
    """Reference id -> (title, craft points) from the ``### `id`: title`` sections."""
    out: dict[str, tuple[str, list[CraftPoint]]] = {}
    current: str | None = None
    points: list[tuple[str, str]] = []

    def close() -> None:
        if current is None:
            return
        title, _ = out[current]
        seen: dict[str, int] = {}
        craft = []
        for label, text in points:
            slug = _slug(label) or "point"
            seen[slug] = seen.get(slug, 0) + 1
            if seen[slug] > 1:
                slug = f"{slug}-{seen[slug]}"
            craft.append(
                CraftPoint(id=f"{current}:{slug}", aspect=_aspect(label), label=label, text=text)
            )
        out[current] = (title, craft)

    for raw in markdown.splitlines():
        line = raw.rstrip()
        if line.startswith("#"):
            close()
            points = []
            m = _SECTION.match(line)
            current = m["id"] if m else None
            if current is not None:
                out[current] = ((m["title"] if m else "").strip(), [])
            continue
        if current is None:
            continue
        b = _BULLET.match(line.strip())
        if b is not None:
            points.append((b["label"].strip(), b["text"].strip()))
        elif line.startswith("  ") and line.strip() and points:
            label, text = points[-1]
            points[-1] = (label, f"{text} {line.strip()}")
    close()
    return out


def parse_references(markdown: str) -> dict[str, Reference]:
    """Every reference in ``references.md`` that has both a measurement row and a section."""
    table = _table(markdown)
    sections = _sections(markdown)
    refs: dict[str, Reference] = {}
    for rid, (avg, cuts) in table.items():
        title, craft = sections.get(rid, ("", []))
        refs[rid] = Reference(id=rid, title=title, avg_shot_s=avg, cuts=cuts, craft=craft)
    return refs


def load_reference_set(
    set_id: str = DEFAULT_REFERENCE_SET,
    *,
    root: Path | None = None,
    store: Path | None = None,
) -> ReferenceSet:
    """The set ``set_id``: its pieces' craft notes and the keyframes found in ``store``.

    ``root`` is the benchmarks directory (``sets/<set_id>/set.json`` lists the pieces,
    ``references.md`` holds their notes). Keyframes are ``store/keyframes/<id>.png``,
    listed only when the file exists; the videos are never touched.
    """
    base = Path(root) if root is not None else default_benchmarks_dir()
    keyframes = (Path(store) if store is not None else default_store()) / "keyframes"
    set_path = base / "sets" / set_id / "set.json"
    notes_path = base / "references.md"
    try:
        pieces = json.loads(set_path.read_text(encoding="utf-8"))["pieces"]
        markdown = notes_path.read_text(encoding="utf-8")
    except (OSError, KeyError, ValueError) as exc:
        raise ReferenceError(f"cannot read reference set {set_id!r} under {base}: {exc}") from exc
    parsed = parse_references(markdown)
    refs: list[Reference] = []
    for piece in pieces:
        rid = str(piece["id"])
        ref = parsed.get(rid)
        if ref is None:
            raise ReferenceError(
                f"reference {rid!r} of set {set_id!r} has no notes in {notes_path}"
            )
        frame = keyframes / f"{rid}.png"
        found = [str(frame.resolve())] if frame.is_file() else []
        refs.append(ref.model_copy(update={"keyframes": found}))
    return ReferenceSet(id=set_id, references=refs)
