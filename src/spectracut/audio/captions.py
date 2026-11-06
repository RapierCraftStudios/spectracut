# SPDX-License-Identifier: AGPL-3.0-or-later
"""Captions aligned to VO with the local Whisper (``asr``) model, snapped to edit frames.

The ASR provider writes JSON ``{"words": [{"word", "start", "end"}]}`` (seconds). Times are
converted with exact :class:`fractions.Fraction` maths and rounded to the nearest frame of
the edit's :class:`~spectracut.edit.timeline.FrameRate`, so every caption boundary is
within half a frame of the spoken time.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from pydantic import Field

from spectracut.edit.timeline import FrameRate
from spectracut.providers.base import ProviderError
from spectracut.providers.local.registry import LocalProvider, get_provider
from spectracut.providers.local.runtime import ModelRuntime
from spectracut.spec._base import StrictModel


@dataclass(frozen=True)
class Word:
    text: str
    start_s: Fraction
    end_s: Fraction


class Caption(StrictModel):
    text: str
    start_frame: int = Field(ge=0)
    end_frame: int = Field(ge=1)
    """Exclusive; always after ``start_frame``."""


def transcribe(
    audio: Path,
    out_path: Path,
    *,
    provider: LocalProvider | None = None,
    runtime: ModelRuntime | None = None,
) -> list[Word]:
    """Word timings of ``audio`` from the local ASR model."""
    provider = provider if provider is not None else get_provider("asr", runtime)
    provider.generate(str(audio), Path(out_path), word_timestamps=True)
    try:
        data = json.loads(Path(out_path).read_text(encoding="utf-8"))
        return [
            Word(
                text=str(w["word"]).strip(),
                start_s=Fraction(str(w["start"])),
                end_s=Fraction(str(w["end"])),
            )
            for w in data["words"]
        ]
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise ProviderError("asr", f"no word timings in {out_path}: {exc}") from exc


def captions_from_words(
    words: Sequence[Word], rate: FrameRate, *, max_words: int = 6, offset_frames: int = 0
) -> list[Caption]:
    """Group words into captions of up to ``max_words`` and snap them to ``rate`` frames."""
    if max_words < 1:
        raise ValueError("max_words must be at least 1")
    out: list[Caption] = []
    for i in range(0, len(words), max_words):
        group = words[i : i + max_words]
        start = rate.frames(group[0].start_s) + offset_frames
        end = max(rate.frames(group[-1].end_s) + offset_frames, start + 1)
        out.append(
            Caption(
                text=" ".join(w.text for w in group if w.text), start_frame=start, end_frame=end
            )
        )
    return out


def align_captions(
    audio: Path,
    rate: FrameRate,
    work_dir: Path,
    *,
    provider: LocalProvider | None = None,
    runtime: ModelRuntime | None = None,
    max_words: int = 6,
    offset_frames: int = 0,
) -> list[Caption]:
    """Transcribe ``audio`` locally and return frame-accurate captions."""
    words = transcribe(
        audio, Path(work_dir) / f"{Path(audio).stem}.words.json", provider=provider, runtime=runtime
    )
    return captions_from_words(words, rate, max_words=max_words, offset_frames=offset_frames)
