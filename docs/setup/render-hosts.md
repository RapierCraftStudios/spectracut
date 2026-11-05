# Render hosts

Render hosts run Blender and the Flamenco worker. The control plane reaches them over SSH
(Tailscale) and installs everything itself. You never need admin rights on the control plane.

## Registering a host

```sh
spectracut hosts add <ssh-alias> [--role general|gpu-only] [--gpu "MODEL:VRAM_GB"] [--ram-cap-gb N]
spectracut setup
```

`hosts add` installs `spectracut-host`, Blender and the Flamenco worker on the host, reads
its render profile and registers it. `spectracut setup` then configures shared storage and
the worker task or service.

### Roles

- `general` (default): GPU work and CPU build lanes, using all of the host's memory.
- `gpu-only`: for a machine the owner also uses day to day. It gets GPU work only and no
  build lanes, and the scheduler caps it at **4 GB of system RAM** by default (owner rule).
  Jobs that need more memory than that never go to it. `--ram-cap-gb N` changes the cap.

```sh
spectracut hosts add mrdubey-1 --role gpu-only --gpu "RTX 4070 Ti:12"
```

## Windows hosts

On Windows, the worker runs as the scheduled task `spectracut-worker`. It starts at boot and
runs as the host's SSH account, with no password stored (S4U logon). The task runs
`%USERPROFILE%\spectracut\windows_worker.ps1`, which reads its settings (Manager URL, storage
drive and share) from `worker-args.json` next to it. Setup rewrites that file whenever the
settings change, so the task itself is registered only once.

Setup never uses CIM/WMI (`*-ScheduledTask`, `Get-CimInstance`, `Get-NetAdapter`). A standard
account signed in over SSH is denied those. Setup uses `schtasks.exe` to start and stop the
task, and does nothing if the task is not registered yet.

### SSH account is an Administrator

Setup registers the task itself. Nothing else is needed.

### SSH account is a standard user (recommended for a PC you use daily)

A standard account cannot register a task that starts at boot. `spectracut setup` writes
`windows_worker_task.ps1` under the account's profile and prints one warning with the
command to run:

```
warning: mrdubey-1 needs a one-time admin step: ... run
`powershell -NoProfile -ExecutionPolicy Bypass -File "C:\Users\<account>\spectracut\windows_worker_task.ps1"`
```

On that PC, open PowerShell with **Run as administrator** and run the command once. It
registers `spectracut-worker` to run at startup as the SSH account. It also gives that account
the right to start and end the task, then starts the worker. The command and the script
contain no secrets: the storage password stays in the account's `spectracut\smb.pw` file.

After that, `spectracut setup` and `spectracut hosts add` stop and start the worker with
`schtasks.exe` and need no admin. Setup repeats the warning until the task exists.
