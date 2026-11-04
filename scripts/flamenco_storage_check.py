#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Flamenco shared-storage check (issue #79).

Submits a render job to a Flamenco Manager, waits for it, and reports per-frame render,
denoise and wall time plus bytes written, so storage layouts can be compared.

    python3 scripts/flamenco_storage_check.py --manager http://100.105.62.23:8085 \
        --blend /srv/flamenco/jobs/storage-check/cycles.blend --name cycles-4k
"""
import argparse, json, re, time, urllib.request
from pathlib import Path


def call(base, path, body=None):
    req = urllib.request.Request(base + "/api/v3" + path, method="POST" if body is not None else "GET",
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manager", required=True)
    ap.add_argument("--blend", required=True, help="path to the .blend on the shared storage, as the submitter sees it")
    ap.add_argument("--name", required=True)
    ap.add_argument("--frames", default="1-8")
    ap.add_argument("--chunk", type=int, default=2)
    ap.add_argument("--storage", default="/srv/flamenco", help="shared storage root as the submitter sees it")
    ap.add_argument("--manager-storage", type=Path, help="Manager's local storage dir, to read task logs")
    a = ap.parse_args()
    jt = next(t for t in call(a.manager, "/jobs/types")["job_types"] if t["name"] == "simple-blender-render")
    out = f"{a.storage}/renders/{a.name}"
    job = call(a.manager, "/jobs", {
        "name": a.name, "type": jt["name"], "type_etag": jt["etag"], "priority": 50, "submitter_platform": "linux",
        "settings": {"blendfile": a.blend, "frames": a.frames, "chunk_size": a.chunk,
                     "render_output_root": f"{a.storage}/renders/", "add_path_components": 0,
                     "render_output_path": f"{out}/######", "format": "OPEN_EXR_MULTILAYER",
                     "image_file_extension": ".exr", "fps": 24.0, "has_previews": False, "scene": "Scene"}})
    t0 = time.time()
    while (status := call(a.manager, f"/jobs/{job['id']}")["status"]) not in ("completed", "failed", "canceled"):
        time.sleep(5)
    wall = time.time() - t0
    files = sorted(Path(out).glob("*.exr"))
    size = sum(f.stat().st_size for f in files)
    report = {"job": job["id"], "status": status, "frames": len(files), "wall_s": round(wall, 1),
              "wall_per_frame_s": round(wall / max(len(files), 1), 1), "bytes": size,
              "mb_per_frame": round(size / max(len(files), 1) / 1e6, 1)}
    if a.manager_storage:
        render, denoise = [], []
        for log in a.manager_storage.glob(f"job-{job['id'][:4]}/{job['id']}/task-*.txt"):
            text = log.read_text(errors="replace")
            for m in re.finditer(r"Fra:\d+ .*?Time:(\d+):([\d.]+) .*?\| (Finished|Denoising|Finishing)", text):
                secs = int(m.group(1)) * 60 + float(m.group(2))
                (render if m.group(3) == "Finished" else denoise).append((m.group(3), secs))
        fin = [s for k, s in render]
        if fin:
            report["render_s_per_frame"] = round(sum(fin) / len(fin), 1)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
