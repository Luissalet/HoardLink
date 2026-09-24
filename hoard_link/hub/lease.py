"""GPU memory leases: one arbiter for every app that wants VRAM.

Several local programs compete for the same GPUs (a llama-server, Ollama
loading a model, ComfyUI renders, a speech-to-text model, an image
embedder). When each of them peeks at ``nvidia-smi`` on its own, two can
see the same free gigabytes at the same moment and both load — and one of
them runs out of memory. The hub is already the one long-lived local
process every app can reach, so it holds the single queue:

* an app asks for ``vram_mb`` (on a given GPU or any), with a priority and
  a time-to-live, and gets a lease that is either **granted** (with the GPU
  it was placed on) or **queued** (with its position);
* while it holds the lease it renews it; when it is done it releases it.
  A lease that is not renewed expires (TTL), and a lease whose owner
  process has died (``pid``) is reaped, so a crashed app never blocks the
  queue forever;
* leases are written to ``<data>/leases.json`` so a restarted hub still
  knows what it granted.

Accounting, per GPU::

    used_effective = max(smi_used, base_used + reserved)
    available      = total - used_effective - headroom

``reserved`` is the sum of the granted leases on that GPU and
``base_used`` is what ``nvidia-smi`` reported the last time that GPU had
no lease on it (memory held by programs that do not take leases). The
``max`` is what keeps two reservations from being double-booked before
either model is loaded, *without* counting a lease twice once its model
really sits in memory (then ``smi_used`` already includes it). Memory a
lease-less program grabs later shows up in ``smi_used`` and is respected
too.

Queue order is priority (higher first), then arrival. A queued lease that
does not fit holds back every later lease that could use the same GPU,
so a large request is not starved by a stream of small ones; a request
pinned to another GPU still goes ahead.

Standard library only; ``psutil`` (optional) for the pid checks.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, Optional

DEFAULT_TTL_S = 1800
MIN_TTL_S = 5
MAX_TTL_S = 7 * 24 * 3600
#: A queued lease must be polled (request with ``lease_id``, renew, or get)
#: at least this often or it leaves the queue: its owner is gone.
QUEUE_KEEPALIVE_S = 90
MAX_WAIT_S = 25.0
INVENTORY_CACHE_S = 2.0
DEFAULT_HEADROOM_MB = 256


@dataclass
class Lease:
    id: str
    owner: str
    purpose: str
    vram_mb: int
    gpu_request: Any                 # "any" or an int
    priority: int
    ttl_s: int
    state: str                       # granted | queued
    created_at: float
    expires_at: float
    seq: int
    gpu: Optional[int] = None        # assigned GPU once granted
    granted_at: Optional[float] = None
    pid: Optional[int] = None
    pid_created: Optional[float] = None
    note: str = ""

    def to_dict(self, now: Optional[float] = None) -> dict[str, Any]:
        d = asdict(self)
        d["lease_id"] = self.id
        if now is not None:
            d["expires_in_s"] = max(0, int(self.expires_at - now))
        return d


@dataclass
class _Gpu:
    index: int
    total_mb: int
    used_mb: int


def _default_gpu_fn() -> list[Any]:
    from hoard_link.gpu import gpu_free_mb
    return gpu_free_mb()


def _psutil():
    try:
        import psutil  # type: ignore
        return psutil
    except Exception:  # noqa: BLE001
        return None


def pid_create_time(pid: int) -> Optional[float]:
    ps = _psutil()
    if ps is None:
        return None
    try:
        return float(ps.Process(pid).create_time())
    except Exception:  # noqa: BLE001
        return None


def pid_alive(pid: int, created: Optional[float] = None) -> bool:
    """True when ``pid`` exists (and, when ``created`` is known, is still
    the same process: pids are recycled). Unknown (no psutil) counts as
    alive — never reap on a guess."""
    ps = _psutil()
    if ps is None:
        return True
    try:
        if not ps.pid_exists(pid):
            return False
        if created is not None:
            return abs(ps.Process(pid).create_time() - created) < 2.0
        return True
    except Exception:  # noqa: BLE001
        return True


class LeaseError(ValueError):
    pass


class LeaseArbiter:
    """Thread-safe; every public method reaps and schedules first, so the
    arbiter needs no background thread of its own."""

    def __init__(
        self,
        path: Optional[str] = None,
        *,
        gpu_fn: Optional[Callable[[], list[Any]]] = None,
        headroom_mb: int = DEFAULT_HEADROOM_MB,
        now: Callable[[], float] = time.time,
        alive: Callable[[int, Optional[float]], bool] = pid_alive,
        cache_s: float = INVENTORY_CACHE_S,
    ):
        self.path = path
        self._gpu_fn = gpu_fn or _default_gpu_fn
        self.headroom_mb = max(0, int(headroom_mb))
        self._now = now
        self._alive = alive
        self._cache_s = cache_s
        self._cond = threading.Condition(threading.RLock())
        self._leases: dict[str, Lease] = {}
        self._base_used: dict[int, int] = {}
        self._seq = 0
        self._inv: tuple[float, list[_Gpu]] = (-1e18, [])
        self.reaped: list[dict[str, Any]] = []   # last few reaps, for the UI
        self._load()

    # -- inventory ---------------------------------------------------------
    def inventory(self, force: bool = False) -> list[_Gpu]:
        ts, gpus = self._inv
        now = self._now()
        if not force and now - ts < self._cache_s:
            return gpus
        out: list[_Gpu] = []
        try:
            for g in self._gpu_fn() or []:
                out.append(_Gpu(int(getattr(g, "index")), int(getattr(g, "total_mb")), int(getattr(g, "used_mb"))))
        except Exception:  # noqa: BLE001
            out = []
        self._inv = (now, out)
        return out

    def _reserved(self, index: int) -> int:
        return sum(l.vram_mb for l in self._leases.values() if l.state == "granted" and l.gpu == index)

    def _available(self, g: _Gpu, extra_reserved: int = 0) -> int:
        reserved = self._reserved(g.index) + extra_reserved
        base = self._base_used.get(g.index, g.used_mb)
        used = max(g.used_mb, base + reserved)
        return g.total_mb - used - self.headroom_mb

    def _refresh_bases(self, gpus: list[_Gpu]) -> None:
        for g in gpus:
            if self._reserved(g.index) == 0:
                self._base_used[g.index] = g.used_mb

    # -- persistence -------------------------------------------------------
    def _load(self) -> None:
        if not self.path or not os.path.isfile(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        fields = set(Lease.__dataclass_fields__)
        for item in raw.get("leases") or []:
            if not isinstance(item, dict):
                continue
            try:
                lease = Lease(**{k: v for k, v in item.items() if k in fields})
            except TypeError:
                continue
            self._leases[lease.id] = lease
            self._seq = max(self._seq, int(lease.seq))
        for k, v in (raw.get("base_used") or {}).items():
            try:
                self._base_used[int(k)] = int(v)
            except (TypeError, ValueError):
                pass
        with self._cond:
            self._tick(save=True)

    def _save(self) -> None:
        if not self.path:
            return
        payload = {
            "saved_at": self._now(),
            "leases": [asdict(l) for l in self._ordered()],
            "base_used": {str(k): v for k, v in self._base_used.items()},
        }
        tmp = self.path + ".tmp"
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=1)
            os.replace(tmp, self.path)
        except OSError:
            pass

    # -- scheduling --------------------------------------------------------
    def _ordered(self) -> list[Lease]:
        return sorted(self._leases.values(), key=lambda l: (l.state != "granted", -l.priority, l.seq))

    def _queue(self) -> list[Lease]:
        return sorted((l for l in self._leases.values() if l.state == "queued"), key=lambda l: (-l.priority, l.seq))

    def _reap(self) -> bool:
        now = self._now()
        gone: list[tuple[Lease, str]] = []
        for lease in list(self._leases.values()):
            if lease.expires_at <= now:
                gone.append((lease, "expired" if lease.state == "granted" else "abandoned in queue"))
            elif lease.pid and not self._alive(lease.pid, lease.pid_created):
                gone.append((lease, f"owner process {lease.pid} is gone"))
        for lease, why in gone:
            self._leases.pop(lease.id, None)
            self.reaped = (self.reaped + [{"lease_id": lease.id, "owner": lease.owner, "purpose": lease.purpose,
                                           "reason": why, "at": now}])[-20:]
        return bool(gone)

    def _candidates(self, lease: Lease, gpus: list[_Gpu]) -> list[_Gpu]:
        if lease.gpu_request == "any":
            return list(gpus)
        return [g for g in gpus if g.index == lease.gpu_request]

    def _schedule(self, gpus: list[_Gpu]) -> bool:
        changed = False
        blocked: set[int] = set()
        now = self._now()
        for lease in self._queue():
            if not gpus:
                # No inventory (no NVIDIA GPU, or nvidia-smi missing): nothing
                # to arbitrate against; grant so apps are never stuck.
                self._grant(lease, None, now, "no GPU inventory; granted without a memory check")
                changed = True
                continue
            cands = [g for g in self._candidates(lease, gpus) if g.index not in blocked]
            fits = [(self._available(g), g) for g in cands]
            fits = [(avail, g) for avail, g in fits if avail >= lease.vram_mb]
            if fits:
                fits.sort(key=lambda ag: (-ag[0], ag[1].index))  # most room first: spreads the load
                self._grant(lease, fits[0][1].index, now)
                changed = True
            else:
                blocked.update(g.index for g in self._candidates(lease, gpus))
        return changed

    def _grant(self, lease: Lease, gpu: Optional[int], now: float, note: str = "") -> None:
        # base_used for this GPU was refreshed by _tick just before scheduling.
        lease.state = "granted"
        lease.gpu = gpu
        lease.granted_at = now
        lease.expires_at = now + lease.ttl_s
        lease.note = note
        self._cond.notify_all()

    def _tick(self, save: bool = False, force_inventory: bool = False) -> list[_Gpu]:
        changed = self._reap()
        gpus = self.inventory(force=force_inventory or changed)
        self._refresh_bases(gpus)
        changed = self._schedule(gpus) or changed
        if changed or save:
            self._save()
        return gpus

    def _position(self, lease: Lease) -> int:
        if lease.state != "queued":
            return 0
        for i, l in enumerate(self._queue(), 1):
            if l.id == lease.id:
                return i
        return 0

    def _reply(self, lease: Lease) -> dict[str, Any]:
        return {"ok": True, "lease_id": lease.id, "state": lease.state, "gpu": lease.gpu,
                "expires_at": lease.expires_at, "position": self._position(lease),
                "lease": lease.to_dict(self._now())}

    # -- public API ----------------------------------------------------------
    def request(self, owner: str = "", purpose: str = "", vram_mb: int = 0, gpu: Any = None, priority: int = 0,
                ttl_s: Optional[float] = None, wait: bool = False, pid: Optional[int] = None,
                lease_id: Optional[str] = None, wait_s: Optional[float] = None) -> dict[str, Any]:
        """New lease, or (with ``lease_id``) the current state of one that is
        already queued — that is how a client keeps waiting without losing
        its place. ``wait`` long-polls up to 25 s for a grant."""
        with self._cond:
            gpus = self._tick()
            if lease_id:
                lease = self._leases.get(str(lease_id))
                if lease is None:
                    return {"ok": False, "error": "unknown or expired lease", "lease_id": lease_id, "status": 404}
                if lease.state == "queued":
                    lease.expires_at = self._now() + QUEUE_KEEPALIVE_S
            else:
                lease = self._new(owner, purpose, vram_mb, gpu, priority, ttl_s, pid, gpus)
                self._leases[lease.id] = lease
                self._tick(save=True)
                if lease.id not in self._leases:
                    return {"ok": False, "error": f"owner process {lease.pid} is not running", "lease_id": lease.id,
                            "status": 404}
            if wait and lease.state == "queued":
                limit = MAX_WAIT_S if wait_s is None else max(0.0, min(float(wait_s), MAX_WAIT_S))
                deadline = time.monotonic() + limit
                while lease.state == "queued" and lease.id in self._leases:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        break
                    self._cond.wait(timeout=min(1.0, left))  # re-check memory every second
                    self._tick()
                if lease.id not in self._leases:
                    return {"ok": False, "error": "lease was released or reaped while waiting", "lease_id": lease.id,
                            "status": 404}
                if lease.state == "queued":
                    lease.expires_at = self._now() + QUEUE_KEEPALIVE_S
            return self._reply(lease)

    def _new(self, owner: str, purpose: str, vram_mb: Any, gpu: Any, priority: Any, ttl_s: Any, pid: Any,
             gpus: list[_Gpu]) -> Lease:
        try:
            vram = int(vram_mb)
        except (TypeError, ValueError):
            raise LeaseError("vram_mb must be an integer number of MiB") from None
        if vram < 0:
            raise LeaseError("vram_mb must be >= 0")
        if gpu is None or (isinstance(gpu, str) and gpu.strip().lower() in ("", "any", "auto")):
            gpu_req: Any = "any"
        else:
            try:
                gpu_req = int(gpu)
            except (TypeError, ValueError):
                raise LeaseError("gpu must be a GPU index or 'any'") from None
            if gpus and gpu_req not in {g.index for g in gpus}:
                raise LeaseError(f"no GPU with index {gpu_req} (have {sorted(g.index for g in gpus)})")
        if gpus:
            pool = gpus if gpu_req == "any" else [g for g in gpus if g.index == gpu_req]
            biggest = max(g.total_mb - self.headroom_mb for g in pool)
            if vram > biggest:
                raise LeaseError(f"{vram} MiB can never fit: the largest eligible GPU has {biggest} MiB")
        try:
            prio = int(priority or 0)
        except (TypeError, ValueError):
            raise LeaseError("priority must be an integer") from None
        try:
            ttl = int(float(ttl_s)) if ttl_s is not None else DEFAULT_TTL_S
        except (TypeError, ValueError):
            raise LeaseError("ttl_s must be a number of seconds") from None
        ttl = max(MIN_TTL_S, min(ttl, MAX_TTL_S))
        pid_i: Optional[int] = None
        created: Optional[float] = None
        if pid not in (None, "", 0):
            try:
                pid_i = int(pid)
            except (TypeError, ValueError):
                raise LeaseError("pid must be an integer") from None
            created = pid_create_time(pid_i)
        self._seq += 1
        now = self._now()
        return Lease(id=secrets.token_hex(8), owner=str(owner or "unknown")[:80], purpose=str(purpose or "")[:160],
                     vram_mb=vram, gpu_request=gpu_req, priority=prio, ttl_s=ttl, state="queued",
                     created_at=now, expires_at=now + QUEUE_KEEPALIVE_S, seq=self._seq, pid=pid_i, pid_created=created)

    def renew(self, lease_id: str, ttl_s: Optional[float] = None) -> dict[str, Any]:
        with self._cond:
            self._tick()
            lease = self._leases.get(str(lease_id or ""))
            if lease is None:
                return {"ok": False, "error": "unknown or expired lease", "lease_id": lease_id, "status": 404}
            now = self._now()
            if lease.state == "granted":
                if ttl_s is not None:
                    try:
                        lease.ttl_s = max(MIN_TTL_S, min(int(float(ttl_s)), MAX_TTL_S))
                    except (TypeError, ValueError):
                        raise LeaseError("ttl_s must be a number of seconds") from None
                lease.expires_at = now + lease.ttl_s
            else:
                lease.expires_at = now + QUEUE_KEEPALIVE_S
            self._save()
            return self._reply(lease)

    def release(self, lease_id: str) -> dict[str, Any]:
        with self._cond:
            lease = self._leases.pop(str(lease_id or ""), None)
            self._cond.notify_all()
            self._tick(save=True, force_inventory=True)
            if lease is None:
                return {"ok": True, "released": False, "lease_id": lease_id, "detail": "already gone"}
            return {"ok": True, "released": True, "lease_id": lease.id}

    def get(self, lease_id: str) -> dict[str, Any]:
        with self._cond:
            self._tick()
            lease = self._leases.get(str(lease_id or ""))
            if lease is None:
                return {"ok": False, "error": "unknown or expired lease", "lease_id": lease_id, "status": 404}
            if lease.state == "queued":
                lease.expires_at = self._now() + QUEUE_KEEPALIVE_S
            return self._reply(lease)

    def status(self, force: bool = False) -> dict[str, Any]:
        with self._cond:
            gpus = self._tick(force_inventory=force)
            now = self._now()
            per_gpu = []
            for g in gpus:
                reserved = self._reserved(g.index)
                per_gpu.append({
                    "index": g.index, "total_mb": g.total_mb, "used_mb": g.used_mb, "free_mb": g.total_mb - g.used_mb,
                    "reserved_mb": reserved, "base_used_mb": self._base_used.get(g.index, g.used_mb),
                    "available_mb": max(0, self._available(g)),
                    "leases": sum(1 for l in self._leases.values() if l.state == "granted" and l.gpu == g.index),
                })
            granted = [l.to_dict(now) for l in self._ordered() if l.state == "granted"]
            queue = [dict(l.to_dict(now), position=i) for i, l in enumerate(self._queue(), 1)]
            return {"ok": True, "gpus": per_gpu, "inventory": bool(gpus), "leases": granted, "queue": queue,
                    "headroom_mb": self.headroom_mb, "reaped": list(self.reaped[-10:]), "checked_at": self._inv[0]}
