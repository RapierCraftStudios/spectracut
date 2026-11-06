# SPDX-License-Identifier: AGPL-3.0-or-later
"""Learned audio quality predictors: scores per stem and mix that flag, never fail.

Audiobox Aesthetics (CC-BY 4.0) scores any stem; UTMOS scores speech. Both run
locally and are imported lazily, so CI and installs without them use
:class:`MockAudioPredictor`. Known biases (a predictor can rate low-bitrate audio
too highly) are why a low score only *flags*: :func:`check_predictors` never
returns a ``fail`` row, so a predictor alone cannot fail a mix.
"""

from __future__ import annotations

import hashlib
import importlib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from spectracut.providers.base import ProviderError
from spectracut.qa.common import QACheck

FLAG_BELOW = 0.4
"""Scores (0..1) under this are flagged as weak."""


class AudioPredictor(Protocol):
    """Scores one WAV in 0..1 (higher is better); ``kind`` is the stem name or ``mix``."""

    name: str

    def score(self, path: Path, kind: str) -> float: ...


class MockAudioPredictor:
    """Deterministic score from the file bytes, or a fixed score per ``kind`` (for CI)."""

    name = "mock-audio"

    def __init__(self, overrides: Mapping[str, float] | None = None) -> None:
        self.overrides = dict(overrides or {})

    def score(self, path: Path, kind: str) -> float:
        if kind in self.overrides:
            return self.overrides[kind]
        digest = hashlib.sha256(Path(path).read_bytes()).digest()
        return round(int.from_bytes(digest[:2], "big") / 0xFFFF, 3)


class AudioboxAesthetics:
    """Meta Audiobox Aesthetics: the mean of its four axes (0..10) scaled to 0..1."""

    name = "audiobox-aesthetics"

    def __init__(self) -> None:
        try:
            infer = importlib.import_module("audiobox_aesthetics.infer")
        except ImportError:
            raise ProviderError(self.name, "audiobox_aesthetics is not installed") from None
        self._predictor: Any = infer.initialize_predictor()

    def score(self, path: Path, kind: str) -> float:
        axes = self._predictor.forward([{"path": str(path)}])[0]
        return float(sum(axes.values())) / (10.0 * len(axes))


class UTMOS:
    """UTMOS speech MOS (1..5) scaled to 0..1; for the dx stem."""

    name = "utmos"

    def __init__(self) -> None:
        try:
            import torch

            self._torch: Any = torch
            self._model: Any = torch.hub.load(
                "tarepan/SpeechMOS:v1.2.0", "utmos22_strong", trust_repo=True
            )
        except Exception as exc:
            raise ProviderError(self.name, f"UTMOS is unavailable: {exc}") from None

    def score(self, path: Path, kind: str) -> float:
        import soundfile

        wave, rate = soundfile.read(str(path), dtype="float32", always_2d=True)
        mos = float(self._model(self._torch.from_numpy(wave.mean(axis=1))[None], rate)[0])
        return min(1.0, max(0.0, (mos - 1.0) / 4.0))


def default_predictors() -> dict[str, list[AudioPredictor]]:
    """UTMOS and Audiobox for VO, Audiobox for everything else."""
    audiobox = AudioboxAesthetics()
    return {"dx": [UTMOS(), audiobox], "mx": [audiobox], "fx": [audiobox], "mix": [audiobox]}


def check_predictors(
    stems: Mapping[str, Path],
    predictors: Mapping[str, Sequence[AudioPredictor]],
    threshold: float = FLAG_BELOW,
) -> list[QACheck]:
    """One row per stem and predictor; a score under ``threshold`` is flagged in the detail.

    The status is ``pass`` (scored, flagged or not) or ``skipped`` (the predictor
    errored); never ``fail``.
    """
    rows: list[QACheck] = []
    for kind, path in stems.items():
        for predictor in predictors.get(kind, ()):
            name = f"audio.predictor.{kind}"
            try:
                score = predictor.score(path, kind)
            except (ProviderError, OSError, ValueError) as exc:
                rows.append(QACheck(name=name, status="skipped", detail=f"{predictor.name}: {exc}"))
                continue
            flag = " FLAG: weak audio" if score < threshold else ""
            rows.append(
                QACheck(
                    name=name,
                    status="pass",
                    value=round(score, 3),
                    limit=threshold,
                    detail=f"{predictor.name}{flag}",
                )
            )
    return rows
