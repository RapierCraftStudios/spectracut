# SPDX-License-Identifier: AGPL-3.0-or-later
"""What ``spectracut setup`` installs on the model host for each local model family.

One :class:`ModelAsset` per family of :data:`spectracut.providers.local.registry.MODELS`:
the ``spectracut-host`` extra holding its Python dependencies (``host/pyproject.toml``),
any source repository its loader imports (pinned to a commit), the Hugging Face weights
(pinned to a commit sha, optionally narrowed to the files the loader reads) and the
approximate download size, which the free-disk check uses. Every source is ungated:
``HF_TOKEN`` is optional (it only raises rate limits) and never required.

Weights live under the host model root (:data:`MODEL_ROOT_ENV`, else the per-OS default)
as ``<root>/<family>/<model id>/<repo name>``; source checkouts as ``.../src``. Setup
writes them there and ``spectracut_host.loaders`` reads them from there.
"""

from __future__ import annotations

import hashlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path

MODEL_ROOT_ENV = "SPECTRACUT_MODEL_ROOT"
"""Overrides the host model root on the model host."""
LINUX_MODEL_ROOT = "$HOME/.local/share/spectracut/models"
WINDOWS_MODEL_ROOT = "%LOCALAPPDATA%\\spectracut\\models"
"""The Windows default (setup steps spell it ``$env:LOCALAPPDATA/spectracut/models``)."""

TORCH_INDEX = "https://download.pytorch.org/whl/cu128"
"""CUDA torch for the model host (PyPI's Windows torch is CPU-only)."""
HF_CLI = "huggingface-hub==1.33.0"
"""The ``hf`` CLI setup downloads weights with (the version the extras pin)."""
SRC_DIR = "src"
BIN_DIR = "bin"
LLAMA_CPP_RELEASE = "b12110"
"""The llama.cpp release measured on legion (local-model-selection.md, CUDA 12.4 build)."""
_LLAMA_URL = f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_CPP_RELEASE}"


@dataclass(frozen=True)
class WeightsSource:
    """One Hugging Face repo at a pinned commit; ``include`` narrows the files fetched."""

    repo: str
    revision: str
    include: tuple[str, ...] = ()

    @property
    def subdir(self) -> str:
        return self.repo.rsplit("/", 1)[-1]


@dataclass(frozen=True)
class ModelAsset:
    """Everything one family needs on the model host besides spectracut-host itself."""

    family: str
    extra: str
    model_ids: tuple[str, ...]
    weights: tuple[WeightsSource, ...]
    size_gb: float
    git_repo: str | None = None
    git_commit: str | None = None
    gated: bool = False
    windows_archives: tuple[str, ...] = ()
    """Release zips setup expands into ``.../<model id>/bin`` on a Windows model host."""


ASSETS: dict[str, ModelAsset] = {
    a.family: a
    for a in (
        ModelAsset(
            "ezaudio",
            "ezaudio",
            ("ezaudio-xl",),
            (
                WeightsSource(
                    "OpenSound/EzAudio",
                    "58579e864ba3220a2962d3db4e11f903272bc999",
                    ("ckpts/s3/ezaudio_s3_xl.pt", "ckpts/vae/*"),
                ),
                WeightsSource(
                    "google/flan-t5-xl",
                    "7d6315df2c2fb742f0f5b556879d730926ca9001",
                    ("*.json", "spiece.model", "*.safetensors"),
                ),
            ),
            15.5,
            "https://github.com/haidog-yaqub/EzAudio.git",
            "86f18268117dde73bb274401c610e639e4a8fae1",
        ),
        ModelAsset(
            "moss-sfx",
            "moss-sfx",
            ("moss-sfx-8b-nf4",),
            (
                WeightsSource(
                    "OpenMOSS-Team/MOSS-SoundEffect", "1cc77ff2357c8aa8f95fd0c6512cf3cf65860d6e"
                ),
            ),
            16.7,
        ),
        ModelAsset(
            "hunyuan-foley",
            "hunyuan-foley",
            ("hunyuan-foley-xl",),
            (
                WeightsSource(
                    "tencent/HunyuanVideo-Foley",
                    "3abd4e833b95b8db0fc9c687afc52483a48e9a97",
                    (
                        "hunyuanvideo_foley_xl.pth",
                        "vae_128d_48k.pth",
                        "synchformer_state_dict.pth",
                        "*.yaml",
                        "*.json",
                    ),
                ),
            ),
            8.3,
            "https://github.com/Tencent-Hunyuan/HunyuanVideo-Foley.git",
            "df7b005b5023df2a9b73e1d66dd51d452799884e",
        ),
        ModelAsset(
            "ace-step",
            "ace-step",
            ("ace-step-1.5",),
            (
                WeightsSource(
                    "ACE-Step/Ace-Step1.5",
                    "19671f406d603126926c1b7e2adc169acbcade22",
                    ("acestep-v15-turbo/*", "Qwen3-Embedding-0.6B/*", "vae/*", "*.json"),
                ),
                WeightsSource(
                    "ACE-Step/acestep-5Hz-lm-0.6B", "148d8ea0225bdab342ee1ae3a354275ccd60ca80"
                ),
            ),
            7.6,
            "https://github.com/ACE-Step/ACE-Step-1.5.git",
            "ca1e85fe9430179831e6bc6be790c332190a3866",
        ),
        ModelAsset(
            "kokoro",
            "kokoro",
            ("kokoro-82m",),
            (WeightsSource("hexgrad/Kokoro-82M", "f3ff3571791e39611d31c381e3a41a3af07b4987"),),
            0.5,
        ),
        ModelAsset(
            "llama-cpp",
            "llama-cpp",
            ("qwen2.5-omni-7b-q4",),
            (
                WeightsSource(
                    "ggml-org/Qwen2.5-Omni-7B-GGUF",
                    "89b785438c8901d4635e42f50480ba5985a1bbf1",
                    ("Qwen2.5-Omni-7B-Q4_K_M.gguf", "mmproj-Qwen2.5-Omni-7B-Q8_0.gguf"),
                ),
            ),
            6.3,
            windows_archives=(
                f"{_LLAMA_URL}/llama-{LLAMA_CPP_RELEASE}-bin-win-cuda-12.4-x64.zip",
                f"{_LLAMA_URL}/cudart-llama-bin-win-cuda-12.4-x64.zip",
            ),
        ),
        ModelAsset(
            "diffusers-gguf",
            "diffusers-gguf",
            ("z-image-turbo-q4",),
            (
                WeightsSource(
                    "jayn7/Z-Image-Turbo-GGUF",
                    "6e2dff8e3abd78051c1211fb5833a03818a2f3cc",
                    ("z_image_turbo-Q4_K_M.gguf",),
                ),
                WeightsSource(
                    "Tongyi-MAI/Z-Image-Turbo",
                    "f332072aa78be7aecdf3ee76d5c247082da564a6",
                    (
                        "model_index.json",
                        "scheduler/*",
                        "text_encoder/*",
                        "tokenizer/*",
                        "transformer/config.json",
                        "vae/*",
                    ),
                ),
            ),
            13.3,
        ),
        ModelAsset(
            "faster-whisper",
            "faster-whisper",
            ("faster-whisper-large-v3-turbo",),
            (
                WeightsSource(
                    "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
                    "0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf",
                ),
            ),
            1.7,
        ),
        ModelAsset(
            "clap",
            "clap",
            ("larger-clap-general",),
            (
                WeightsSource(
                    "laion/larger_clap_general",
                    "ada0c23a36c4e8582805bb38fec3905903f18b41",
                    ("*.json", "*.txt", "*.safetensors"),
                ),
            ),
            0.9,
        ),
    )
}


def asset_for(model_id: str) -> ModelAsset:
    """The asset of the family that serves ``model_id``."""
    for asset in ASSETS.values():
        if model_id in asset.model_ids:
            return asset
    raise KeyError(f"no model asset for {model_id!r}")


def model_version(model_id: str) -> str:
    """What setup state records for ``model_id``: a digest of its pinned sources."""
    a = asset_for(model_id)
    parts = [f"{w.repo}@{w.revision}:{','.join(w.include)}" for w in a.weights]
    if a.git_repo:
        parts.append(f"{a.git_repo}@{a.git_commit}")
    parts += a.windows_archives
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:12]


def extras_digest() -> str:
    """Digest of what the deps step installs besides the wheel (extras and torch index)."""
    text = "\n".join([TORCH_INDEX, *sorted(a.extra for a in ASSETS.values())])
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def default_model_root() -> Path:
    """The host model root on this machine: :data:`MODEL_ROOT_ENV`, else the OS default."""
    env = os.environ.get(MODEL_ROOT_ENV, "").strip()
    if env:
        return Path(env).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "spectracut" / "models"
    return Path.home() / ".local" / "share" / "spectracut" / "models"


__all__ = [
    "ASSETS",
    "BIN_DIR",
    "HF_CLI",
    "LINUX_MODEL_ROOT",
    "LLAMA_CPP_RELEASE",
    "MODEL_ROOT_ENV",
    "SRC_DIR",
    "TORCH_INDEX",
    "WINDOWS_MODEL_ROOT",
    "ModelAsset",
    "WeightsSource",
    "asset_for",
    "default_model_root",
    "extras_digest",
    "model_version",
]
