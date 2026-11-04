**Verdict**: CONFIRMED
**Confidence**: high
**Severity**: medium (setup cannot serve storage from a Windows host without hand work; manifest installs fail on missing commands)
**Task Type**: feature

### What Was Claimed
`--storage-host` can't use a registered host / create a Windows SMB share; manifest calls nonexistent commands and non-repo Arch packages; need a gpu-only host role with RAM cap; audio tools should come from the #48 image.

### What We Found
- `--storage-host` already accepts registered hosts (`src/spectracut/cli/setup.py:92` passes `[CONTROL, *workers]`; `storage.py:57-67`), so the "only control" claim is partly stale (fixed by #124).
- Windows storage serving runs `New-LocalUser`/`New-SmbShare`/firewall as a plain remote step (`storage.py:139-165`); these need admin, but workers are non-admin (#121, `windows_worker.ps1:1-3`). No `windows_storage.ps1` exists.
- Manifest references unshipped commands: `spectracut-plugins` (`manifest.yaml:121`), `spectracut-models` (`:129-131`), `spectracut-assets` (`:136,:142`), `spectracut-addons` (`:149-152`). `pyproject.toml:18-19` ships only `spectracut`; `host/pyproject.toml:11-12` only `spectracut-host`.
- `audio-plugins` names `dragonfly-reverb`, `surge-xt` via pacman (`manifest.yaml:119-120`); ardour/supercollider use distro packages (`:100-115`) while `host/docker/audio.Dockerfile` pins Debian trixie packages.
- No host role / RAM cap: `HostEntry` (`src/spectracut/cli/render.py:181-198`) and `HostSlot` (`queue/executors/hostpool.py:31-38`) have only cpus/mem_gb/gpus; `hosts add` (`render.py:257-290`) has no role option. No computeshare allowance code exists in the repo.

### Root Cause
Greenfield gaps left by #80/#121: Windows share creation assumed admin; manifest written ahead of CLIs; no per-host role model.

### Affected Files
`src/spectracut/setup/storage.py`, `src/spectracut/setup/windows_storage.ps1` (new), `src/spectracut/setup/manifest.yaml`, `src/spectracut/setup/plan.py`, `src/spectracut/cli/setup.py`, `src/spectracut/cli/render.py`, `src/spectracut/queue/executors/hostpool.py`, `pyproject.toml` (ship the .ps1 if needed), `tests/setup/test_storage.py` (new; issue says modify but it does not exist), `tests/setup/test_manifest.py`, `tests/setup/test_plan.py`.

### Sibling Sweep
`windows_worker.ps1` is loaded via `Path.with_name` (`plan.py:91-93`), so new .ps1 is picked up by hatch `packages`. `plan.py:_roles` and the `storage` role `samba` component are affected by storage changes. `test_manifest.py:17-20` EXPECTED set must track component changes.

### History
`e4a88b3` (#121) and #80 (`ecc23fc`, `dc09a6f`) introduced storage.py/manifest.yaml; no prior fix of this issue.

### Recommendation
1. Windows storage host: write `windows_storage.ps1` (hidden `flamenco` account, SMB share, Tailscale-only firewall, password read from an ACL'd file, removed after use). Replace the remote admin step with: write script + password file to the host, print its path and "run as admin, then re-run", and detect whether the share exists (non-admin probe); once present, continue to CIFS mount on control and map on workers.
2. Add `role: general|gpu-only` and `ram_cap_gb` (default 4 for gpu-only) to `HostEntry`/`hosts add`; `HostSlot` gains `build_lanes`; a pure function builds the computeshare allowance (`mem_gb=min(mem, cap)`, `build_lanes=false`) and the host pool never offers build jobs to gpu-only hosts.
3. Manifest: drop `local-models`, `polyhaven-assets`, `ambientcg-assets`, `addons` (CLIs don't exist; #49 owns them) or implement; replace `audio-plugins`/`ardour`/`supercollider` with one `audio-image` component that builds/pulls `host/docker/audio.Dockerfile` (docker, control only). Add a test resolving every command's first token to a shipped console script, a system tool, or the image.

### Related Issues
#80, #121, #48, #49, #1.
