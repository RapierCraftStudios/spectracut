# computeshare (optional)

computeshare places Docker sandboxes on spare machines on your tailnet. SpectraCut can use it as
a second executor next to the host pool: `ComputeshareExecutor`
(`src/spectracut/queue/executors/computeshare.py`). It is **off by default**. Without the config
block below, SpectraCut behaves exactly as if computeshare didn't exist.

## How a job runs

1. The executor calls the router's `POST /sandboxes` with a SpectraCut image and the command
   `spectracut-host --once --job <id> --output <output_dir>`.
2. Inside the sandbox, `spectracut-host` pulls that one signed job from the control plane
   (`SPECTRACUT_CONTROL_URL`) over the normal host protocol, runs it, and writes frames straight
   to SpectraCut storage (`output_dir`). Frames never pass through the router.
3. While the job runs, the executor polls `GET /sandboxes/:id` every `poll_interval_s`. The router
   renews a sandbox's lease only while its client keeps polling, so this is what keeps the lease
   alive. Keep the interval well under the router's 10-minute lease.
4. When the sandbox exits, the executor reads the small JSON result manifest
   (`GET /sandboxes/:id/result`), which lists the frames and render profile.
5. The sandbox is always deleted (`DELETE /sandboxes/:id`): on completion, on failure, on timeout
   and when the job is cancelled (`ComputeshareExecutor.cancel(job_id)`).

When no host has a free reservation, the router answers `503` with `Retry-After`. The job stays
`queued` and is tried again once that time has passed. If the header is missing,
`default_retry_after_s` is used.

## Config

In the local SpectraCut config (the same file `spectracut doctor` reads):

```toml
[integrations.computeshare]
enabled = true
router_url = "http://archlinux:8787"        # computeshare router
image = "spectracut-worker:cpu"             # must be on the router's image allow-list
control_url = "http://archlinux:8080"       # where the sandbox pulls its job (else $SPECTRACUT_CONTROL_URL)
output_dir = "/srv/spectracut/output"       # SpectraCut storage, as mounted in the sandbox
poll_interval_s = 30
token_env = "COMPUTESHARE_TOKEN"            # name of the env var holding the router token
# timeout_s = 3600
# default_retry_after_s = 60
# capacity = { cpus = 2, mem_gb = 3 }       # largest job one sandbox takes (router lane slice)
```

Build the executor from that table with
`spectracut.queue.executors.computeshare.computeshare_executor(table)`. It returns `None` unless
`enabled = true`.

The router token is read only from the environment variable named by `token_env` and sent as an
`Authorization: Bearer` header. Never put it in the config file, a spec, a recipe or a log.

`spectracut doctor` reports the integration as `off`, `on` (unhealthy, with the reason) or
`healthy`.

## Current limits

- **CPU jobs only.** Jobs that ask for `gpus > 0` are never offered to computeshare. They stay on
  the host pool or Flamenco until computeshare has whole-GPU leases, the native runner, per-client
  tokens and warm sessions (RapierCraftStudios/computeshare#2, #3, #5, #6).
- The router sizes sandboxes by its own fixed lane slice. The job's `cpus` and `mem_gb` are sent
  with the spec, and `capacity` stops larger jobs being offered at all.
- By default, any non-`blender` job kind may go to computeshare once it is in the queue's executor
  list. Use `JobQueue(route=...)` to restrict which kinds go there.
