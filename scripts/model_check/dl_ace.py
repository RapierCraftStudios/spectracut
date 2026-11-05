# SPDX-License-Identifier: AGPL-3.0-or-later
"""ACE-Step 1.5 trimmed checkpoints (turbo DiT, VAE, text embedding, 5 Hz LM 0.6B; skips the
3.5 GB 1.7B LM). Run after ``setup_ace.ps1`` cloned the repo: ``run.ps1 tts dl_ace.py``.
"""

from __future__ import annotations

from common import models_root, setup_env

PATTERNS = ["acestep-v15-turbo/*", "vae/*", "Qwen3-Embedding-0.6B/*", "config.json"]


def main() -> None:
    setup_env()
    from huggingface_hub import snapshot_download

    ck = models_root() / "repos" / "ace" / "checkpoints"
    ck.mkdir(parents=True, exist_ok=True)
    snapshot_download("ACE-Step/Ace-Step1.5", local_dir=str(ck), allow_patterns=PATTERNS)
    lm = ck / "acestep-5Hz-lm-0.6B"
    snapshot_download("ACE-Step/acestep-5Hz-lm-0.6B", local_dir=str(lm))
    print("DONE ace checkpoints", flush=True)


if __name__ == "__main__":
    main()
