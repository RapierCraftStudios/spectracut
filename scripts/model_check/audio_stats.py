# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sanity stats for generated wavs: duration, peak, RMS, 95 % spectral roll-off, silent fraction.

Usage: ``run.ps1 tts audio_stats.py [glob]`` (default ``out/*.wav``).
"""

from __future__ import annotations

import glob
import os
import sys

from common import out_dir


def main() -> None:
    import numpy as np
    import soundfile as sf

    pattern = sys.argv[1] if len(sys.argv) > 1 else str(out_dir() / "*.wav")
    for f in sorted(glob.glob(pattern)):
        a, sr = sf.read(f)
        mono = a if a.ndim == 1 else a.mean(1)
        seg = mono[: sr * 8]
        sp = np.abs(np.fft.rfft(seg)) ** 2
        fr = np.fft.rfftfreq(len(seg), 1 / sr)
        roll = fr[min(len(fr) - 1, int(np.searchsorted(np.cumsum(sp) / sp.sum(), 0.95)))]
        ch = 1 if a.ndim == 1 else a.shape[1]
        print(
            f"{os.path.basename(f)[:32]:32} sr={sr} ch={ch} dur={len(mono) / sr:.1f}s "
            f"peak={abs(mono).max():.2f} rms={np.sqrt((mono**2).mean()):.3f} "
            f"rolloff95={int(roll)}Hz silent={float((np.abs(mono) < 0.01).mean()):.2f}"
        )


if __name__ == "__main__":
    main()
