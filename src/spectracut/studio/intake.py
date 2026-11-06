# SPDX-License-Identifier: AGPL-3.0-or-later
"""Intake autofill: a vague idea plus sources becomes a CreativeBrief with an assumptions page.

The flow mirrors ForgeDock's bootstrap: each brief aspect is answered by the
idea or the sources, else it gets a default with a one-line rationale. Aspects
with real alternatives become *doubts*: multiple-choice questions with a
recommended option, asked in batches of at most :data:`MAX_DOUBTS`. Headless
mode takes every recommended option.

Proof points are sentences of fetched sources and cite that source's id.
Candidate claims without a known source are dropped and listed, never
invented. Brand forbidden words are enforced on every generated text.

Fetching lives here, not in a department (departments never touch the
network); ``fetch`` is injectable so tests stay offline.
"""

from __future__ import annotations

import re
import urllib.request
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from spectracut.spec._base import Slug
from spectracut.spec.brief import (
    Assumption,
    AudienceProfile,
    ClaimSource,
    CreativeBrief,
    Persona,
    ProofPoint,
)
from spectracut.studio.departments.discovery import IntakeNotes, intake_inputs
from spectracut.writer.copy_gate import _normalise, _word_pattern

MAX_DOUBTS = 4
MAX_PROOF_POINTS = 5
MAX_FETCH_BYTES = 400_000
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
_PLATFORMS = ("youtube", "tiktok", "linkedin", "instagram", "x")


class IntakeError(ValueError):
    """The idea and sources cannot make a valid brief."""


@dataclass(frozen=True)
class Aspect:
    """One brief aspect: a question, its options (the recommended one marked) and why."""

    id: str
    question: str
    options: tuple[str, ...]
    recommended: int
    rationale: str
    doubt: bool = False
    """Ask the user (non-headless) instead of silently defaulting."""

    @property
    def default(self) -> str:
        return self.options[self.recommended]


@dataclass(frozen=True)
class Source:
    id: str
    ref: str
    text: str


Ask = Callable[[Sequence[Aspect]], Sequence[int]]
"""Answers one batch of doubts with the chosen option index of each."""
Fetch = Callable[[str], str]


def _common(tone: str, cta: str) -> tuple[Aspect, ...]:
    return (
        Aspect(
            "audience",
            "Who is the film for?",
            ("Developers and technical buyers", "Executives", "General consumers"),
            0,
            "Product docs and READMEs speak to builders first.",
            doubt=True,
        ),
        Aspect(
            "tone",
            "What tone?",
            (tone, "Playful and bold", "Calm and premium"),
            0,
            "Clarity over atmosphere is the studio default.",
            doubt=True,
        ),
        Aspect(
            "success_measure",
            "How is success measured?",
            ("Signups from the film's call to action", "Views", "Demo requests"),
            0,
            "A single conversion is measurable and tied to the call to action.",
            doubt=True,
        ),
        Aspect(
            "cta",
            "What should viewers do next?",
            (cta, "Read the docs", "Book a demo"),
            0,
            "One clear next step ends the film.",
            doubt=True,
        ),
        Aspect(
            "platforms",
            "Where will it run?",
            ("youtube,linkedin", "tiktok", "youtube"),
            0,
            "Landscape launch film works on the two default channels.",
        ),
        Aspect(
            "offer", "Is there an offer?", ("none",), 0, "No offer was given, so none is invented."
        ),
    )


QUESTION_BANK: dict[str, tuple[Aspect, ...]] = {
    "launch": _common("Confident and clear", "Try it today"),
    "explainer": _common("Friendly and clear", "Learn more"),
}
"""Question bank per film type."""


def film_type(idea: str) -> str:
    return "launch" if "launch" in idea.lower() else "explainer"


def product_of(idea: str) -> str:
    """The product named by ``idea`` ("launch film for AlterLab" gives "AlterLab")."""
    match = re.search(r"\b(?:for|of|about)\s+(.+)$", idea.strip(), re.IGNORECASE)
    return (match.group(1) if match else idea).strip(" .") or idea.strip()


def batches(aspects: Sequence[Aspect], size: int = MAX_DOUBTS) -> list[list[Aspect]]:
    """Doubts in batches of at most ``size``."""
    return [list(aspects[i : i + size]) for i in range(0, len(aspects), size)]


def fetch_url(url: str) -> str:
    """Fetch an http(s) URL as text (bounded); the default :data:`Fetch`."""
    if not url.startswith(("http://", "https://")):
        raise IntakeError(f"unsupported source URL {url!r}")
    with urllib.request.urlopen(url, timeout=20) as resp:  # noqa: S310 - scheme checked above
        data: bytes = resp.read(MAX_FETCH_BYTES)
    return data.decode("utf-8", errors="replace")


def load_sources(refs: Iterable[str], fetch: Fetch = fetch_url) -> list[Source]:
    """Read each path or URL into a :class:`Source` with a unique slug id."""
    out: list[Source] = []
    for ref in refs:
        try:
            if ref.startswith(("http://", "https://")):
                text = fetch(ref)
            else:
                text = Path(ref).read_text(encoding="utf-8")
        except (OSError, ValueError) as exc:
            raise IntakeError(f"cannot read source {ref}: {exc}") from exc
        base = (
            re.sub(r"[^a-z0-9]+", "-", Path(ref.split("?")[0]).name.lower()).strip("-") or "source"
        )
        sid, n = base, 2
        while any(s.id == sid for s in out):
            sid, n = f"{base}-{n}", n + 1
        out.append(Source(sid, ref, text))
    return out


def _sentences(text: str) -> list[str]:
    in_fence = False
    lines: list[str] = []
    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
        elif not in_fence and not line.lstrip().startswith(("#", "|", "[", "!", "<")):
            lines.append(re.sub(r"^[\s>*+-]+", "", line))
    out = []
    for raw in _SENTENCE.split("\n".join(lines)):
        sentence = re.sub(r"[*_`]|\[([^\]]*)\]\([^)]*\)", r"\1", raw).strip()
        if 20 <= len(sentence) <= 200 and sentence[-1] in ".!?" and " " in sentence:
            out.append(sentence)
    return out


def _forbidden_in(text: str, words: Iterable[str]) -> str | None:
    for word in _normalise(words):
        if _word_pattern(word).search(text):
            return word
    return None


def resolve(
    idea: str, headless: bool = True, ask: Ask | None = None
) -> tuple[dict[str, str], list[Assumption]]:
    """Aspect values and the assumptions behind them.

    The idea answers ``product`` (and ``platforms`` when it names one). Other
    aspects take the user's answer to a doubt, or the recommended default,
    which is recorded as an assumption.
    """
    if not idea.strip():
        raise IntakeError("an idea is required")
    bank = QUESTION_BANK[film_type(idea)]
    values = {"product": product_of(idea)}
    named = [p for p in _PLATFORMS if re.search(rf"\b{p}\b", idea, re.IGNORECASE)]
    if named:
        values["platforms"] = ",".join(named)
    todo = [a for a in bank if a.id not in values]
    chosen: dict[str, int] = {}
    doubts = [a for a in todo if a.doubt]
    if not headless:
        if ask is None:
            raise IntakeError("non-headless intake needs an ask callback")
        for batch in batches(doubts):
            answers = ask(batch)
            if len(answers) != len(batch):
                raise IntakeError("ask must answer every doubt in the batch")
            for aspect, index in zip(batch, answers, strict=True):
                if not 0 <= index < len(aspect.options):
                    raise IntakeError(f"option {index} is out of range for {aspect.id!r}")
                chosen[aspect.id] = index
    assumptions: list[Assumption] = []
    for aspect in todo:
        index = chosen.get(aspect.id, aspect.recommended)
        values[aspect.id] = aspect.options[index]
        if aspect.id not in chosen:
            assumptions.append(
                Assumption(
                    aspect=aspect.id, value=aspect.options[index], rationale=aspect.rationale
                )
            )
    return values, assumptions


def build_brief(
    idea: str,
    sources: Sequence[Source],
    *,
    headless: bool = True,
    ask: Ask | None = None,
    forbidden_words: Iterable[str] = (),
    claims: Sequence[tuple[str, str | None]] = (),
) -> CreativeBrief:
    """A schema-valid CreativeBrief from ``idea`` and fetched ``sources``.

    ``claims`` are extra candidate ``(text, source_id)`` pairs; those without a
    known source are dropped. Source sentences that use a forbidden word are
    dropped too, and both are listed in ``dropped_claims``.
    """
    if not sources:
        raise IntakeError("at least one source is required (--source PATH|URL)")
    words = list(forbidden_words)
    values, assumptions = resolve(idea, headless=headless, ask=ask)
    answers, pages = intake_inputs(idea, values, [{"ref": s.ref, "text": s.text} for s in sources])
    notes = IntakeNotes.model_validate(answers)
    known = {s.id for s in sources}
    candidates = [(c, sid) for c, sid in claims]
    per_source = [[(c, s.id) for c in _sentences(s.text)] for s in sources]
    while any(per_source):  # round-robin so every source can contribute
        for lst in per_source:
            if lst:
                candidates.append(lst.pop(0))
    proofs: list[ProofPoint] = []
    dropped: list[str] = []
    for claim, sid in candidates:
        if sid is None or sid not in known:
            dropped.append(f"{claim} (no fetched source)")
        elif (hit := _forbidden_in(claim, words)) is not None:
            dropped.append(f"{claim} (forbidden word {hit!r})")
        elif len(proofs) < MAX_PROOF_POINTS and all(p.claim != claim for p in proofs):
            proofs.append(ProofPoint(claim=claim, source_id=sid))
    if not proofs:
        raise IntakeError("no sourced claim survived; supply a richer --source")
    message = f"{notes.product}: {proofs[0].claim}"
    texts = [message, notes.product, values["tone"], values["cta"], values["audience"]]
    for text in texts:
        if (hit := _forbidden_in(text, words)) is not None:
            raise IntakeError(f"generated text {text!r} uses forbidden word {hit!r}")
    slug: Slug = re.sub(r"[^a-z0-9]+", "-", f"{notes.product}-{film_type(idea)}".lower()).strip("-")
    return CreativeBrief(
        id=slug or "production",
        message=message,
        audience=AudienceProfile(
            personas=[Persona(name="Primary viewer", description=values["audience"])]
        ),
        sources=[ClaimSource(id=s.id, title=s.id, ref=s.ref) for s in sources],
        proof_points=proofs,
        tone=values["tone"],
        success_measure=values["success_measure"],
        offer="" if values["offer"] == "none" else values["offer"],
        cta=values["cta"],
        assumptions=assumptions,
        dropped_claims=dropped,
    )


def assumptions_markdown(brief: CreativeBrief) -> str:
    """The ``assumptions.md`` page: every default with its rationale, then dropped claims."""
    lines = [f"# Assumptions for {brief.id}", ""]
    lines += [f"- **{a.aspect}**: {a.value}. {a.rationale}" for a in brief.assumptions]
    if not brief.assumptions:
        lines.append("- none: every aspect was answered")
    lines += ["", "## Dropped claims", ""]
    lines += [f"- {c}" for c in brief.dropped_claims] or ["- none"]
    return "\n".join(lines) + "\n"
