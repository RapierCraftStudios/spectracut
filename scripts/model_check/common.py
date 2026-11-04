"""Shared measurement helpers: peak VRAM (nvidia-smi sampling), wall time, JSONL results."""
import json, os, subprocess, threading, time, platform, contextlib

ROOT = os.environ.get("SC_MODELS", os.path.expanduser("~/spectracut-models"))
OUT = os.path.join(ROOT, "out")
os.makedirs(OUT, exist_ok=True)
os.environ.setdefault("HF_HOME", os.path.join(ROOT, "hf"))


def vram_used_mib():
    r = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
                       capture_output=True, text=True)
    used, total = r.stdout.strip().splitlines()[0].split(",")
    return int(used), int(total)


def _proc_ram_mib():
    """(current, peak) working set of THIS process in MiB (Windows, ctypes; no psutil needed)."""
    try:
        import ctypes
        from ctypes import wintypes
        class PMC(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t), ("a", ctypes.c_size_t), ("b", ctypes.c_size_t),
                        ("c", ctypes.c_size_t), ("d", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
        pm = PMC(); pm.cb = ctypes.sizeof(pm)
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.GetCurrentProcess.restype = wintypes.HANDLE
        k.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
        k.K32GetProcessMemoryInfo(k.GetCurrentProcess(), ctypes.byref(pm), pm.cb)
        return pm.WorkingSetSize // 2**20, pm.PeakWorkingSetSize // 2**20
    except Exception:
        return 0, 0


RAM_LIMIT_MIB = int(float(os.environ.get("SC_RAM_LIMIT_GB", "0")) * 1024)  # owner rule: mrdubey-1 <= 4 GB


class Sampler(threading.Thread):
    """Samples whole-GPU memory.used every 100 ms; peak is reported relative to the baseline at start
    (so other desktop processes already on the card are excluded)."""
    def __init__(self):
        super().__init__(daemon=True)
        self.base, self.total = vram_used_mib()
        self.peak = self.base
        self.stop = False

    def run(self):
        while not self.stop:
            try:
                self.peak = max(self.peak, vram_used_mib()[0])
            except Exception:
                pass
            cur, pk = _proc_ram_mib(); self.ram_peak = max(getattr(self, "ram_peak", 0), pk)
            if RAM_LIMIT_MIB and cur > RAM_LIMIT_MIB:  # watchdog: kill the test rather than disturb the owner
                print(f"[common] RAM {cur} MiB > limit {RAM_LIMIT_MIB} MiB -> killing", flush=True); os._exit(99)
            time.sleep(0.1)


@contextlib.contextmanager
def measure(job, model, settings, note=""):
    """Usage: with measure('tts','kokoro','prompt=...') as m: ...; m['extra']={...}"""
    s = Sampler(); s.start()
    rec = {"job": job, "model": model, "settings": settings, "host": platform.node(),
           "gpu_total_mib": s.total, "vram_cap_gb": os.environ.get("SC_VRAM_CAP_GB"), "baseline_used_mib": s.base, "extra": {}}
    t0 = time.time()
    try:
        yield rec
        rec["ok"] = True
    except BaseException as e:
        rec["ok"] = False
        rec["error"] = repr(e)[:500]
        raise
    finally:
        rec["seconds"] = round(time.time() - t0, 2)
        time.sleep(0.25); s.stop = True
        rec["peak_vram_mib"] = s.peak - s.base
        rec["peak_vram_abs_mib"] = s.peak
        rec["peak_ram_mib"] = max(getattr(s, "ram_peak", 0), _proc_ram_mib()[1])
        rec["note"] = note
        with open(os.path.join(OUT, "results.jsonl"), "a") as f:
            f.write(json.dumps(rec) + "\n")
        print("RESULT", json.dumps(rec))


# Non-admin Windows (mrdubey-1): symlinks can be created but not traversed (os error 448/22).
# Force huggingface_hub to copy files into the cache instead of symlinking.
def _no_symlinks():
    try:
        import huggingface_hub.file_download as fd
        fd.are_symlinks_supported = lambda *a, **k: False
    except Exception:
        pass


_no_symlinks()


# Emulate a smaller card on a bigger one: SC_VRAM_CAP_GB=8 caps this process's CUDA allocator at 8 GiB
# (an OOM then means it would not fit an 8 GB card, modulo ~0.3-0.5 GB CUDA context/driver overhead).
def _vram_cap():
    cap = os.environ.get("SC_VRAM_CAP_GB")
    if cap:
        import torch
        total = torch.cuda.get_device_properties(0).total_memory
        torch.cuda.set_per_process_memory_fraction(min(1.0, float(cap) * 2**30 / total))
        print(f"[common] VRAM cap {cap} GiB applied")


_vram_cap()
