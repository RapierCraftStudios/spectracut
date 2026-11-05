# SPDX-License-Identifier: AGPL-3.0-or-later
"""Render an :class:`~spectracut.audio.recipe.AudioRecipe` to a master and DX/MX/FX stems.

:func:`render_mix` does the whole job:

1. places every cue on the edit (exact sample positions);
2. makes the sounds through a :class:`SoundCache`. A sound is generated only when no
   sound with the same :func:`~spectracut.audio.recipe.sound_key` is cached, so changing
   one cue regenerates that cue's sounds and nothing else;
3. writes the Ardour session (:mod:`spectracut.audio.ardour`) and swaps the real sounds
   in;
4. reloads it in a fresh Ardour process, checks routing and automation, and exports the
   master plus one stem per bus.

Ardour runs through :class:`ArdourRunner`. That is ``ardour9-lua`` or ``ardour8-lua`` on
``PATH``, or else the pinned audio image ``spectracut-audio:current``
(src/spectracut/setup/manifest.yaml, component ``audio-image``).

WAV I/O uses only the standard library. Cached sounds and swapped sources are 32-bit
float; exports are 24-bit PCM.
"""

from __future__ import annotations

import math
import os
import random
import shutil
import struct
import subprocess
import sys
from array import array
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from spectracut.audio.ardour import (
    AUTOMATION_OK,
    BUILD_OK,
    EXPORT_OK,
    ROUTING_OK,
    build_lua,
    parse_sources,
    reload_export_lua,
    swap_sources,
    track_name,
    write_export_preset,
)
from spectracut.audio.recipe import (
    BUS_NAMES,
    AudioRecipe,
    BusName,
    SoundSpec,
    place_cues,
    sound_key,
)
from spectracut.edit.timeline import Edit

AUDIO_IMAGE = "spectracut-audio:current"
Channels = list[array[float]]

# --------------------------------------------------------------------------- WAV I/O


def write_wav(
    path: Path, channels: Sequence[Sequence[float]], sample_rate: int, *, float_format: bool
) -> None:
    """Write interleaved ``channels`` as 32-bit float (``float_format``) or 24-bit PCM."""
    if not channels:
        raise ValueError("no channels")
    n = len(channels[0])
    if any(len(c) != n for c in channels):
        raise ValueError("channels differ in length")
    nch = len(channels)
    if float_format:
        inter = array("f", [0.0]) * (n * nch)
        for c, chan in enumerate(channels):
            inter[c::nch] = array("f", chan)
        if sys.byteorder != "little":  # pragma: no cover - big-endian hosts
            inter.byteswap()
        data = inter.tobytes()
        fmt, width = 3, 4
    else:
        out = bytearray(n * nch * 3)
        i = 0
        for s in range(n):
            for c in range(nch):
                v = max(-8388608, min(8388607, round(channels[c][s] * 8388608.0)))
                out[i : i + 3] = (v & 0xFFFFFF).to_bytes(3, "little")
                i += 3
        data = bytes(out)
        fmt, width = 1, 3
    header = struct.pack(
        "<HHIIHH", fmt, nch, sample_rate, sample_rate * nch * width, nch * width, width * 8
    )
    riff = b"WAVE" + b"fmt " + struct.pack("<I", len(header)) + header
    riff += b"data" + struct.pack("<I", len(data)) + data
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"RIFF" + struct.pack("<I", len(riff)) + riff)


def read_wav(path: Path) -> tuple[int, Channels]:
    """Read a PCM (16/24/32-bit) or 32-bit float WAV as float channels in [-1, 1]."""
    raw = path.read_bytes()
    if raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise ValueError(f"{path} is not a RIFF/WAVE file")
    pos, fmt, data = 12, None, None
    while pos + 8 <= len(raw):
        cid, size = raw[pos : pos + 4], struct.unpack("<I", raw[pos + 4 : pos + 8])[0]
        body = raw[pos + 8 : pos + 8 + size]
        if cid == b"fmt ":
            fmt = struct.unpack("<HHIIHH", body[:16])
        elif cid == b"data":
            data = body
        pos += 8 + size + (size & 1)
    if fmt is None or data is None:
        raise ValueError(f"{path}: missing fmt or data chunk")
    tag, nch, rate, _, _, bits = fmt
    if tag == 0xFFFE:  # WAVE_FORMAT_EXTENSIBLE: the real tag leads the sub-format GUID
        tag = 3 if bits == 32 and b"\x03\x00" == _subformat(raw) else 1
    width = bits // 8
    frames = len(data) // (width * nch)
    flat = array("f")
    if tag == 3 and bits == 32:
        flat.frombytes(data[: frames * nch * 4])
        if sys.byteorder != "little":  # pragma: no cover
            flat.byteswap()
    elif tag == 1 and bits in (16, 24, 32):
        scale = float(1 << (bits - 1))
        flat = array(
            "f",
            (
                int.from_bytes(data[i : i + width], "little", signed=True) / scale
                for i in range(0, frames * nch * width, width)
            ),
        )
    else:
        raise ValueError(f"{path}: unsupported WAV format tag {tag} with {bits} bits")
    return rate, [flat[c::nch] for c in range(nch)]


def _subformat(raw: bytes) -> bytes:
    i = raw.find(b"fmt ")
    return raw[i + 8 + 24 : i + 8 + 26]


# ---------------------------------------------------------------------------- sounds


class SoundGenerator(Protocol):
    """Makes the audio of a :class:`SoundSpec` (procedural, generative or library)."""

    def generate(self, spec: SoundSpec, sample_rate: int) -> Channels: ...


class ToneGenerator:
    """Deterministic built-in generator for ``kind`` ``tone`` (sine) and ``noise``.

    Params: ``freq`` (Hz, tone only, default 440), ``level`` (peak, default 0.5),
    ``attack_s`` (default 0) and ``release_s`` (default 0.01). Noise is seeded by the
    spec's ``seed``. Real sound design plugs in through :class:`SoundGenerator`.
    """

    def generate(self, spec: SoundSpec, sample_rate: int) -> Channels:
        n = spec.samples(sample_rate)
        level = float(_param(spec, "level", 0.5))
        attack = round(float(_param(spec, "attack_s", 0.0)) * sample_rate)
        release = round(float(_param(spec, "release_s", 0.01)) * sample_rate)
        if spec.kind == "tone":
            w = 2.0 * math.pi * float(_param(spec, "freq", 440.0)) / sample_rate
            raw = [math.sin(w * i) for i in range(n)]
        elif spec.kind == "noise":
            rng = random.Random(spec.seed)
            raw = [rng.uniform(-1.0, 1.0) for _ in range(n)]
        else:
            raise ValueError(f"ToneGenerator cannot make sound kind {spec.kind!r}")
        out = array("f", [0.0]) * n
        for i, v in enumerate(raw):
            env = 1.0
            if attack and i < attack:
                env = i / attack
            if release and i >= n - release:
                env = min(env, (n - 1 - i) / release)
            out[i] = level * env * v
        return [out] if spec.channels == 1 else [out, array("f", out)]


def _param(spec: SoundSpec, name: str, default: float) -> float:
    value = spec.params.get(name, default)
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise ValueError(f"sound param {name!r} must be a number")
    return float(value)


class SoundCache:
    """Sounds on disk keyed by :func:`sound_key`; generates only missing keys."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.generated: list[str] = []
        """Keys generated by this cache instance, in order."""

    def path(self, spec: SoundSpec) -> Path:
        return self.root / (sound_key(spec).removeprefix("sha256:") + ".wav")

    def ensure(self, spec: SoundSpec, generator: SoundGenerator, sample_rate: int) -> Path:
        path = self.path(spec)
        if path.is_file():
            rate, chans = read_wav(path)
            if rate == sample_rate and len(chans[0]) == spec.samples(sample_rate):
                return path
        audio = generator.generate(spec, sample_rate)
        if len(audio) != spec.channels or any(len(c) != spec.samples(sample_rate) for c in audio):
            raise ValueError(f"generator returned the wrong shape for {spec.kind!r}")
        tmp = path.with_suffix(".tmp")
        write_wav(tmp, audio, sample_rate, float_format=True)
        tmp.replace(path)
        self.generated.append(sound_key(spec))
        return path

    def load(self, spec: SoundSpec, generator: SoundGenerator, sample_rate: int) -> Channels:
        return read_wav(self.ensure(spec, generator, sample_rate))[1]


# ---------------------------------------------------------------------------- Ardour


class ArdourError(RuntimeError):
    """An Ardour program failed or did not print its success marker."""


@dataclass
class ArdourRunner:
    """Runs a Lua program with ``ardourN-lua``: on PATH, or in the pinned audio image.

    In the image the work dir is mounted at the same path and the program runs as the
    calling user (``HOME=/tmp``), so session paths match and no root-owned files remain.
    """

    image: str = AUDIO_IMAGE
    binary: str | None = None
    docker_args: Sequence[str] = ()
    timeout: float = 600.0

    def command(self) -> list[str] | None:
        """The base command, or ``None`` when no Ardour is available."""
        if self.binary:
            return [self.binary]
        for name in ("ardour9-lua", "ardour8-lua"):
            found = shutil.which(name)
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

    def available(self) -> bool:
        return self.command() is not None

    def run(self, script: Path, workdir: Path, xdg_config_home: Path, marker: str) -> str:
        base = self.command()
        if base is None:
            raise ArdourError(f"no ardour9-lua/ardour8-lua on PATH and no image {self.image}")
        env = dict(os.environ)
        if Path(base[0]).name == "docker":
            work = str(workdir.resolve())
            argv = [
                base[0], "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}",
                "-e", "HOME=/tmp", "-e", f"XDG_CONFIG_HOME={xdg_config_home.resolve()}",
                "-v", f"{work}:{work}", *self.docker_args, self.image,
                "ardour8-lua", str(script.resolve()),
            ]  # fmt: skip
        else:
            env["XDG_CONFIG_HOME"] = str(xdg_config_home.resolve())
            argv = [*base, str(script.resolve())]
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                env=env,
                check=False,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise ArdourError(f"{script.name} timed out after {self.timeout} s") from exc
        log = proc.stdout + proc.stderr
        (script.with_suffix(".log")).write_text(log, encoding="utf-8")
        if proc.returncode != 0 or marker not in log:
            tail = "\n".join(line for line in log.splitlines() if line.strip())[-2000:]
            raise ArdourError(f"{script.name} failed (exit {proc.returncode}):\n{tail}")
        return log


# ------------------------------------------------------------------------------ mix


@dataclass
class MixResult:
    master: Path
    stems: dict[BusName, Path]
    session_dir: Path
    placements: dict[str, int]
    generated: list[str] = field(default_factory=list)
    """Sound keys generated for this render (empty when every sound was cached)."""
    log: str = ""


def render_mix(
    recipe: AudioRecipe,
    edit: Edit,
    workdir: Path,
    *,
    generator: SoundGenerator | None = None,
    runner: ArdourRunner | None = None,
    cache: SoundCache | None = None,
) -> MixResult:
    """Render ``recipe`` on ``edit`` to ``workdir/out/{master,dx,mx,fx}.wav``.

    The session (``workdir/session``) and the mix are rebuilt on every call; sounds come
    from ``cache`` (default ``workdir/sounds``) and are generated only when missing.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    generator = generator or ToneGenerator()
    runner = runner or ArdourRunner()
    cache = cache or SoundCache(workdir / "sounds")
    first = len(cache.generated)

    placements = place_cues(recipe, edit)
    audio: dict[str, Channels] = {}
    for cue in recipe.cues:
        for layer in cue.layers:
            audio[track_name(cue, layer)] = cache.load(layer.sound, generator, recipe.sample_rate)

    session_dir = workdir / "session"
    export_dir = workdir / "export"
    xdg = workdir / "xdg"
    for stale in (session_dir, export_dir):
        if stale.exists():
            shutil.rmtree(stale)
    write_export_preset(xdg)

    build = workdir / "build.lua"
    build.write_text(build_lua(recipe, placements, session_dir), encoding="utf-8")
    log = runner.run(build, workdir, xdg, BUILD_OK)
    swap_sources(session_dir, parse_sources(log), audio, recipe.sample_rate)

    export = workdir / "reload_export.lua"
    export.write_text(reload_export_lua(recipe, session_dir, export_dir), encoding="utf-8")
    log += runner.run(export, workdir, xdg, EXPORT_OK)
    for marker in (ROUTING_OK, AUTOMATION_OK):
        if marker not in log:
            raise ArdourError(f"reload check {marker} missing")

    out = workdir / "out"
    out.mkdir(exist_ok=True)
    names: list[str] = ["master", *BUS_NAMES]
    files: dict[str, Path] = {}
    for name in names:
        found = sorted((export_dir / name).glob("*.wav"))
        if len(found) != 1:
            raise ArdourError(f"expected one exported WAV in {export_dir / name}, got {found}")
        files[name] = out / f"{name}.wav"
        found[0].replace(files[name])
    stems: dict[BusName, Path] = {b: files[b] for b in BUS_NAMES}
    return MixResult(
        master=files["master"],
        stems=stems,
        session_dir=session_dir,
        placements=placements,
        generated=cache.generated[first:],
        log=log,
    )


def stem_sum_error_db(master: Channels, stems: Mapping[str, Channels]) -> float:
    """Level of ``master - sum(stems)`` relative to the master's RMS, in dB."""
    err = sig = 0.0
    for c, chan in enumerate(master):
        for i, m in enumerate(chan):
            d = m - sum(s[c][i] for s in stems.values())
            err += d * d
            sig += m * m
    if sig == 0.0:
        raise ValueError("master is silent")
    if err == 0.0:
        return -math.inf
    return 10.0 * math.log10(err / sig)
