# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reference comparison: does the work hit the bar the owner's reference films set?

A judge separate from the builder's verifier (:mod:`spectracut.critique.scene_verifier`):

- :func:`measure_storyboard` measures the shot plan (the storyboard's boards): average
  board length against the cited references' pace range (:func:`pace_range`) and type
  treatment (words per on-screen text card, at most :data:`MAX_CARD_WORDS`).
- :func:`compare_frames` asks a :class:`~spectracut.providers.base.ReferenceJudge` to
  score each frame against the keyframe of the primary cited reference on pace, type,
  UI legibility and look, and records a :class:`ReferenceComparison` (stored on the
  style frames and the cut). With no judge or no local keyframes it records no frame
  scores and a note; it never fails the node.
- :func:`assert_not_reference` refuses output media that is a reference file: the
  films are studied for craft only, never copied.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import Field

from spectracut.providers.base import ProviderError, ReferenceJudge
from spectracut.spec._base import StrictModel
from spectracut.spec.references import Reference, ReferenceSet, default_store

if TYPE_CHECKING:
    from spectracut.storyboard.models import Storyboard

PACE_TOLERANCE = 0.25
"""The cited references' average-shot range is widened by this fraction on each side."""

MAX_CARD_WORDS = 6
"""Type treatment: one idea per card (``benchmarks/references.md`` targets)."""

REFERENCE_RUBRIC = (
    "Does the frame hit the quality bar of the reference on pace (energy of the shot), "
    "type (kinetic, one idea per card, legible), UI legibility (real UI readable at full "
    "size) and look (palette rhythm, light and polish)? Study for craft only; the frame "
    "must not copy the reference's footage, frames, copy or marks."
)


class PaceMeasure(StrictModel):
    """Measured average shot length against the cited references' range."""

    avg_shot_s: float
    range_s: tuple[float, float]
    ok: bool
    score: float = Field(ge=0, le=1)


class StoryboardMeasure(StrictModel):
    """The shot plan measured against the references it cites."""

    cited: list[str]
    pace: PaceMeasure | None
    max_card_words: int
    problems: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


class FrameComparison(StrictModel):
    frame: str
    reference: str
    score: float = Field(ge=0, le=1)
    notes: str = ""


class ReferenceComparison(StrictModel):
    """A reference-comparison score recorded on a node (style frames, the cut)."""

    references: list[str] = Field(default_factory=list)
    """Ids of the cited references compared against."""
    score: float | None = Field(default=None, ge=0, le=1)
    """Mean of the frame scores and the pace score; ``None`` when nothing was measured."""
    pace: PaceMeasure | None = None
    frames: list[FrameComparison] = Field(default_factory=list)
    judge: str | None = None
    note: str = ""


def pace_range(
    refs: ReferenceSet, cited: Sequence[str], tolerance: float = PACE_TOLERANCE
) -> tuple[float, float] | None:
    """[min, max] average shot of the cited references, widened by ``tolerance``."""
    shots = [r.avg_shot_s for r in refs.cited(list(cited))]
    if not shots:
        return None
    return round(min(shots) * (1 - tolerance), 3), round(max(shots) * (1 + tolerance), 3)


def measure_pace(
    durations: Sequence[float], refs: ReferenceSet, cited: Sequence[str]
) -> PaceMeasure | None:
    """The average of ``durations`` against :func:`pace_range` (``None`` if unmeasurable)."""
    span = pace_range(refs, cited)
    if span is None or not durations:
        return None
    avg = round(sum(durations) / len(durations), 3)
    lo, hi = span
    if lo <= avg <= hi:
        score = 1.0
    else:
        bound = lo if avg < lo else hi
        score = max(0.0, 1.0 - abs(avg - bound) / bound)
    return PaceMeasure(avg_shot_s=avg, range_s=span, ok=lo <= avg <= hi, score=round(score, 3))


def _words(text: str) -> int:
    return len(text.split())


def measure_storyboard(storyboard: Storyboard, refs: ReferenceSet) -> StoryboardMeasure:
    """Average board length and type treatment against the references the board cites."""
    cited = list(storyboard.craft_refs)
    pace = measure_pace([b.duration_s for b in storyboard.boards], refs, cited)
    words = max((_words(b.text) for b in storyboard.boards), default=0)
    problems = []
    if pace is not None and not pace.ok:
        lo, hi = pace.range_s
        problems.append(
            f"average shot {pace.avg_shot_s} s is outside the cited references' range "
            f"{lo}-{hi} s"
        )
    long_cards = [
        f"{b.shot_id}={b.text!r}" for b in storyboard.boards if _words(b.text) > MAX_CARD_WORDS
    ]
    if long_cards:
        problems.append(
            f"on-screen text over {MAX_CARD_WORDS} words (one idea per card): "
            + ", ".join(long_cards)
        )
    return StoryboardMeasure(cited=cited, pace=pace, max_card_words=words, problems=problems)


def primary_reference(refs: ReferenceSet, cited: Sequence[str]) -> Reference | None:
    """The cited reference with the most citations (ties: set order)."""
    counts: dict[str, int] = {}
    for c in cited:
        if refs.point(c) is not None:
            rid = c.split(":", 1)[0]
            counts[rid] = counts.get(rid, 0) + 1
    best: Reference | None = None
    for ref in refs.references:
        if ref.id in counts and (best is None or counts[ref.id] > counts[best.id]):
            best = ref
    return best


def _rubric(ref: Reference) -> str:
    points = "; ".join(f"{p.label}: {p.text}" for p in ref.craft)
    return f"{REFERENCE_RUBRIC}\nReference {ref.id} craft points: {points}"


def compare_frames(
    frames: Sequence[Path],
    refs: ReferenceSet,
    cited: Sequence[str],
    judge: object | None,
    *,
    pace: PaceMeasure | None = None,
) -> ReferenceComparison:
    """Score ``frames`` against the primary cited reference's keyframe; mean with ``pace``."""
    cited_refs = [r.id for r in refs.cited(list(cited))]
    primary = primary_reference(refs, cited)
    notes: list[str] = []
    scored: list[FrameComparison] = []
    name = getattr(judge, "name", None) if judge is not None else None
    if primary is None:
        notes.append("no reference craft points cited")
    elif not primary.keyframes:
        notes.append(f"no local keyframes for {primary.id} (reference store not on this host)")
    elif not isinstance(judge, ReferenceJudge):
        notes.append("no reference judge configured")
    elif not frames:
        notes.append("no frames to compare")
    else:
        keyframe = Path(primary.keyframes[0])
        rubric = _rubric(primary)
        for frame in frames:
            try:
                verdict = judge.compare(Path(frame), keyframe, rubric)
            except ProviderError as exc:
                notes.append(f"{Path(frame).name}: judge failed: {exc}")
                continue
            scored.append(
                FrameComparison(
                    frame=str(frame),
                    reference=primary.id,
                    score=min(1.0, max(0.0, verdict.score)),
                    notes=verdict.notes,
                )
            )
    parts = [f.score for f in scored] + ([pace.score] if pace is not None else [])
    score = round(sum(parts) / len(parts), 3) if parts else None
    return ReferenceComparison(
        references=cited_refs,
        score=score,
        pace=pace,
        frames=scored,
        judge=name if scored else None,
        note="; ".join(notes),
    )


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def assert_not_reference(
    path: Path | str, refs: ReferenceSet | None, store: Path | None = None
) -> None:
    """Raise :class:`ValueError` when ``path`` is inside the reference store or a keyframe copy."""
    out = Path(path)
    root = (Path(store) if store is not None else default_store()).resolve()
    resolved = out.resolve()
    if resolved == root or resolved.is_relative_to(root):
        raise ValueError(
            f"output {out} is inside the reference store {root}; never copy references"
        )
    if refs is None or not out.is_file():
        return
    keyframes = [Path(k) for r in refs.references for k in r.keyframes if Path(k).is_file()]
    if not keyframes:
        return
    digest = _sha256(out)
    for k in keyframes:
        if k.stat().st_size == out.stat().st_size and _sha256(k) == digest:
            raise ValueError(
                f"output {out} is a copy of reference keyframe {k}; never copy references"
            )
