# SPDX-License-Identifier: AGPL-3.0-or-later
"""Local model providers: job kind -> configured model -> provider, with a mock per kind.

Which model serves a kind is configuration, never pipeline code: the ``[models]``
table of the local config (``sfx = "ezaudio-xl"``) or ``SPECTRACUT_MODEL_<KIND>``.
``SPECTRACUT_PROVIDERS=mock`` selects the deterministic mocks CI uses.
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
import tomllib
import wave
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from spectracut.providers.base import ProviderError
from spectracut.providers.config import config_path
from spectracut.providers.local.runtime import ModelRuntime, ModelSpec
from spectracut.providers.mock_image import solid_png

#: Job kinds served by local models.
KINDS = ("sfx", "foley", "music", "tts", "critic", "image", "asr", "clap")

#: Picks from docs/engineering/local-model-selection.md (VRAM in GB on the 8 GB host).
MODELS: dict[str, ModelSpec] = {
    m.id: m
    for m in (
        ModelSpec(
            "ezaudio-xl",
            "sfx",
            "ezaudio",
            8,
            source="OpenSound/EzAudio",
            version="xl",
            licence="MIT (weights), Apache-2.0 (flan-t5-xl)",
        ),
        ModelSpec(
            "moss-sfx-8b-nf4",
            "sfx",
            "moss-sfx",
            8,
            source="OpenMOSS-Team/MOSS-SoundEffect",
            version="8b-nf4",
            licence="Apache-2.0",
        ),
        ModelSpec(
            "hunyuan-foley-xl",
            "foley",
            "hunyuan-foley",
            8,
            source="tencent/HunyuanVideo-Foley",
            version="xl",
            licence="Tencent Hunyuan Community",
        ),
        ModelSpec(
            "ace-step-1.5",
            "music",
            "ace-step",
            8,
            source="ACE-Step/Ace-Step1.5",
            version="1.5",
            licence="MIT",
        ),
        ModelSpec("kokoro-82m", "tts", "kokoro", 2, source="hexgrad/Kokoro-82M", licence="Apache-2.0"),
        ModelSpec(
            "qwen2.5-omni-7b-q4",
            "critic",
            "llama-cpp",
            8,
            source="ggml-org/Qwen2.5-Omni-7B-GGUF",
            licence="Apache-2.0",
        ),
        ModelSpec(
            "z-image-turbo-q4",
            "image",
            "diffusers-gguf",
            8,
            source="jayn7/Z-Image-Turbo-GGUF",
            licence="Apache-2.0",
        ),
        ModelSpec(
            "faster-whisper-large-v3-turbo",
            "asr",
            "faster-whisper",
            6,
            source="mobiuslabsgmbh/faster-whisper-large-v3-turbo",
            licence="MIT",
        ),
        ModelSpec(
            "larger-clap-general",
            "clap",
            "clap",
            2,
            source="laion/larger_clap_general",
            licence="Apache-2.0",
        ),
    )
}

DEFAULT_MODELS = {
    "sfx": "ezaudio-xl",
    "foley": "hunyuan-foley-xl",
    "music": "ace-step-1.5",
    "tts": "kokoro-82m",
    "critic": "qwen2.5-omni-7b-q4",
    "image": "z-image-turbo-q4",
    "asr": "faster-whisper-large-v3-turbo",
    "clap": "larger-clap-general",
}


@runtime_checkable
class LocalProvider(Protocol):
    """Every local model kind sits behind this one interface (sfx, tts, music, ...)."""

    name: str
    kind: str

    def generate(self, prompt: str, out_path: Path, **options: Any) -> Path:
        """Produce this kind's artifact (audio, image, JSON verdict) at ``out_path``."""
        ...

    def batch(self) -> Any:
        """Context manager keeping the model warm: loaded once for all calls inside."""
        ...


def _model_table(path: Path | None = None) -> dict[str, Any]:
    path = path or config_path()
    if not path.is_file():
        return {}
    try:
        table = tomllib.loads(path.read_text(encoding="utf-8")).get("models", {})
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ProviderError("config", f"cannot read {path}: {exc}") from exc
    if not isinstance(table, dict):
        raise ProviderError("config", f"{path}: [models] must be a table")
    return table


def resolve_model(
    kind: str, config: Mapping[str, Any] | None = None, env: Mapping[str, str] | None = None
) -> ModelSpec:
    """The model for ``kind``: env, then ``[models]`` config, then the default pick."""
    if kind not in KINDS:
        raise ProviderError("local", f"unknown model kind {kind!r}; expected one of {KINDS}")
    env = os.environ if env is None else env
    table = _model_table() if config is None else config
    model_id = (
        env.get(f"SPECTRACUT_MODEL_{kind.upper()}") or table.get(kind) or DEFAULT_MODELS[kind]
    )
    spec = MODELS.get(str(model_id))
    if spec is None:
        raise ProviderError(
            "local", f"unknown model {model_id!r} for {kind}; known: {sorted(MODELS)}"
        )
    if spec.kind != kind:
        raise ProviderError("local", f"model {spec.id!r} is for {spec.kind}, not {kind}")
    return spec


class RuntimeProvider:
    """A provider that runs its model through a :class:`ModelRuntime` warm session."""

    def __init__(self, spec: ModelSpec, runtime: ModelRuntime) -> None:
        self.spec, self.kind, self.name = spec, spec.kind, spec.id
        self._runtime = runtime

    @contextmanager
    def batch(self) -> Iterator[None]:
        with self._runtime.session(self.spec):
            yield

    def generate(self, prompt: str, out_path: Path, **options: Any) -> Path:
        with self._runtime.session(self.spec) as session:
            session.generate(prompt, out_path=Path(out_path), **options)
        return Path(out_path)


_RATE = 16000


def _wav(prompt: str, seconds: float) -> bytes:
    import io

    digest = hashlib.sha256(prompt.encode()).digest()
    freq = 200 + digest[0] * 4
    frames = b"".join(
        struct.pack("<h", int(8000 * ((i * freq // _RATE) % 2 * 2 - 1)))
        for i in range(int(_RATE * seconds))
    )
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(_RATE)
        w.writeframes(frames)
    return buf.getvalue()


class MockLocalProvider:
    """Deterministic stand-in for any kind: no GPU, no weights, no network."""

    def __init__(self, kind: str) -> None:
        self.kind, self.name = kind, f"mock-{kind}"

    @contextmanager
    def batch(self) -> Iterator[None]:
        yield

    def generate(self, prompt: str, out_path: Path, **options: Any) -> Path:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if self.kind == "image":
            d = hashlib.sha256(prompt.encode()).digest()
            out_path.write_bytes(solid_png(64, 64, (d[0], d[1], d[2])))
        elif self.kind in ("critic", "asr", "clap"):
            out_path.write_text(json.dumps({"kind": self.kind, "text": prompt}), encoding="utf-8")
        else:
            out_path.write_bytes(_wav(prompt, float(options.get("duration_s", 0.1))))
        return out_path


def mock_mode() -> bool:
    return os.environ.get("SPECTRACUT_PROVIDERS", "").strip().lower() == "mock"


def get_provider(
    kind: str,
    runtime: ModelRuntime | None = None,
    *,
    config: Mapping[str, Any] | None = None,
) -> LocalProvider:
    """The provider for ``kind``: the mock in mock mode, else the configured model."""
    if mock_mode():
        resolve_model(kind, config)  # still validates the kind and the configured model
        return MockLocalProvider(kind)
    if runtime is None:
        raise ProviderError("local", f"{kind}: a ModelRuntime is required outside mock mode")
    return RuntimeProvider(resolve_model(kind, config), runtime)
