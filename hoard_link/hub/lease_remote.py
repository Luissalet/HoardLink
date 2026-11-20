"""Let another Hub on this PC use the primary Hub's GPU queue."""

from __future__ import annotations

import threading
from urllib.parse import quote

from .._hubclient import DEFAULT_URL, fetch
from .lease import LeaseError, MAX_WAIT_S


class RemoteLeaseArbiter:
    def __init__(self, port: int, url: str = DEFAULT_URL):
        self.url = url.rstrip("/")
        self._scope = f"hub@{port}/"
        self._granted: set[str] = set()
        self._lock = threading.Lock()
        self.on_event = None

    def _call(self, path, body=None):
        status, result = fetch(f"{self.url}/api/lease{path}", body, timeout=MAX_WAIT_S + 5)
        if status is None or not isinstance(result, dict):
            return {"ok": False, "status": 503, "error": "GPU arbiter unavailable / Árbitro de GPU no disponible"}
        if status >= 400:
            return {**result, "status": status, "ok": False}
        return result

    def _service(self, name):
        return self._scope + name if name else None

    def _grant_event(self, result):
        lease = result.get("lease")
        if self.on_event and result.get("state") == "granted" and lease:
            uid = result["lease_id"]
            with self._lock:
                if uid in self._granted:
                    return result
                self._granted.add(uid)
            self.on_event("granted", lease)
        return result

    def request(self, owner="", purpose="", vram_mb=0, gpu=None, priority=0, ttl_s=None,
                wait=False, pid=None, lease_id=None, wait_s=None, *, service=None, avoid_gpus=()):
        return self._grant_event(self._call("/request", {
            "owner": owner, "purpose": purpose, "vram_mb": vram_mb, "gpu": gpu, "priority": priority,
            "ttl_s": ttl_s, "wait": wait, "pid": pid, "lease_id": lease_id, "wait_s": wait_s,
            "service": self._service(service), "avoid_gpus": avoid_gpus,
        }))

    def status(self, force=False):
        result = self._call("?force=1" if force else "")
        if not result.get("ok"):
            return {"inventory": False, "gpus": [], "leases": [], "queue": [], **result}
        return result

    def get(self, lease_id):
        result = self._call("/" + quote(str(lease_id), safe=""))
        lease = result.get("lease")
        if lease and lease["purpose"].startswith(self._scope):
            return {**result, "lease": {**lease, "purpose": lease["purpose"][len(self._scope):]}}
        return result

    def renew(self, lease_id, ttl_s=None):
        return self._call("/renew", {"lease_id": lease_id, "ttl_s": ttl_s})

    def release(self, lease_id):
        result = self._call("/release", {"lease_id": lease_id})
        if result.get("ok"):
            with self._lock:
                self._granted.discard(lease_id)
            if self.on_event:
                self.on_event("released", {"lease_id": lease_id})
        return result

    def _service_action(self, path, body):
        result = self._call(path, body)
        if result.get("status") == 404 and result.get("error") == "not found":
            return None
        if not result.get("ok"):
            raise LeaseError(result.get("error") or "The GPU arbiter refused the service reservation")
        return result

    def bind_process(self, lease_id, pid, *, service=None):
        bound = self._service_action("/bind-process", {"lease_id": lease_id, "pid": pid, "service": self._service(service)})
        if bound is not None:
            return bound
        # Older primaries support PID-owned grants but cannot attach a PID in place.
        # Overlap both grants until the new owner is recorded, then give the first back.
        lease = self.get(lease_id).get("lease") or {}
        if lease.get("state") != "granted":
            raise LeaseError("The service startup lease expired / Ha caducado la reserva de arranque")
        bound = self.request(owner="hub-service", purpose=self._service(service), gpu=lease["gpu"],
                             vram_mb=lease["vram_mb"], pid=pid, ttl_s=604800)
        self._require_granted(bound)
        self.release(lease_id)
        return bound

    def reserve_service_for_work(self, job_id, service, vram_mb, gpu):
        reserved = self._service_action("/reserve-service", {"lease_id": job_id, "service": self._service(service),
                                                             "vram_mb": vram_mb, "gpu": gpu})
        if reserved is not None:
            return reserved
        job = self.get(job_id).get("lease") or {}
        if (job.get("state") != "granted" or job.get("service_target") != self._service(service)
                or job.get("gpu") is None or gpu not in ("any", job["gpu"])):
            raise LeaseError("The job has no matching GPU reservation for this service")
        reserved = self.request(owner="hub-service", purpose=self._service(service), vram_mb=vram_mb, gpu=job["gpu"])
        self._require_granted(reserved)
        return reserved

    def _require_granted(self, result):
        if result.get("state") != "granted":
            if result.get("lease_id"):
                self.release(result["lease_id"])
            raise LeaseError(result.get("error") or "No GPU memory for service startup / No hay memoria de GPU para el arranque")
