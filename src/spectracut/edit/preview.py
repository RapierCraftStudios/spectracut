# SPDX-License-Identifier: AGPL-3.0-or-later
"""A playable preview (mp4) of an :class:`~spectracut.edit.timeline.Edit` for review.

:class:`FfmpegPreview` encodes the animatic in one ffmpeg run: the picture track's
board images as a concat-demuxer list (each held for its clip's duration), scaled
and padded to 1280x720 at the edit's frame rate, with every audio-track clip delayed
to its record start, trimmed to its duration and mixed. ffmpeg runs from ``PATH``,
else in the pinned audio image through docker (the same probe as
:class:`~spectracut.audio.render.ArdourRunner`). No Python dependency is added.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from spectracut.audio.render import AUDIO_IMAGE
from spectracut.edit.timeline import Clip, Edit, Track

AnimaticPreview = Callable[[Edit, Path], Path]
"""Writes a playable preview of ``edit`` to ``out`` and returns ``out``."""

WIDTH, HEIGHT = 1280, 720
SAMPLE_RATE = 48_000


class PreviewError(RuntimeError):
    """The preview could not be encoded (no ffmpeg, missing media or ffmpeg failed)."""


def _seconds(frames: int, fps: Fraction) -> str:
    return f"{float(Fraction(frames) / fps):.6f}"


def _picture(edit: Edit) -> Track:
    for track in edit.tracks:
        if track.id == "picture":
            return track
    for track in edit.tracks:
        if track.kind == "video":
            return track
    raise PreviewError(f"edit {edit.id!r} has no picture track")


def _media(clip: Clip) -> Path:
    path = Path(clip.media_ref)
    if not path.is_file():
        raise PreviewError(f"clip {clip.id!r}: media {clip.media_ref!r} is not a file")
    return path.resolve()


def _quote(path: Path) -> str:
    return "'" + str(path).replace("'", "'\\''") + "'"


@dataclass
class FfmpegPreview:
    """Encodes an edit's picture and audio tracks to an H.264/AAC mp4 with ffmpeg.

    In the image the output dir and every media dir are mounted at the same path and
    ffmpeg runs as the calling user, so paths match and no root-owned files remain.
    """

    image: str = AUDIO_IMAGE
    binary: str | None = None
    timeout: float = 600.0

    def command(self) -> list[str] | None:
        """The base command, or ``None`` when no ffmpeg is available."""
        if self.binary:
            return [self.binary]
        found = shutil.which("ffmpeg")
        if found:
            return [found]
        docker = shutil.which("docker")
        if docker is None:
            return None
        probe = subprocess.run(
            [docker, "image", "inspect", self.image],
            capture_output=True,
            check=False,
            timeout=60,
        )
        return [docker] if probe.returncode == 0 else None

    def arguments(self, edit: Edit, out: Path) -> tuple[list[str], list[Path]]:
        """ffmpeg's arguments for ``edit`` and the media files they read.

        Writes the concat list next to ``out``. Raises :class:`PreviewError` when a
        clip's media is not an existing file.
        """
        fps = edit.rate.fps
        picture = _picture(edit)
        if not picture.clips() or any(not isinstance(i, Clip) for i in picture.items):
            raise PreviewError(f"picture track {picture.id!r} must be clips with no gaps")
        images = [(_media(c), c.duration) for c in picture.clips()]
        sounds = [
            (_media(c), c, track.record_start(c.id))
            for track in edit.tracks
            if track.kind == "audio"
            for c in track.clips()
        ]
        out.parent.mkdir(parents=True, exist_ok=True)
        listing = out.with_name(f"{out.stem}.concat.txt")
        lines = ["ffconcat version 1.0"]
        for path, frames in images:
            lines += [f"file {_quote(path)}", f"duration {_seconds(frames, fps)}"]
        lines.append(f"file {_quote(images[-1][0])}")
        listing.write_text("\n".join(lines) + "\n", encoding="utf-8")
        rate = f"{fps.numerator}/{fps.denominator}"
        args = ["-y", "-hide_banner", "-loglevel", "error"]
        args += ["-f", "concat", "-safe", "0", "-i", str(listing)]
        args += ["-f", "lavfi", "-i", f"anullsrc=r={SAMPLE_RATE}:cl=stereo"]
        for path, _clip, _start in sounds:
            args += ["-i", str(path)]
        graph = [
            f"[0:v]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
            f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2,fps={rate},format=yuv420p[v]"
        ]
        mixed = ["[1:a]"]
        for n, (_path, clip, start) in enumerate(sounds, start=2):
            delay = round(float(Fraction(start) / fps) * 1000)
            graph.append(
                f"[{n}:a]atrim=start={_seconds(clip.source_in, fps)}"
                f":duration={_seconds(clip.duration, fps)},asetpts=PTS-STARTPTS,"
                f"aresample={SAMPLE_RATE},adelay={delay}:all=1[a{n}]"
            )
            mixed.append(f"[a{n}]")
        graph.append(f"{''.join(mixed)}amix=inputs={len(mixed)}:duration=longest:normalize=0[a]")
        args += ["-filter_complex", ";".join(graph), "-map", "[v]", "-map", "[a]"]
        args += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", rate]
        args += ["-c:a", "aac", "-b:a", "192k", "-ar", str(SAMPLE_RATE)]
        args += ["-t", f"{float(edit.seconds):.6f}", "-movflags", "+faststart", str(out)]
        return args, [listing, *(p for p, _ in images), *(p for p, _c, _s in sounds)]

    def __call__(self, edit: Edit, out: Path) -> Path:
        out = Path(out).absolute()
        args, files = self.arguments(edit, out)
        base = self.command()
        if base is None:
            raise PreviewError(f"no ffmpeg on PATH and no image {self.image}")
        if Path(base[0]).name == "docker":
            dirs = sorted({str(out.parent), *(str(f.parent) for f in files)})
            mounts = [arg for d in dirs for arg in ("-v", f"{d}:{d}")]
            argv = [
                base[0], "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}",
                "-e", "HOME=/tmp", *mounts, self.image, "ffmpeg", *args,
            ]  # fmt: skip
        else:
            argv = [*base, *args]
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, check=False, timeout=self.timeout
            )
        except subprocess.TimeoutExpired as exc:
            raise PreviewError(f"ffmpeg timed out after {self.timeout} s") from exc
        if proc.returncode != 0 or not out.is_file():
            log = proc.stdout + proc.stderr
            tail = "\n".join(line for line in log.splitlines() if line.strip())[-2000:]
            raise PreviewError(f"ffmpeg failed (exit {proc.returncode}) for {out}:\n{tail}")
        return out
