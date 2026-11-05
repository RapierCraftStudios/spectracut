# SPDX-License-Identifier: AGPL-3.0-or-later
"""ASR/alignment (faster-whisper large-v3-turbo, MIT) and CLAP (laion/larger_clap_general,
Apache-2.0) on ``out/kokoro.wav``. Also the round-trip check for the TTS candidates:
``run.ps1 tts asr_clap.py [wav ...]`` transcribes every given clip.
"""

from __future__ import annotations

import glob
import os
import site
import sys
import time

from common import measure, out_dir, setup_env


def _cuda_dlls() -> None:
    """Make pip-installed CUDA DLLs (nvidia-cublas-cu12, nvidia-cudnn-cu12) loadable on Windows."""
    for sp in site.getsitepackages():
        for d in glob.glob(os.path.join(sp, "nvidia", "*", "bin")):
            getattr(os, "add_dll_directory")(d)  # noqa: B009 (Windows only)
            os.environ["PATH"] = d + os.pathsep + os.environ["PATH"]


def main() -> None:
    setup_env()
    if os.name == "nt":
        _cuda_dlls()
    wavs = sys.argv[1:] or [str(out_dir() / "kokoro.wav")]
    with measure("asr", "faster-whisper large-v3-turbo", "float16 word_timestamps beam=1") as m:
        from faster_whisper import WhisperModel

        t = time.time()
        mdl = WhisperModel("large-v3-turbo", device="cuda", compute_type="float16")
        m["load_s"] = round(time.time() - t, 1)
        for wav in wavs:
            for i in range(2):
                t = time.time()
                segs, _info = mdl.transcribe(wav, word_timestamps=True, beam_size=1)
                words = [w for s in segs for w in s.words]
                took = round(time.time() - t, 2)
            name = os.path.basename(wav)
            m[name] = {
                "s": took,
                "n_words": len(words),
                "text": " ".join(w.word.strip() for w in words)[:240],
            }
    with measure("clap", "laion/larger_clap_general", "fp32 zero-shot 3 captions") as m:
        import librosa
        import soundfile as sf
        import torch
        from transformers import ClapModel, ClapProcessor

        a, sr = sf.read(wavs[0])
        a = librosa.resample(a.astype("float32"), orig_sr=sr, target_sr=48000)
        p = ClapProcessor.from_pretrained("laion/larger_clap_general")
        c = ClapModel.from_pretrained("laion/larger_clap_general").cuda()
        caps = ["a person speaking", "an explosion", "piano music"]
        t = time.time()
        inp = p(text=caps, audio=a, sampling_rate=48000, return_tensors="pt", padding=True)
        with torch.no_grad():
            lg = c(**inp.to("cuda")).logits_per_audio.softmax(-1)[0].tolist()
        m["probs"] = dict(zip(caps, [round(x, 3) for x in lg], strict=True))
        m["infer_s"] = round(time.time() - t, 2)


if __name__ == "__main__":
    main()
