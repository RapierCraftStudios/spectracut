#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Real e2e check for issue #49: push the model_check scripts to legion (the only model host), run
# one Kokoro-82M measurement on its RTX 3070 Laptop (8 GB) and verify the newest results row:
# it must come from legion with peak_vram_mib > 0 and seconds > 0.
# Usage: bash scripts/model_check/legion_smoke.sh   (needs `ssh legion` and the `tts` venv there)
set -euo pipefail

HOST=${LEGION_HOST:-legion}
ROOT='C:/spectracut/models'
here=$(cd "$(dirname "$0")" && pwd)
scratch=$(mktemp -d "${TMPDIR:-/tmp}/legion_smoke.XXXXXX")
trap 'rm -rf "$scratch"' EXIT

quiet() { grep -viE 'post-quantum|store now|openssh.com' || true; }

bash "$here/push.sh" "$HOST"
ssh "$HOST" "powershell -NoProfile -ExecutionPolicy Bypass -Command \"\$env:SC_MODELS='$ROOT'; & '$ROOT/model_check/run.ps1' tts tts_kokoro.py\"" \
  2>&1 | quiet | tee "$scratch/run.log" | tail -n 3
scp -q "$HOST:$ROOT/out/results.jsonl" "$scratch/results.jsonl" 2>&1 | quiet

python3 - "$scratch/results.jsonl" <<'PY'
import json
import sys

rows = []
with open(sys.argv[1], encoding="utf-8-sig") as fh:
    for line in fh:
        s = line.strip().lstrip("\ufeff")
        if s.startswith("{"):
            rows.append(json.loads(s))
last = rows[-1]
ok = (
    last.get("model") == "Kokoro-82M"
    and str(last.get("host", "")).lower() == "legion"
    and last.get("ok") is True
    and last.get("peak_vram_mib", 0) > 0
    and last.get("seconds", 0) > 0
)
print("legion smoke:", "PASS" if ok else "FAIL", json.dumps(last)[:300])
sys.exit(0 if ok else 1)
PY
