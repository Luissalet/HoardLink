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
The reservation lasts until the service process exits and remains separate from the model job's reservation.
An already-running service keeps its existing ownership. Use the returned `undo` to stop the newly started
process by its service ID and PID.

Call `hub_work_link_task` with `app`, `job_id`, `task_id` and, for tasks outside
the default space, `space_id`. If the app reports a failure, the task thread
receives the job ID and error. A recently failed job can also be linked after
the failure. The reply includes the current and previous links. To undo a
link, send the same app and job ID with `task_id: null`. Existing notices
remain in the thread history.
