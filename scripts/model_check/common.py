# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared measurement helpers for the local model checks (issue #49).

Every model script wraps one load + generate run in ``measure(...)``. That samples whole-GPU
``memory.used`` through ``nvidia-smi`` every 100 ms and appends one JSON row to
``$SC_MODELS/out/results.jsonl`` with the job, model, host, peak VRAM above the pre-run baseline,
peak process RAM and wall seconds. The pure helpers (``parse_nvidia_smi``, ``build_record``,
``append_result``, ``read_results``, ``results_table``) do not import torch so they can be
unit-tested on the dev box (``tests/test_model_check.py``).

Model hosts: only ``legion`` (RTX 3070 Laptop, 8 GB). Secrets such as ``HF_TOKEN`` are read from
the environment by the scripts that need them and never written to results.
"""

from __future__ import annotations

import contextlib
import json
import os
import platform
import subprocess
import threading
import time
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

RESULT_KEYS = ("job", "model", "settings", "host", "ok", "peak_vram_mib", "seconds")
SECRET_ENV = ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN")


def models_root() -> Path:
    """``$SC_MODELS`` (the folder holding venvs, weights and ``out``), default ``~/spectracut-models``."""
    return Path(os.environ.get("SC_MODELS", str(Path.home() / "spectracut-models")))


def out_dir() -> Path:
    out = models_root() / "out"
    out.mkdir(parents=True, exist_ok=True)
    return out


def parse_nvidia_smi(text: str) -> tuple[int, int]:
    """Parse ``nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader,nounits``."""
    first = text.strip().splitlines()[0]
    used, total = (int(v.strip()) for v in first.split(","))
    return used, total


def vram_used_mib() -> tuple[int, int]:
    query = ["--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"]
    r = subprocess.run(["nvidia-smi", *query], capture_output=True, text=True, check=True)
    return parse_nvidia_smi(r.stdout)


def _proc_ram_mib() -> tuple[int, int]:
    """(current, peak) working set of this process in MiB (Windows only; 0, 0 elsewhere)."""
    if os.name != "nt":
        return 0, 0
    import ctypes
    from ctypes import wintypes

    class PMC(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    pm = PMC()
    pm.cb = ctypes.sizeof(pm)
    k = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)  # noqa: B009 (Windows only)
    k.GetCurrentProcess.restype = wintypes.HANDLE
    k.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
    k.K32GetProcessMemoryInfo(k.GetCurrentProcess(), ctypes.byref(pm), pm.cb)
    return int(pm.WorkingSetSize) // 2**20, int(pm.PeakWorkingSetSize) // 2**20


class Sampler(threading.Thread):
    """Samples GPU memory.used every 100 ms; the peak is reported above the start baseline."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.base, self.total = vram_used_mib()
        self.peak = self.base
        self.ram_peak = 0
        self.halt = False
        limit_gb = float(os.environ.get("SC_RAM_LIMIT_GB", "0"))
        self.ram_limit_mib = int(limit_gb * 1024)

    def run(self) -> None:
        while not self.halt:
            with contextlib.suppress(Exception):
                self.peak = max(self.peak, vram_used_mib()[0])
            cur, pk = _proc_ram_mib()
            self.ram_peak = max(self.ram_peak, pk)
            if self.ram_limit_mib and cur > self.ram_limit_mib:
                print(f"[common] RAM {cur} MiB > limit {self.ram_limit_mib} MiB, stopping", flush=True)
                os._exit(99)
            time.sleep(0.1)


def build_record(
    job: str,
    model: str,
    settings: str,
    *,
    host: str,
    ok: bool,
    seconds: float,
    peak_vram_mib: int,
    peak_ram_mib: int = 0,
    gpu_total_mib: int = 0,
    baseline_used_mib: int = 0,
    extra: dict[str, Any] | None = None,
    error: str = "",
    note: str = "",
) -> dict[str, Any]:
    """One results row. Refuses to record a secret value from the environment."""
    rec: dict[str, Any] = {
        "job": job,
        "model": model,
        "settings": settings,
        "host": host,
        "ok": ok,
        "peak_vram_mib": peak_vram_mib,
        "seconds": round(seconds, 2),
        "peak_ram_mib": peak_ram_mib,
        "gpu_total_mib": gpu_total_mib,
        "baseline_used_mib": baseline_used_mib,
        "vram_cap_gb": os.environ.get("SC_VRAM_CAP_GB"),
        "extra": extra or {},
        "note": note,
    }
    if error:
        rec["error"] = error[:500]
    blob = json.dumps(rec)
    for name in SECRET_ENV:
        secret = os.environ.get(name)
        if secret and secret in blob:
            raise ValueError(f"refusing to write {name} into results")
    return rec


def append_result(path: Path, rec: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def read_results(lines: Iterable[str]) -> list[dict[str, Any]]:
    """Parse JSONL rows (PowerShell may prefix a BOM; non-JSON lines are skipped)."""
    rows: list[dict[str, Any]] = []
    for line in lines:
        s = line.strip().lstrip("﻿")
        if s.startswith("{"):
            rows.append(json.loads(s))
    return rows


def results_table(rows: Iterable[dict[str, Any]]) -> str:
    """Markdown table of results rows (job, model, host, settings, ok, VRAM, seconds, extra)."""
    out = [
        "| job | model | host | settings | ok | peak VRAM MiB | wall s | extra |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        extra = json.dumps(r.get("extra", {})) if r.get("ok") else str(r.get("error", ""))
        out.append(
            f"| {r['job']} | {r['model']} | {r['host']} | {r.get('settings', '')} | {r.get('ok')} "
            f"| {r['peak_vram_mib']} | {r['seconds']} | {extra[:160]} |"
        )
    return "\n".join(out)


def setup_env() -> None:
    """Host-side defaults: HF cache under ``$SC_MODELS/hf``; optional 8 GiB allocator cap."""
    os.environ.setdefault("HF_HOME", str(models_root() / "hf"))
    cap = os.environ.get("SC_VRAM_CAP_GB")
    if cap:  # emulate a smaller card on a bigger one (informational runs only)
        import torch

        total = torch.cuda.get_device_properties(0).total_memory
        torch.cuda.set_per_process_memory_fraction(min(1.0, float(cap) * 2**30 / total))


@contextlib.contextmanager
def measure(job: str, model: str, settings: str, note: str = "") -> Iterator[dict[str, Any]]:
    """``with measure("tts", "Kokoro-82M", "voice=af_heart") as extra: extra["gen_s"] = ...``"""
    sampler = Sampler()
    sampler.start()
    extra: dict[str, Any] = {}
    t0 = time.time()
    ok, error = False, ""
    try:
        yield extra
        ok = True
    except BaseException as e:
        error = repr(e)
        raise
    finally:
        seconds = time.time() - t0
        time.sleep(0.25)
        sampler.halt = True
        rec = build_record(
            job,
            model,
            settings,
            host=platform.node(),
            ok=ok,
            seconds=seconds,
            peak_vram_mib=sampler.peak - sampler.base,
            peak_ram_mib=max(sampler.ram_peak, _proc_ram_mib()[1]),
            gpu_total_mib=sampler.total,
            baseline_used_mib=sampler.base,
            extra=extra,
            error=error,
            note=note,
        )
        append_result(out_dir() / "results.jsonl", rec)
        print("RESULT", json.dumps(rec), flush=True)
