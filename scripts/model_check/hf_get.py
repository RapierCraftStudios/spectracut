# SPDX-License-Identifier: AGPL-3.0-or-later
"""Download single files: ``hf_get.py <repo> <file> [<file> ...]``, saved under ``$SC_MODELS/gguf``.

Gated repos read the token from ``HF_TOKEN`` in the environment (never from a file in the repo).
"""

from __future__ import annotations

import sys

from common import models_root, setup_env


def main() -> None:
    setup_env()
    from huggingface_hub import hf_hub_download

    repo, files = sys.argv[1], sys.argv[2:]
    dest = models_root() / "gguf" / repo.split("/")[-1]
    for f in files:
        print(hf_hub_download(repo, f, local_dir=str(dest)), flush=True)


if __name__ == "__main__":
    main()
