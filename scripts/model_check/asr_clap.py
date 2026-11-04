"""faster-whisper large-v3-turbo (ASR + word timestamps) and laion/larger_clap_general (audio-text similarity)
on the Kokoro sample produced by tts_kokoro.py. Venv: tts (+ faster-whisper, transformers, nvidia-cublas-cu12, nvidia-cudnn-cu12)."""
import os, sys, time, site, glob
for sp in site.getsitepackages():  # make pip-installed CUDA DLLs discoverable (Windows)
    for d in glob.glob(os.path.join(sp, "nvidia", "*", "bin")):
        os.add_dll_directory(d); os.environ["PATH"] = d + os.pathsep + os.environ["PATH"]
from common import measure, OUT
wav = f"{OUT}/kokoro.wav"
with measure("asr", "faster-whisper large-v3-turbo", "float16 word_timestamps beam=1") as m:
    from faster_whisper import WhisperModel
    t = time.time(); mdl = WhisperModel("large-v3-turbo", device="cuda", compute_type="float16"); m["extra"]["load_s"] = round(time.time() - t, 1)
    for i in range(2):
        t = time.time(); segs, info = mdl.transcribe(wav, word_timestamps=True, beam_size=1); words = [w for s in segs for w in s.words]
        m["extra"][f"run{i}_s"] = round(time.time() - t, 2)
    m["extra"]["text"] = " ".join(w.word.strip() for w in words)[:200]; m["extra"]["n_words"] = len(words)
    m["extra"]["first_last"] = [round(words[0].start, 2), round(words[-1].end, 2)]
with measure("clap", "laion/larger_clap_general", "fp32 zero-shot 3 captions") as m:
    import torch, soundfile as sf, librosa
    from transformers import ClapModel, ClapProcessor
    a, sr = sf.read(wav); a = librosa.resample(a.astype("float32"), orig_sr=sr, target_sr=48000)
    p = ClapProcessor.from_pretrained("laion/larger_clap_general"); c = ClapModel.from_pretrained("laion/larger_clap_general").cuda()
    caps = ["a person speaking", "an explosion", "piano music"]
    t = time.time()
    inp = p(text=caps, audio=a, sampling_rate=48000, return_tensors="pt", padding=True).to("cuda")
    with torch.no_grad(): lg = c(**inp).logits_per_audio.softmax(-1)[0].tolist()
    m["extra"]["probs"] = dict(zip(caps, [round(x, 3) for x in lg])); m["extra"]["infer_s"] = round(time.time() - t, 2)
