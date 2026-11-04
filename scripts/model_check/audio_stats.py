"""Sanity stats for generated wavs (duration, peak, RMS, 95% spectral roll-off, silence fraction). Usage: run.ps1 tts audio_stats.py"""
import glob, os, numpy as np, soundfile as sf
from common import OUT
for f in sorted(glob.glob(os.path.join(OUT, "*.wav"))):
    a, sr = sf.read(f); m = a if a.ndim == 1 else a.mean(1)
    seg = m[: sr * 8]; sp = np.abs(np.fft.rfft(seg)) ** 2; fr = np.fft.rfftfreq(len(seg), 1 / sr)
    roll = fr[min(len(fr) - 1, np.searchsorted(np.cumsum(sp) / sp.sum(), 0.95))]
    sil = float((np.abs(m) < 0.01).mean())
    print(f"{os.path.basename(f)[:28]:28} sr={sr} ch={1 if a.ndim==1 else a.shape[1]} dur={len(m)/sr:.1f}s peak={abs(m).max():.2f} rms={np.sqrt((m**2).mean()):.3f} rolloff95={int(roll)}Hz silent={sil:.2f}")
