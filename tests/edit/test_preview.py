# SPDX-License-Identifier: AGPL-3.0-or-later
"""The animatic's playable preview (#276): ffmpeg encodes boards and VO to an mp4."""

from __future__ import annotations

import json
import shutil
import struct
import subprocess
import wave
import zlib
from pathlib import Path

import pytest

from spectracut.edit.preview import FfmpegPreview, PreviewError
from spectracut.edit.timeline import Edit
from spectracut.storyboard.animatic import TempAudio, build_animatic
from spectracut.storyboard.models import Storyboard


def _png(path: Path, rgb: tuple[int, int, int], size: tuple[int, int] = (64, 36)) -> Path:
    width, height = size
    row = b"\x00" + bytes(rgb) * width
    raw = zlib.compress(row * height)

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", raw) + chunk(b"IEND", b"")
    )
    return path


def _wav(path: Path, seconds: float, rate: int = 48_000) -> Path:
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(b"\x10\x00" * int(seconds * rate))
    return path


def _edit(tmp: Path) -> tuple[Edit, Storyboard]:
    boards = Storyboard.model_validate(
        {
            "boards": [
                {
                    "shot_id": "a",
                    "beat": "x",
                    "action": "a",
                    "duration_s": 1.0,
                    "image": str(_png(tmp / "a.png", (200, 0, 0))),
                },
                {
                    "shot_id": "b",
                    "beat": "x",
                    "action": "b",
                    "duration_s": 0.5,
                    "image": str(_png(tmp / "b.png", (0, 200, 0))),
                },
                {
                    "shot_id": "c",
                    "beat": "y",
                    "action": "c",
                    "duration_s": 1.0,
                    "image": str(_png(tmp / "c.png", (0, 0, 200))),
                },
            ]
        }
    )
    vo = {
        "x": TempAudio(str(_wav(tmp / "x.wav", 2.0)), 2.0),
        "y": TempAudio(str(_wav(tmp / "y.wav", 0.5)), 0.5),
    }
    return build_animatic("anim", boards, vo, fps=24), boards


def test_ffmpeg_arguments_hold_each_board_for_its_clip_and_delay_each_vo(tmp_path: Path) -> None:
    edit, _ = _edit(tmp_path)
    out = tmp_path / "out" / "anim.mp4"
    args, files = FfmpegPreview(binary="ffmpeg").arguments(edit, out)
    listing = (tmp_path / "out" / "anim.concat.txt").read_text(encoding="utf-8").splitlines()
    assert listing[0] == "ffconcat version 1.0"
    # a holds 1 s; b (planned 0.5 s) holds 1 s so beat x's 2 s line is not cut; c 1 s.
    durations = [float(line.split()[1]) for line in listing if line.startswith("duration")]
    assert durations == pytest.approx([1.0, 1.0, 1.0])
    assert listing[-1] == f"file '{(tmp_path / 'c.png').resolve()}'"
    graph = args[args.index("-filter_complex") + 1]
    assert "scale=1280:720" in graph and "fps=24/1" in graph and "format=yuv420p" in graph
    assert "adelay=0:all=1" in graph and "adelay=2000:all=1" in graph
    assert "amix=inputs=3:duration=longest:normalize=0" in graph
    assert args[args.index("-t") + 1] == "3.000000"
    assert "+faststart" in args and args[-1] == str(out)
    assert (tmp_path / "x.wav").resolve() in files and (tmp_path / "a.png").resolve() in files


def test_ffmpeg_preview_refuses_a_clip_whose_media_is_missing(tmp_path: Path) -> None:
    edit, _ = _edit(tmp_path)
    (tmp_path / "y.wav").unlink()
    with pytest.raises(PreviewError, match="y.wav"):
        FfmpegPreview(binary="ffmpeg")(edit, tmp_path / "anim.mp4")


def test_no_ffmpeg_and_no_image_is_a_preview_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    edit, _ = _edit(tmp_path)
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    preview = FfmpegPreview()
    assert preview.command() is None
    with pytest.raises(PreviewError, match="no ffmpeg on PATH"):
        preview(edit, tmp_path / "anim.mp4")


def test_ffmpeg_failure_raises_with_the_log_tail(tmp_path: Path) -> None:
    edit, _ = _edit(tmp_path)
    fake = tmp_path / "ffmpeg"
    fake.write_text("#!/bin/sh\necho 'boom: bad filter' >&2\nexit 3\n", encoding="utf-8")
    fake.chmod(0o755)
    with pytest.raises(PreviewError, match=r"exit 3\).*\n.*boom: bad filter"):
        FfmpegPreview(binary=str(fake))(edit, tmp_path / "anim.mp4")


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not on PATH")
def test_ffmpeg_encodes_a_playable_mp4_of_the_animatic(tmp_path: Path) -> None:
    edit, _ = _edit(tmp_path)
    out = FfmpegPreview()(edit, tmp_path / "review" / "anim.mp4")
    assert out.is_file() and out.stat().st_size > 0
    if shutil.which("ffprobe") is None:
        return
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(out)],
        capture_output=True,
        text=True,
        check=True,
    )
    info = json.loads(probe.stdout)
    kinds = {s["codec_type"]: s for s in info["streams"]}
    assert kinds["video"]["codec_name"] == "h264"
    assert (kinds["video"]["width"], kinds["video"]["height"]) == (1280, 720)
    assert kinds["audio"]["codec_name"] == "aac"
    assert float(info["format"]["duration"]) == pytest.approx(3.0, abs=0.1)
