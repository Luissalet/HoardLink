"""Browser lanes: one holder at a time per site (and per browser profile), so two apps never automate the same shop at once.

The GPU lease arbiter (:mod:`.lease`) stops two apps from loading models into the same VRAM; this one stops two apps from
driving a browser against the same site, or from sharing one Edge profile, at the same moment. The hub is the one
long-lived local process every app can reach, so it holds the lanes:

* a **lane** is keyed by the registrable root domain (``shop.example.co.uk`` -> ``example.co.uk``) and, when the caller names
  one, by the browser profile folder. A request for both takes both keys or neither;
* ``acquire`` gives the lane to the caller, or waits up to ``wait_s`` for it (waiters are served in arrival order, and a
  waiter never overtakes an earlier one for the same key) and then answers *busy, held by X since T*;
* a lane is held for ``ttl_s`` and kept by ``renew`` (the client's heartbeat); a holder that crashes loses it when the time runs
  out, or at once when its process (``pid``) is gone. Only the holder can renew or release it (``force`` is for the hub UI);
* a lane belongs to the **owner** that took it (the family token's owner, set by the server): only that owner can renew or
  release it, whatever holder name it claims. A waiter that comes back after a busy answer keeps its place in the queue with
  the ``ticket`` the hub gave it (server-issued, bound to its owner and holder, kept for a short grace period);
* the granted lanes are written to ``<data>/browser-lanes.json`` so a restarted hub still knows who holds what.

Events on the bus: ``hub.lane.acquired``, ``hub.lane.released`` and ``hub.lane.expired`` (``reason`` says ttl or process gone).

Standard library only; ``psutil`` (optional) for the pid checks.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, Optional

from ..browser_lane import profile_key, registrable_domain
from .lease import pid_alive, pid_create_time

log = logging.getLogger(__name__)

DEFAULT_TTL_S = 300.0
MIN_TTL_S = 1.0
MAX_TTL_S = 6 * 3600.0
#: One acquire call never blocks longer than this; a client that wants more simply asks again.
MAX_WAIT_S = 30.0
#: How long a waiter that was told "busy, ask again" keeps its place in the queue (it is polling, not gone).
TICKET_GRACE_S = 20.0


class LaneError(ValueError):
    pass


@dataclass
class LaneLease:
    id: str
    holder: str
    purpose: str
    domain: str
    profile: str
    keys: list
    ttl_s: float
    created_at: float
    granted_at: float
    expires_at: float
    pid: Optional[int] = None
    pid_created: Optional[float] = None
    owner: str = ""          # who may renew / release it: the token owner that took it ("" = not bound, legacy)

    def to_dict(self, now: Optional[float] = None, ids: bool = True) -> dict[str, Any]:
        d = asdict(self)
        d["lease_id"] = self.id
        d["since"] = self.granted_at
        if not ids:
            d.pop("id", None)
            d.pop("lease_id", None)
        if now is not None:
            d["expires_in_s"] = max(0, int(self.expires_at - now))
            d["held_for_s"] = max(0, int(now - self.granted_at))
        return d


@dataclass
class _Waiter:
    seq: int
    keys: list
    holder: str
    domain: str
    profile: str
    purpose: str
    since: float
    owner: str = ""
    ticket: str = ""
    parked_until: float = 0.0     # > 0: told "busy" and not waiting right now; its place is kept until then


class LaneArbiter:
    """Thread-safe; every public method reaps first, so the arbiter needs no background thread of its own."""

    def __init__(self, path: Optional[str] = None, *, now: Callable[[], float] = time.time,
                 alive: Callable[[int, Optional[float]], bool] = pid_alive):
        self.path = path
        self._now = now
        self._alive = alive
        self._cond = threading.Condition(threading.RLock())
        self._leases: dict[str, LaneLease] = {}
        self._waiters: list[_Waiter] = []
        self._seq = 0
        self.reaped: list[dict[str, Any]] = []   # last few expiries, for the UI
        #: Optional ``(kind, lane_dict)`` callback: "acquired" / "released" / "expired".
        self.on_event: Optional[Callable[[str, dict[str, Any]], None]] = None
        self._load()

    # -- events / persistence -------------------------------------------------
    def _notify(self, kind: str, lease: LaneLease, **extra: Any) -> None:
        cb = self.on_event
        if cb is None:
            return
        try:
            d = lease.to_dict(self._now())
            d.update(extra)
            cb(kind, d)
        except Exception:  # noqa: BLE001
            pass

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
        fields = set(LaneLease.__dataclass_fields__)
        for item in raw.get("lanes") or []:
            if not isinstance(item, dict):
                continue
            try:
                lease = LaneLease(**{k: v for k, v in item.items() if k in fields})
            except TypeError:
                continue
            self._leases[lease.id] = lease
        with self._cond:
            self._reap(save=True)

    def _save(self) -> None:
        if not self.path:
            return
        payload = {"saved_at": self._now(), "lanes": [asdict(l) for l in self._ordered()]}
        tmp = self.path + ".tmp"
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=1)
            os.replace(tmp, self.path)
        except OSError:
            pass

    # -- internals ------------------------------------------------------------
    def _ordered(self) -> list[LaneLease]:
        return sorted(self._leases.values(), key=lambda l: l.granted_at)

    def _reap(self, save: bool = False) -> bool:
        now = self._now()
        gone: list[tuple[LaneLease, str]] = []
        for lease in list(self._leases.values()):
            if lease.expires_at <= now:
                gone.append((lease, "ttl"))
            elif lease.pid and not self._alive(lease.pid, lease.pid_created):
                gone.append((lease, "owner process gone"))
        for w in [x for x in self._waiters if x.parked_until and x.parked_until <= now]:
            self._waiters.remove(w)                       # a parked waiter that never came back
            self._cond.notify_all()
        for lease, why in gone:
            self._leases.pop(lease.id, None)
            self.reaped = (self.reaped + [{"lease_id": lease.id, "holder": lease.holder, "domain": lease.domain,
                                           "profile": lease.profile, "reason": why, "at": now}])[-20:]
            self._notify("expired", lease, reason=why)
        if gone:
            self._cond.notify_all()
        if gone or save:
            self._save()
        return bool(gone)

    def _holder_of(self, keys: list) -> Optional[LaneLease]:
        for lease in self._ordered():
            if any(k in lease.keys for k in keys):
                return lease
        return None

    def _blocked_by_earlier(self, w: _Waiter) -> Optional[_Waiter]:
        for other in self._waiters:
            if other.seq < w.seq and any(k in other.keys for k in w.keys):
                return other
        return None

    @staticmethod
    def _keys(domain: str, profile: str) -> tuple[str, str, list]:
        root = registrable_domain(str(domain or ""))
        prof = profile_key(str(profile or ""))
        if not root and not prof:
            raise LaneError("a lane needs a domain (or URL) or a browser profile folder")
        keys = []
        if root:
            keys.append("domain:" + root)
        if prof:
            keys.append("profile:" + prof)
        return root, prof, keys

    def _busy(self, w: _Waiter, blocking: Optional[LaneLease], started: float) -> dict[str, Any]:
        now = self._now()
        out: dict[str, Any] = {"ok": True, "granted": False, "busy": True, "domain": w.domain, "profile": w.profile,
                               "keys": list(w.keys), "queue_seq": w.seq, "waited_s": round(time.monotonic() - started, 3),
                               "waiting": len(self._waiters) - 1}
        if blocking is not None:
            out.update(held_by=blocking.holder, since=blocking.granted_at, purpose=blocking.purpose,
                       expires_at=blocking.expires_at, expires_in_s=max(0, int(blocking.expires_at - now)))
        else:
            ahead = self._blocked_by_earlier(w)
            out.update(held_by=ahead.holder if ahead else "", since=ahead.since if ahead else None,
                       purpose=ahead.purpose if ahead else "", queued_ahead=True)
        return out

    # -- public API -------------------------------------------------------------
    def acquire(self, domain: str = "", holder: str = "", purpose: str = "", ttl_s: Any = None, wait_s: Any = 0,
                profile: str = "", pid: Any = None, queue_seq: Any = None, owner: str = "", ticket: Any = None,
                final: bool = True, max_ttl_s: Optional[float] = None) -> dict[str, Any]:
        """Take the lane for ``domain`` (a URL or host) and/or ``profile``; wait up to ``wait_s`` (capped at 30 s) for it.

        ``owner`` is who the server authenticated (only it may renew / release the lane). With ``final=False`` a *busy*
        answer carries a ``ticket``: asking again with it keeps the caller's place in the queue for ``TICKET_GRACE_S``
        (the hub issues and checks it; ``queue_seq`` from the caller is ignored). ``final=True`` leaves the queue at once."""
        root, prof, keys = self._keys(domain, profile)
        try:
            ttl = float(ttl_s) if ttl_s is not None else DEFAULT_TTL_S
        except (TypeError, ValueError):
            raise LaneError("ttl_s must be a number of seconds") from None
        ttl = max(MIN_TTL_S, min(ttl, MAX_TTL_S, max_ttl_s or MAX_TTL_S))
        try:
            wait = max(0.0, min(float(wait_s or 0), MAX_WAIT_S))
        except (TypeError, ValueError):
            raise LaneError("wait_s must be a number of seconds") from None
        pid_i: Optional[int] = None
        created: Optional[float] = None
        if pid not in (None, "", 0):
            try:
                pid_i = int(pid)
            except (TypeError, ValueError):
                raise LaneError("pid must be an integer") from None
            created = pid_create_time(pid_i)
            if not self._alive(pid_i, created):
                return {"ok": False, "error": f"owner process {pid_i} is not running", "status": 404}
        who = str(holder or "unknown")[:80]
        owner = str(owner or "")
        started = time.monotonic()
        deadline = started + wait
        with self._cond:
            self._reap()
            w = self._claim_ticket(ticket, owner, who, keys)
            if w is None:
                self._seq += 1
                w = _Waiter(seq=self._seq, keys=keys, holder=who, domain=root, profile=prof, owner=owner,
                            purpose=str(purpose or "")[:160], since=self._now(), ticket=secrets.token_hex(8))
                self._waiters.append(w)
                self._waiters.sort(key=lambda x: x.seq)
            w.parked_until = 0.0
            parked = False
            try:
                while True:
                    self._reap()
                    blocking = self._holder_of(keys)
                    if blocking is None and self._blocked_by_earlier(w) is None:
                        return self._grant(w, ttl, pid_i, created, started)
                    left = deadline - time.monotonic()
                    if left <= 0:
                        out = self._busy(w, blocking, started)
                        if not final:
                            w.parked_until = self._now() + TICKET_GRACE_S      # keep its place for the next poll
                            out["ticket"] = w.ticket
                            parked = True
                        return out
                    self._cond.wait(timeout=min(1.0, left))     # re-check expiry every second
            finally:
                if not parked:
                    try:
                        self._waiters.remove(w)
                    except ValueError:
                        pass
                self._cond.notify_all()

    def _claim_ticket(self, ticket: Any, owner: str, holder: str, keys: list) -> Optional[_Waiter]:
        """The parked waiter this ticket was issued to, if it is still parked and the caller is the same owner / holder / lane."""
        if not ticket:
            return None
        for w in self._waiters:
            if w.parked_until and w.ticket == str(ticket):
                if w.owner == owner and w.holder == holder and w.keys == keys:
                    return w
                return None
        return None

    def _grant(self, w: _Waiter, ttl: float, pid: Optional[int], created: Optional[float], started: float) -> dict[str, Any]:
        now = self._now()
        lease = LaneLease(id=secrets.token_hex(8), holder=w.holder, purpose=w.purpose, domain=w.domain, profile=w.profile,
                          keys=list(w.keys), ttl_s=ttl, created_at=w.since, granted_at=now, expires_at=now + ttl,
                          pid=pid, pid_created=created, owner=w.owner)
        self._leases[lease.id] = lease
        self._save()
        self._notify("acquired", lease, waited_s=round(time.monotonic() - started, 3))
        return {"ok": True, "granted": True, "busy": False, "lease_id": lease.id, "domain": w.domain, "profile": w.profile,
                "expires_at": lease.expires_at, "waited_s": round(time.monotonic() - started, 3), "lane": lease.to_dict(now)}

    def _refuse(self, lease: LaneLease, holder: Optional[str], what: str) -> dict[str, Any]:
        return {"ok": False, "status": 403,
                "error": f"lane {lease.domain or lease.profile} is held by {lease.holder}, not {holder or 'an unnamed caller'}: "
                         f"{what} refused", "held_by": lease.holder}

    @staticmethod
    def _not_owner(lease: LaneLease, owner: Optional[str]) -> bool:
        """True when the caller was authenticated as someone other than the owner the lane is bound to."""
        return owner is not None and bool(lease.owner) and str(owner) != lease.owner

    def renew(self, lease_id: str, holder: Optional[str] = None, ttl_s: Any = None,
              owner: Optional[str] = None, max_ttl_s: Optional[float] = None) -> dict[str, Any]:
        with self._cond:
            self._reap()
            lease = self._leases.get(str(lease_id or ""))
            if lease is None:
                return {"ok": False, "error": "unknown or expired lane", "lease_id": lease_id, "status": 404}
            if (holder is not None and str(holder)[:80] != lease.holder) or self._not_owner(lease, owner):
                return self._refuse(lease, holder, "renew")
            if ttl_s is not None:
                try:
                    lease.ttl_s = max(MIN_TTL_S, min(float(ttl_s), MAX_TTL_S, max_ttl_s or MAX_TTL_S))
                except (TypeError, ValueError):
                    raise LaneError("ttl_s must be a number of seconds") from None
            lease.expires_at = self._now() + lease.ttl_s
            self._save()
            return {"ok": True, "lease_id": lease.id, "expires_at": lease.expires_at, "lane": lease.to_dict(self._now())}

    def release(self, lease_id: str, holder: Optional[str] = None, force: bool = False,
                owner: Optional[str] = None) -> dict[str, Any]:
        """Give the lane back. Only its holder (and owner) may; ``force`` is the hub UI's override."""
        with self._cond:
            self._reap()
            lease = self._leases.get(str(lease_id or ""))
            if lease is None:
                return {"ok": True, "released": False, "lease_id": lease_id, "detail": "already gone"}
            if not force and (holder is None or str(holder)[:80] != lease.holder or self._not_owner(lease, owner)):
                return self._refuse(lease, holder, "release")
            self._leases.pop(lease.id, None)
            self._save()
            self._cond.notify_all()
            self._notify("released", lease, forced=bool(force), held_s=max(0, int(self._now() - lease.granted_at)))
            return {"ok": True, "released": True, "lease_id": lease.id}

    def status(self, ids: bool = True) -> dict[str, Any]:
        """The lanes in use and who waits. ``ids=False`` leaves the lease ids out (what an anonymous reader sees)."""
        with self._cond:
            self._reap()
            now = self._now()
            return {"ok": True, "lanes": [l.to_dict(now, ids) for l in self._ordered()],
                    "waiting": [{"holder": w.holder, "domain": w.domain, "profile": w.profile, "purpose": w.purpose,
                                 "since": w.since, "waiting_s": max(0, int(now - w.since))} for w in self._waiters],
                    "reaped": [{k: v for k, v in r.items() if ids or k != "lease_id"} for r in self.reaped[-10:]],
                    "checked_at": now}
