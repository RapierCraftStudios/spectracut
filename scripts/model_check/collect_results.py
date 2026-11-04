#!/usr/bin/env python3
"""Fetch results.jsonl from both hosts and print a markdown table.  Usage: collect_results.py [outdir]"""
import json, subprocess, sys, os
HOSTS = {"legion": "C:/spectracut/models/out/results.jsonl",
         "mrdubey-1": "C:/Users/artac-remote.MRDUBEY/spectracut-models/out/results.jsonl"}
out = sys.argv[1] if len(sys.argv) > 1 else "."
rows = []
for h, p in HOSTS.items():
    dst = os.path.join(out, f"{h}.jsonl")
    subprocess.run(["scp", "-q", f"{h}:{p}", dst], check=False, stderr=subprocess.DEVNULL)
    if os.path.exists(dst):
        rows += [json.loads(l) for l in open(dst) if l.strip()]
print("| job | model | host | settings | ok | peak VRAM MiB | wall s | extra |\n|---|---|---|---|---|---|---|---|")
for r in rows:
    if r.get("ok") is None: continue
    print(f"| {r['job']} | {r['model']} | {r['host']} | {r['settings']} | {r['ok']} | {r['peak_vram_mib']} | {r['seconds']} | {json.dumps(r['extra']) if r['ok'] else r.get('error','')[:80]} |")
