# SPDX-License-Identifier: AGPL-3.0-or-later
"""Generative sound: local sound effects, foley and score with candidate selection.

Every cue's description becomes a prompt for a local model (``sfx``, ``foley`` or
``music``). Each cue gets ``candidates`` generations and the one with the best QA score
wins; all scores are recorded on its :class:`Selection`. A scene is generated in one
pass per model kind inside one ``provider.batch()``, so each model loads once.

Each chosen sound becomes a :class:`~spectracut.audio.recipe.SoundSpec` whose
provenance and params carry the model, its version and the prompt. Nothing here reaches
an external service: sounds come only from the local registry's ``get_provider``.
"""

from __future__ import annotations

import math
from array import array
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from spectracut.audio import render
from spectracut.audio.recipe import Provenance, SoundSpec, sound_key
from spectracut.providers.base import ProviderError
from spectracut.providers.local import foley, music, sfx
from spectracut.providers.local.registry import LocalProvider, get_provider
from spectracut.providers.local.runtime import ModelRuntime
from spectracut.qa.audio import AudioError, read_wav

Scorer = Callable[[Path], float]
"""Scores one candidate file; higher is better. Plug in CLAP or the listening critic here."""

KINDS = ("sfx", "foley", "music")
_PROMPTS = {"sfx": sfx.build_sfx_prompt, "foley": foley.build_foley_prompt, "music": music.build_music_prompt}
DEFAULT_CANDIDATES = 3


@dataclass(frozen=True)
class CueRequest:
    """One sound to generate: what the cue is, how long, and which model kind makes it."""

    cue_id: str
    kind: str
    description: str
    duration_s: float
    size: str = "medium"
    palette: str = ""
    """The Look's sonic palette, added to the prompt."""
    video_path: Path | None = None
    """Foley only: the rendered picture the sound is matched to."""
    stems: tuple[str, ...] = ()
    """Music only: one generation per stem (e.g. ``drums``); empty means one full mix."""
    seed: int = 0
    stem: str = ""
    """Set on the per-stem requests that ``stems`` expands into."""


@dataclass(frozen=True)
class GeneratedCandidate:
    index: int
    path: Path
    score: float


@dataclass(frozen=True)
class Selection:
    """The chosen candidate of one cue with every candidate's score and its provenance."""

    cue_id: str
    kind: str
    model: str
    version: str
    prompt: str
    candidates: tuple[GeneratedCandidate, ...]
    chosen: int
    """Index into ``candidates`` of the winner."""
    spec: SoundSpec = field(compare=False)

    @property
    def path(self) -> Path:
        return self.candidates[self.chosen].path

    @property
    def score(self) -> float:
        return self.candidates[self.chosen].score

    @property
    def key(self) -> str:
        return sound_key(self.spec)


def qa_score(path: Path) -> float:
    """Default scorer from audio QA metrics: 0 for unreadable or silent files.

    Penalises clipped samples and silence; among clean files, fuller ones score higher.
    """
    try:
        _, channels = read_wav(path)
    except (AudioError, OSError):
        return 0.0
    samples = [x for c in channels for x in c]
    if not samples:
        return 0.0
    peak = max(abs(x) for x in samples)
    if peak < 1e-4:
        return 0.0
    clipped = sum(1 for x in samples if abs(x) >= 0.999) / len(samples)
    quiet = sum(1 for x in samples if abs(x) < 0.01 * peak) / len(samples)
    rms = math.sqrt(sum(x * x for x in samples) / len(samples))
    return round((1.0 - clipped) * (1.0 - quiet) * min(1.0, rms / 0.1), 6)


def _expand(requests: Sequence[CueRequest]) -> list[CueRequest]:
    """Music stems become one request each (``<cue>-<stem>``); everything else is as given."""
    out: list[CueRequest] = []
    for r in requests:
        if r.kind not in KINDS:
            raise ProviderError("local", f"cue {r.cue_id!r}: unknown generative kind {r.kind!r}")
        if r.kind == "music" and r.stems:
            out += [_stem(r, s) for s in r.stems]
        else:
            out.append(r)
    return out


def _stem(r: CueRequest, stem: str) -> CueRequest:
    return replace(r, cue_id=f"{r.cue_id}-{stem}", stems=(), stem=stem)


def _prompt(r: CueRequest) -> str:
    kwargs: dict[str, Any] = {"size": r.size, "duration_s": r.duration_s, "palette": r.palette}
    if r.kind == "music":
        kwargs["stem"] = r.stem
    return _PROMPTS[r.kind](r.description, **kwargs)


def _options(r: CueRequest, seed: int) -> dict[str, Any]:
    options: dict[str, Any] = {"duration_s": r.duration_s, "seed": seed}
    if r.kind == "foley" and r.video_path is not None:
        options["video_path"] = r.video_path
    return options


def _identity(provider: LocalProvider) -> tuple[str, str]:
    spec = getattr(provider, "spec", None)
    if spec is not None:
        return spec.id, spec.version or "unpinned"
    return provider.name, "mock"


def generate_scene(
    requests: Sequence[CueRequest],
    out_dir: Path,
    *,
    runtime: ModelRuntime | None = None,
    config: Mapping[str, Any] | None = None,
    providers: Mapping[str, LocalProvider] | None = None,
    candidates: int = DEFAULT_CANDIDATES,
    parallel: int = 1,
    scorer: Scorer = qa_score,
) -> list[Selection]:
    """Generate every cue of a scene and pick each cue's best candidate.

    Requests are grouped by kind and each kind's provider is opened once, so its model
    loads once for the whole scene. ``parallel`` bounds concurrent generations inside
    that warm session (1 by default: a GPU session is not assumed thread-safe).
    Returns selections in request order (music stems expanded in place).
    """
    if candidates < 1:
        raise ValueError("candidates must be at least 1")
    if parallel < 1:
        raise ValueError("parallel must be at least 1")
    expanded = _expand(requests)
    ids = [r.cue_id for r in expanded]
    if len(set(ids)) != len(ids):
        raise ValueError("cue ids must be unique within a scene")
    out_dir = Path(out_dir)
    done: dict[str, Selection] = {}
    for kind in dict.fromkeys(r.kind for r in expanded):
        group = [r for r in expanded if r.kind == kind]
        provider = (providers or {}).get(kind) or get_provider(kind, runtime, config=config)
        model, version = _identity(provider)
        with provider.batch():
            jobs = [(r, i) for r in group for i in range(candidates)]

            def run(
                job: tuple[CueRequest, int], provider: LocalProvider = provider, kind: str = kind
            ) -> GeneratedCandidate:
                r, i = job
                path = out_dir / kind / f"{r.cue_id}.c{i}.wav"
                provider.generate(_prompt(r), path, **_options(r, r.seed + i))
                return GeneratedCandidate(i, path, float(scorer(path)))

            if parallel == 1:
                made = [run(j) for j in jobs]
            else:
                with ThreadPoolExecutor(max_workers=parallel) as pool:
                    made = list(pool.map(run, jobs))
        for n, r in enumerate(group):
            done[r.cue_id] = _select(r, made[n * candidates : (n + 1) * candidates], model, version)
    return [done[i] for i in ids]


def _select(r: CueRequest, made: list[GeneratedCandidate], model: str, version: str) -> Selection:
    chosen = max(range(len(made)), key=lambda i: (made[i].score, -i))
    prompt = _prompt(r)
    spec = SoundSpec(
        kind=r.kind,
        duration_seconds=r.duration_s,
        params={"prompt": prompt, "model": model, "model_version": version},
        seed=r.seed + chosen,
        provenance=Provenance(
            source="generated",
            origin=f"{model}@{version}",
            licence="model licence (commercial terms of the selected local model)",
            commercial=True,
        ),
    )
    return Selection(r.cue_id, r.kind, model, version, prompt, tuple(made), chosen, spec)


class GenerativeSoundGenerator:
    """A :class:`~spectracut.audio.render.SoundGenerator` that serves chosen candidates.

    Fits each chosen file to its spec: resampled, trimmed or padded to the exact length
    and mixed to the spec's channel count, so it goes through ``SoundCache`` like any sound.
    """

    def __init__(self, selections: Sequence[Selection]) -> None:
        self._by_key = {s.key: s for s in selections}

    def generate(self, spec: SoundSpec, sample_rate: int) -> render.Channels:
        selection = self._by_key.get(sound_key(spec))
        if selection is None:
            raise ProviderError("local", f"no generated sound for {spec.kind!r} {spec.params}")
        rate, channels = render.read_wav(selection.path)
        n = spec.samples(sample_rate)
        out: render.Channels = []
        for c in range(spec.channels):
            src = channels[min(c, len(channels) - 1)]
            out.append(array("f", (_at(src, i * rate / sample_rate) for i in range(n))))
        return out


def _at(src: Sequence[float], pos: float) -> float:
    """Linear-interpolated sample at fractional ``pos``; silence past the end."""
    i = int(pos)
    if i >= len(src):
        return 0.0
    frac = pos - i
    nxt = src[i + 1] if i + 1 < len(src) else src[i]
    return float(src[i] * (1.0 - frac) + nxt * frac)
