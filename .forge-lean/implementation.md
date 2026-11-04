### Approach
Windows storage hosts hand off to a one-time admin script and a gate step; a gpu-only host role is modelled on the registry entry and host slot; the manifest drops unshipped commands and installs audio tools from the pinned image.

### Changes
- src/spectracut/setup/windows_storage.ps1: new admin script (hidden account, SMB share, Tailscale-only firewall, password file consumed and deleted, marker file).
- src/spectracut/setup/storage.py: Windows serve = write script, ACL'd password file (stdin), gate step failing until the share exists; `storage_notes`.
- src/spectracut/setup/plan.py: admin-step note in plan notes.
- src/spectracut/cli/setup.py: `--storage-host` help.
- src/spectracut/cli/render.py: `role`, `ram_cap_gb` (gpu-only default 4 GB), `hosts add --role --ram-cap-gb`.
- src/spectracut/queue/executors/hostpool.py: `HostSlot.build_lanes`, `computeshare_allowance`, build jobs skip gpu-only hosts.
- src/spectracut/setup/manifest.yaml: dropped ardour/supercollider/audio-plugins/local-models/assets/addons; added `audio-image`.
- tests: new tests/setup/test_storage.py; updated test_manifest.py, test_plan.py, test_queue.py.

### Acceptance Criteria Status
- R1 ✅ (unit-level, see Notes) storage.py `_windows_admin_steps`, tests/setup/test_storage.py
- R2 ✅ hostpool.py `computeshare_allowance`, test_queue.py gpu_only test
- R3 ✅ tests/setup/test_manifest.py command-resolution test
- R4 ✅ (unit-level) manifest.yaml audio-image, test_audio_tools_install_from_the_pinned_image
- R5 ✅ windows_storage.ps1, storage.py
- R6 ✅ cli/render.py HostEntry.slot
- R7 ✅ manifest.yaml audio-image
- R8 ✅ components dropped

### Verification
- V1 `uv run --frozen pytest -q tests/setup/test_storage.py tests/setup/test_manifest.py tests/setup/test_plan.py`: pass.
- V2 `uv run --frozen pytest -q tests/queue tests/cli/test_cli.py -k 'host or gpu or role'`: pass.
- New gpu-only/general tests fail without the fix (3 failed with source changes stashed). ruff and mypy clean on changed files.

### Notes
- The [type:e2e] criteria (R1, R4) are verified with unit tests against a fake runner; no live Windows host or docker build was run.
- `audio-image` install runs `docker build` from `$SPECTRACUT_REPO` (default cwd), so it needs a source checkout.
- Existing hosts.json files load unchanged (role defaults to general).

## Conformance fix
- R1: added simulated-host flow test (gate fails until admin script ran, re-run mounts and maps).
- R4: added test executing the audio-image install command with a recording `docker`.
- Real Windows admin run and real docker build are not possible here (no daemon/host); recorded as amendments in result.json.
