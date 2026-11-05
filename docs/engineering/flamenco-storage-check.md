# Flamenco storage check (issue #79)

Measured 2026-11-04 on the owner's tailnet with Flamenco 3.9.3 and Blender 4.5.15 LTS.

## Layout tested

| Piece | Where | Notes |
|---|---|---|
| Flamenco Manager | `archlinux` (control plane), user service `flamenco-manager` | listens on the Tailscale IP only (`100.105.62.23:8085`), Shaman off |
| Shared storage | Samba share `flamenco` → `/srv/flamenco` on `archlinux` | SMB3 only, `hosts allow` Tailscale range + localhost; LAN connections refused |
| Windows worker | `legion` (RTX 3070 Laptop 8 GB), native `flamenco-worker.exe` in `C:\spectracut` | share mapped as `F:` via a PowerShell credential (password never on a command line) |
| Linux worker | `archlinux` (CPU only) | same job, same files |
| Two-way variable `storage` | `/srv/flamenco` (Linux) ↔ `F:\` (Windows) | |

Test scene: an aluminium device slab with an emissive screen, glass-like floor, two area lights,
a moving camera with depth of field; 3840×2160, 8 frames, multilayer EXR (DWAA, half float) with
Z, diffuse colour, glossy direct, emission, vector and object cryptomatte.

## Results

| Job | Engine / device | Render per frame | Denoise | Wall per frame | Output per frame |
|---|---|---|---|---|---|
| Cycles, GPU prefs not applied (first run) | CPU fallback | 3 min 40 s | 45 s (CPU) | about 4.5 min | 67–81 MB |
| Cycles, OptiX | RTX 3070 | **10 s** (64 spp) | 43 s (CPU) | about 100 s | 70–81 MB |
| Cycles, OptiX + GPU denoise | RTX 3070 | 10 s | **2 s** | 46 s | 70–81 MB |
| Eevee | RTX 3070 | **2 s** (64 samples) | n/a | 14 s | about 6 MB |
| Cycles 25 % / 8 spp, Linux worker | CPU | | | 29 s | small |

SMB throughput over Tailscale (legion ↔ archlinux, direct LAN path), 78 MB EXR:
**read 2.3 MB/s, write 2.0 MB/s.**

## Findings

1. **The layout works end to end.** Manager on the control plane, native Windows worker on a GPU
   host, Linux worker, one shared storage, two-way path translation. The same job file renders on
   both platforms.
2. **Windows workers can run headless.** The worker started from an SSH session runs in session 0
   (no desktop), and both Cycles (OptiX) and Eevee rendered on the GPU there. A Windows service or
   startup task is viable; it does not need a logged-in user.
3. **Wi-Fi storage is the bottleneck.** `archlinux` is on Wi-Fi at −70 dBm and `legion` on Wi-Fi
   (144 Mbps link). Writing one 4K Cycles EXR to the share takes about 35–40 s, three times the
   render and denoise time. Eevee frames (6 MB) are fine.
4. **GPU setup is not in the .blend.** Cycles' device list is a user preference, so a factory or
   headless Blender renders on the CPU unless a startup script enables OptiX/CUDA. GPU denoising
   (`scene.cycles.denoising_use_gpu`) is a scene setting, applied after load. Both are in
   `scripts/flamenco_enable_gpu.py`, passed via the Windows `blenderArgs` variable.
5. **Backslashes in Flamenco variables are eaten.** `--python F:\tools\enable_gpu.py` reached
   Blender as `F:toolsenable_gpu.py`. Use forward slashes (`F:/tools/enable_gpu.py`), which Windows
   accepts.
6. **Samba won't bind to `tailscale0` by name or address** (the `/32` interface is skipped). The
   share listens on all interfaces and relies on `hosts allow` (Tailscale range + localhost) and
   `hosts deny = 0.0.0.0/0`; a LAN connection is refused at negotiation.

## Decision

- Keep Flamenco with shared storage (Shaman off, copy per job), Manager on the control plane.
- **Storage host must be wired**, or storage moves to the fastest wired machine on the tailnet
  (e.g. the main PC), with the Manager staying on the control plane. Owner to confirm which box can
  be wired.
- **SpectraCut's `spectracut-shot` job type renders to the worker's local disk and uploads in the
  background** while the next frame renders, so the GPU never waits on the network. Previews
  (Eevee) can write straight to the share.
- `spectracut setup` (#80) installs the Manager, Samba config, workers and
  `flamenco_enable_gpu.py` exactly as above, with forward-slash Windows paths.

## Reproduce

```bash
python3 scripts/flamenco_storage_check.py --manager http://100.105.62.23:8085 \
  --blend /srv/flamenco/jobs/storage-check/cycles.blend --name cycles-4k \
  --manager-storage ~/.local/opt/flamenco-3.9.3-linux-amd64/flamenco-manager-storage
```

## Update: storage on the wired main PC (2026-11-04)

The owner's main PC (`mrdubey`, RTX 4070 Ti, wired) now hosts the shared storage:
`\\MRDUBEY\flamenco` → `D:\SpectraCut\flamenco` (205 GB free), a dedicated hidden local account
`flamenco`, and a firewall rule allowing SMB only from `100.64.0.0/10`. The control plane mounts it
with CIFS at `/srv/flamenco` (credentials in `/etc/spectracut/flamenco.cred`, root-only), so the
Manager's Linux paths did not change. The Samba share on the control plane was retired.

Windows workers now use drive **`S:`** (the main PC already has a physical `F:`). On the storage
host itself `S:` is a `subst` of the local folder, so its frames never touch the network. Both
Windows workers run `scripts/flamenco_windows_worker.ps1`.

| 8 frames, 4K | Legion only, storage on control plane (Wi-Fi) | Both GPUs, storage on main PC |
|---|---|---|
| Cycles OptiX + GPU denoise | 370 s | **98 s** |
| Eevee | 115 s | **64 s** |

Throughput to the share: control plane → main PC 11.3 MB/s; legion → main PC 2.3 MB/s read,
3.6 MB/s write (legion's Wi-Fi is the limit). The main PC takes most final frames because it
writes locally; legion contributes but uploads slowly, so render-local-then-upload in the
`spectracut-shot` job type still matters for Wi-Fi workers.

More findings from adding the second worker:
- **Workers must find Blender via PATH.** With no desktop session, the worker's Blender detection
  (through Windows "Open With") fails; with the Manager's Windows `blender` value set to the
  special value `blender`, the worker resolves it on PATH. The worker script puts its own Blender
  folder first on PATH, so each machine keeps Blender wherever it likes (e.g. under the SSH
  account's profile, which differs per machine).
- **The first render on a new GPU compiles shaders and kernels** (about 2 minutes for Eevee on
  the 4070 Ti); later jobs run at full speed. Setup should warm each worker once.
- **The storage drive letter must be free on every Windows worker**; setup picks one.
