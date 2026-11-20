"""Reserve the CUDA startup footprint of a managed ComfyUI process."""

from .lease import LeaseError, parse_gpu_request


def comfy_repository(launcher):
    """A configured ComfyUI checkout is also a station's source repository."""
    folder, _, _ = launcher.comfy_install()
    if folder and (folder / ".git").exists():
        return {"name": folder.name, "path": str(folder), "app": "comfyui"}
    return None


def start_comfy(launcher, leases, service_id, gpu, wait_s, *, lease_id=None, work_lease_id=None):
    with launcher._lock:
        try:
            service = launcher.get(service_id)
        except KeyError:
            return launcher.start(service_id, gpu=gpu, wait_s=wait_s)
        current = launcher.status(service)
        running = current["state"] in ("running", "starting")
        if lease_id and running:
            existing = leases.get(lease_id).get("lease", {})
            if (existing.get("process_bound") and existing.get("purpose") == service.id
                    and existing.get("pid") == current.get("pid")
                    and gpu in (None, "", "auto", "any", existing["gpu"], str(existing["gpu"]))):
                result = launcher.start(service_id, gpu=existing["gpu"], wait_s=wait_s)
                return {**result, "lease_id": lease_id, "gpu": existing["gpu"],
                        "undo": {"tool": "hub_service_stop",
                                 "arguments": {"id": service.id, "expected_pid": current["pid"]}}}
        if lease_id and (running or not service.argv or "--cpu" in service.argv):
            return {"ok": False, "service": service.id,
                    "error": "A lease can only be attached to a new CUDA service / La reserva requiere un servicio CUDA nuevo"}
        if running or not service.argv or "--cpu" in service.argv:
            return launcher.start(service_id, gpu=gpu, wait_s=wait_s)

        config = launcher.config().get("comfyui") or {}
        wanted = gpu if gpu is not None else ("auto" if lease_id or work_lease_id else config.get("gpu", "auto"))
        supplied_lease = lease_id
        bound = False
        try:
            wanted = "any" if wanted in (None, "", "auto") else parse_gpu_request(wanted)
            if isinstance(wanted, list):
                raise LeaseError("ComfyUI needs 'auto' or a single GPU index")
            footprint = config.get("startup_vram_mb", 512)
            if work_lease_id and supplied_lease:
                raise LeaseError("Supply either a service lease or a job lease")
            if work_lease_id:
                reserved = leases.reserve_service_for_work(work_lease_id, service.id, footprint, wanted)
            elif supplied_lease:
                reserved = leases.get(supplied_lease)
                if (reserved.get("state") != "granted" or reserved.get("gpu") is None
                        or reserved["lease"]["process_bound"] or reserved["lease"]["service_lease"]
                        or wanted not in ("any", reserved["gpu"])):
                    raise LeaseError("Use a granted, unattached lease on the service GPU / Usa una reserva libre en la GPU del servicio")
            else:
                reserved = leases.request(owner="hub-service", purpose=service.id, gpu=wanted, ttl_s=300,
                                          vram_mb=footprint,
                                          avoid_gpus=tuple(launcher.comfy_gpus()) if wanted == "any" else ())
            lease_id = reserved.get("lease_id")
            if reserved.get("state") != "granted":
                return {"ok": False, "service": service.id, "error": reserved.get("error") or
                        "GPU memory is unavailable for this service / No hay memoria de GPU para este servicio"}
            chosen = reserved["gpu"]
            if chosen is not None:
                command = launcher.get(service_id, gpu=chosen).argv
                if command.count("--cuda-device") != 1 or any(
                    arg.startswith("--cuda-device=") for arg in command
                ):
                    raise LeaseError("The ComfyUI command must use the GPU assigned by the Hub")
            result = launcher.start(service_id, gpu=chosen, wait_s=0)
            if result.get("ok") and not result.get("already"):
                if chosen is not None:
                    try:
                        lease = leases.bind_process(lease_id, result["pid"], service=service.id)
                    except LeaseError:
                        launcher.stop(service_id)
                        raise
                    bound = True
                    result["lease_id"] = lease["lease_id"]
                result["undo"] = {"tool": "hub_service_stop",
                                  "arguments": {"id": service.id, "expected_pid": result["pid"]}}
        except LeaseError as exc:
            return {"ok": False, "service": service.id, "error": str(exc)}
        finally:
            if lease_id and not bound and not supplied_lease:
                leases.release(lease_id)
    if wait_s > 0 and result.get("ok") and result.get("state") != "running":
        result.update(launcher._wait(service, wait_s))
        if not result.get("ok") and bound:
            leases.status()  # reap the exited process
    return result
