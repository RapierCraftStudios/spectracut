# SPDX-License-Identifier: AGPL-3.0-or-later
"""Voice-over: local temp VO before the script locks, ElevenLabs final VO after.

- :meth:`VoiceOver.temp` voices lines with the local TTS model; it never reaches ElevenLabs.
- :meth:`VoiceOver.final` refuses to run until the script is locked, then calls ElevenLabs
  once per line. Each line is cached on disk by the content hash of its text, voice and
  settings, so editing one line regenerates only that line.
- The characters sent to ElevenLabs are returned as a :class:`~spectracut.studio.ledger.Usage`
  so the department run records them in the cost ledger. Cache hits cost nothing.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from spectracut.providers.elevenlabs import VoiceProvider
from spectracut.providers.local.tts import LocalTts
from spectracut.spec._base import Slug, StrictModel, content_hash
from spectracut.studio.ledger import Usage


class ScriptNotLockedError(RuntimeError):
    """Final VO was requested before the script locked."""


class VoLine(StrictModel):
    id: Slug
    text: str


class VoiceoverLine(StrictModel):
    id: Slug
    beat: str
    text: str = Field(min_length=1)
    media_ref: str = Field(min_length=1)
    duration_s: float = Field(gt=0.0)


class VoiceoverTrack(StrictModel):
    """Every voiced line of the script; ``temp_vo`` when made by the local model."""

    lines: list[VoiceoverLine] = Field(default_factory=list)
    provider: Literal["elevenlabs", "local"]
    temp_vo: bool
    mark: Literal["temp-vo"] | None = None


def line_key(text: str, voice: str, settings: Mapping[str, Any]) -> str:
    """Cache key of one line: ``sha256:<hex>`` of its text, voice and settings."""
    return content_hash({"text": text, "voice": voice, "settings": dict(settings)})


@dataclass
class FinalVo:
    paths: dict[str, Path] = field(default_factory=dict)
    regenerated: list[str] = field(default_factory=list)
    """Ids of the lines sent to ElevenLabs this call (cache misses)."""
    usage: Usage = field(default_factory=Usage)


class VoiceOver:
    def __init__(
        self,
        cache_dir: Path,
        *,
        voice: str,
        settings: Mapping[str, Any] | None = None,
        local: LocalTts | None = None,
        final_provider: VoiceProvider | None = None,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.voice = voice
        self.settings = dict(settings or {})
        self._local = local
        self._final = final_provider

    def temp(self, lines: Sequence[VoLine]) -> dict[str, Path]:
        """Temp VO for boards and animatics, generated locally."""
        local = self._local or LocalTts()
        out: dict[str, Path] = {}
        with local.batch():
            for line in lines:
                out[line.id] = local.synthesize(
                    line.text, self.cache_dir / "temp" / f"{line.id}.wav", voice=self.voice
                )
        return out

    def final_path(self, line: VoLine, extension: str) -> Path:
        digest = line_key(line.text, self.voice, self.settings).split(":", 1)[-1]
        return self.cache_dir / "final" / f"{digest}.{extension}"

    def final(self, lines: Sequence[VoLine], *, script_locked: bool) -> FinalVo:
        """Final VO from ElevenLabs, once per line; cached lines are free."""
        if not script_locked:
            raise ScriptNotLockedError("final VO needs a locked script; use temp() before lock")
        if self._final is None:
            from spectracut.providers.elevenlabs import get_voice_provider

            self._final = get_voice_provider()
        result = FinalVo()
        chars = 0
        for line in lines:
            path = self.final_path(line, self._final.extension)
            if not path.is_file():
                tmp = path.with_name(path.name + ".part")
                self._final.synthesize(line.text, self.voice, self.settings, tmp)
                tmp.replace(path)
                chars += len(line.text)
                result.regenerated.append(line.id)
            result.paths[line.id] = path
        result.usage = Usage(elevenlabs_characters=chars)
        return result
