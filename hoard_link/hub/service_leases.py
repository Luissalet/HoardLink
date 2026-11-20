"""Reserve the CUDA startup footprint of a managed ComfyUI process."""

from .lease import LeaseError, parse_gpu_request


def comfy_repository(launcher):
    """A configured ComfyUI checkout is also a station's source repository."""
    folder, _, _ = launcher.comfy_install()
    if folder and (folder / ".git").exists():
        return {"name": folder.name, "path": str(folder), "app": "comfyui"}
    return None


def start_comfy(launcher, leases, service_id, gpu, wait_s):
    with launcher._lock:
        try:
            service = launcher.get(service_id)
        except KeyError:
            return launcher.start(service_id, gpu=gpu, wait_s=wait_s)
        current = launcher.status(service)
        if current["state"] in ("running", "starting") or not service.argv or "--cpu" in service.argv:
            return launcher.start(service_id, gpu=gpu, wait_s=wait_s)

        config = launcher.config().get("comfyui") or {}
        wanted = gpu if gpu is not None else config.get("gpu", "auto")
        lease_id = None
        bound = False
        try:
            wanted = "any" if wanted in (None, "", "auto") else parse_gpu_request(wanted)
            if isinstance(wanted, list):
                raise LeaseError("ComfyUI needs 'auto' or a single GPU index")
            if wanted == "any":
                preferred = launcher.pick_gpu(set(leases.protected_gpus))
                if preferred is not None and preferred not in leases.protected_gpus:
                    wanted = preferred
            reserved = leases.request(owner="hub-service", purpose=service.id,
                                      vram_mb=config.get("startup_vram_mb", 512), gpu=wanted, ttl_s=300)
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
                        lease = leases.bind_process(lease_id, result["pid"])
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
            if lease_id and not bound:
                leases.release(lease_id)
    if wait_s > 0 and result.get("ok") and result.get("state") != "running":
        result.update(launcher._wait(service, wait_s))
        if not result.get("ok") and bound:
            leases.status()  # reap the exited process
    return result
