# GPU reservations and job failures

The GPU inventory reports each physical index and the NVIDIA UUID from the
driver. Lease replies include the granted index and its UUID. Runtime
processes can use the UUID for CUDA visibility even when CUDA numbers its
devices differently from the host inventory.

The Work panel and the `hub_work` tool associate reservations with the job ID
included in their purpose. A job using several cards exposes its reservations
under `leases`.

When the Hub starts a local CUDA ComfyUI service it reserves its startup footprint before launching it on
the assigned GPU. The default is 512 MiB, configurable as `comfyui.startup_vram_mb` in `backends.json`.
Automatic selection uses the arbiter's uncommitted VRAM, skips protected GPUs and prefers cards without a
managed renderer. An explicit index keeps the request on that card.

To use an existing manual grant, pass its `lease_id` to `hub_service_start` for a new local CUDA service.
The grant follows that process until it exits. A Prospero job on that endpoint uses the same reservation.
During a job the arbiter accounts for the larger of the service and job budgets and queues further jobs for
that endpoint until the current one releases its share. `hub_lease_status` shows the active job under `work`
on the service's reservation. When a job starts a cold endpoint, the startup uses the job's existing grant.
An already-running service keeps its ownership. The start result includes `undo` to stop the newly started
process by its service ID and PID.

Call `hub_work_link_task` with `app`, `job_id`, `task_id` and, for tasks outside
the default space, `space_id`. If the app reports a failure, the task thread
receives the job ID and error. A recently failed job can also be linked after
the failure. The reply includes the current and previous links. To undo a
link, send the same app and job ID with `task_id: null`. Existing notices
remain in the thread history.
