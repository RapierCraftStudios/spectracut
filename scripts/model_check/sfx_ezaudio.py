# SPDX-License-Identifier: AGPL-3.0-or-later
"""SFX candidate: EzAudio (MIT code and weights, OpenSound/EzAudio; text encoder google/flan-t5-xl,
Apache-2.0). Venv ``ezaudio`` with the repo at ``$SC_MODELS/repos/ezaudio`` (run via run_repo.ps1
so the relative ``ckpts/`` paths resolve).

Usage: ``run_repo.ps1 ezaudio ezaudio sfx_ezaudio.py [s3_l|s3_xl]``.
"""

from __future__ import annotations

import os
import sys
import time

from common import measure, models_root, out_dir, setup_env
from sfx_moss import PROMPTS


def main() -> None:
    size = sys.argv[1] if len(sys.argv) > 1 else "s3_xl"
    setup_env()
    repo = models_root() / "repos" / "ezaudio"
    sys.path.insert(0, str(repo))
    os.chdir(repo)
    import soundfile as sf
    from api.ezaudio import EzAudio

    with measure("sfx", f"EzAudio {size}", "steps=50 guidance=5 fp32 10 s") as m:
        t = time.time()
        ez = EzAudio(model_name=size, device="cuda")
        m["load_s"] = round(time.time() - t, 1)
        for i, p in enumerate(PROMPTS):
            t = time.time()
            sr, audio = ez.generate_audio(p)
            m[f"gen{i}_s"] = round(time.time() - t, 1)
            m[f"gen{i}_audio_s"] = round(len(audio) / sr, 2)
            sf.write(str(out_dir() / f"ezaudio_{size}_{i}.wav"), audio, sr)


if __name__ == "__main__":
    main()
