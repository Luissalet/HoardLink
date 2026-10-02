"""Purchases — the ``purchases`` facet: the life of one purchase across the family.

A purchase starts as a payment Ledger read in the mail, becomes a parcel in Phileas, arrives, gets an invoice
in Kafka and a place at home in HomeHoard. Each app only sees its own step; the hub follows the events they
emit and keeps ONE row per purchase in ``<data>/purchases.db`` with the stage it has reached::

    paid -> shipped -> delivered -> filed -> stored -> closed

Events (ids in ``data``; the facet reads what it finds and ignores the rest):

* ``ledger.mail.recorded {tx_id, merchant, amount, currency, date, order_ref?, message_id?, items?}`` — paid
* ``phileas.shipment.new {shipment_id, merchant?, order_ref?, message_id?, items?, carrier, tracking_number}`` — shipped
* ``phileas.shipment.delivered {shipment_id, ..., delivered_at}`` and ``phileas.update`` with ``status: delivered``
* ``kafka.document.archived {doc_id, kind, merchant?, amount?, date?, order_ref?, message_id?}`` — filed
* ``kafka.warranty.created {doc_id, shipment_id?, merchant?, until}`` — a warranty on the purchase
* ``homehoard.item.created {item_id, source_ref?}`` — stored (``source_ref`` is ``hoard://hub/purchase/<id>``)
* ``tantalus.watcher.bought {watcher_id, purchase_ref}`` — the watcher that waited for it

Matching an event to a purchase, in this order: the purchase the event names (``source_ref``/``purchase_ref``),
a record of the event already linked, the same normalised ``order_ref``, the same ``message_id``, then a
fuzzy match — similar merchant, amount within 1 % (when both are known), dates within 5 days. Each match links
the app records in the ``refs`` facet (rel ``purchase``). Two actions, each once per purchase: a new payment
asks Tantalus to stop watching what was just bought, a delivery tells the person to put it somewhere
(link to HomeHoard's quick-add form).

HTTP: ``GET /api/purchases?stage=&q=&limit=``, ``GET /api/purchases/<id>``, ``POST /api/purchases/<id>/close``
(and ``/reopen``). Tools ``hub_purchases``, ``hub_purchase``. Events are handled on the facet's own worker
thread: nothing here ever blocks ``EventLog.emit``.
"""

from __future__ import annotations

import json
import os
import queue
import re
import sqlite3
import threading
import time
import unicodedata
from datetime import date, datetime, timedelta
from typing import Any, Callable, Optional
from urllib.parse import quote

from .facets import Facet, Request

STAGES = ("paid", "shipped", "delivered", "filed", "stored", "closed")
RANK = {s: i for i, s in enumerate(STAGES, 1)}
REF_ORDER = ("ledger", "phileas", "invoice", "receipt", "document", "warranty", "homehoard", "tantalus")
HOME_URL = "http://127.0.0.1:5196"
AMOUNT_TOLERANCE = 0.01
DATE_WINDOW_DAYS = 5
TANTALUS_MIN_SCORE = 0.8
ENRICH_EVERY_S = 600.0
INVOICE_KINDS = {"invoice", "factura", "receipt", "recibo", "ticket", "order", "pedido", "albaran", "delivery_note"}
WARRANTY_KINDS = {"warranty", "garantia"}
HANDLED = ("ledger.mail.recorded", "phileas.shipment.new", "phileas.shipment.delivered", "phileas.update",
           "kafka.document.archived", "kafka.warranty.created", "homehoard.item.created", "tantalus.watcher.bought")
_GENERIC = {"shop", "store", "tienda", "online", "sl", "sa", "slu", "inc", "ltd", "llc", "gmbh", "com", "www", "es", "eu", "net",
            "org", "the", "de", "la", "el", "los", "las", "pedido", "order", "payment", "pago", "tu", "your", "ref", "no"}
_PURCHASE_URI = re.compile(r"^hoard://hub/purchase/(\d+)$")


def _fold(text: Any) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", str(text or "").lower()) if not unicodedata.combining(c))


def _lang(hub: Any) -> str:
    lang = str(getattr(getattr(hub, "config", None), "language", "auto") or "auto").lower()
    if lang in ("es", "en"):
        return lang
    env = (os.environ.get("LC_ALL") or os.environ.get("LANG") or "").lower()
    return "en" if env.startswith("en") else "es"


def order_key(ref: Any) -> str:
    """An order reference without the noise (``#123-456`` and ``123456`` are the same order); '' when too short to trust."""
    key = re.sub(r"[^a-z0-9]", "", _fold(ref))
    return key if len(key) >= 4 else ""


def merchant_tokens(name: Any) -> frozenset[str]:
    toks = [t for t in re.split(r"[^a-z0-9]+", _fold(name)) if t and t not in _GENERIC and len(t) > 1]
    return frozenset(toks)


def merchant_similar(a: Any, b: Any) -> bool:
    ta, tb = merchant_tokens(a), merchant_tokens(b)
    if not ta or not tb:
        return False
    if ta == tb or ta <= tb or tb <= ta:
        return True
    fa, fb = "".join(sorted(ta)), "".join(sorted(tb))
    if min(len(fa), len(fb)) >= 4 and (fa in fb or fb in fa):
        return True
    return len(ta & tb) / len(ta | tb) >= 0.5


def _to_amount(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return abs(float(value))
    text = re.sub(r"[^0-9,.\-]", "", str(value))
    if not text:
        return None
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".") if text.rfind(",") > text.rfind(".") else text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        return abs(float(text))
    except ValueError:
        return None


def _to_date(value: Any) -> Optional[date]:
    if isinstance(value, (int, float)) and value > 1e8:
        try:
            return datetime.fromtimestamp(float(value)).date()
        except (OverflowError, OSError, ValueError):
            return None
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(value or ""))
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _items(value: Any) -> list[str]:
    out: list[str] = []
    for it in value if isinstance(value, list) else ([value] if isinstance(value, str) and value else []):
        if isinstance(it, dict):
            it = it.get("title") or it.get("name") or it.get("description") or ""
        text = re.sub(r"\s+", " ", str(it or "")).strip()
        if text and text not in out:
            out.append(text[:160])
    return out[:20]


def _item_overlap(a: list[str], b: list[str]) -> float:
    ta = {t for x in a for t in re.split(r"[^a-z0-9]+", _fold(x)) if len(t) > 2}
    tb = {t for x in b for t in re.split(r"[^a-z0-9]+", _fold(x)) if len(t) > 2}
    return len(ta & tb) / min(len(ta), len(tb)) if ta and tb else 0.0


def fmt_amount(value: Any) -> str:
    if value is None:
        return ""
    return f"{float(value):.2f}".rstrip("0").rstrip(".")


def quick_add_url(base: str, purchase: dict[str, Any]) -> str:
    """HomeHoard's quick-add form pre-filled from a purchase (the form reads the hash query)."""
    pairs = [("name", (purchase.get("items") or [""])[0] or purchase.get("merchant") or ""),
             ("source_ref", f"hoard://hub/purchase/{purchase['id']}"),
             ("price", fmt_amount(purchase.get("amount"))),
             ("merchant", purchase.get("merchant") or ""), ("date", purchase.get("date") or "")]
    query = "&".join(f"{k}={quote(str(v), safe='/:')}" for k, v in pairs if v != "")
    return f"{base.rstrip('/')}/#/add?{query}"


class PurchasesFacet(Facet):
    id = "purchases"
    ui_scripts = ("purchases.js",)
    background_enrich = True        # tests turn the periodic look-up off

    def __init__(self, hub: Any):
        super().__init__(hub)
        self._lock = threading.RLock()
        self._queue: "queue.Queue[Optional[dict[str, Any]]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._off: Optional[Callable[[], None]] = None
        self._stop = threading.Event()
        self._catch_up_to = 0
        self._since = 0
        path = os.path.join(hub.config.data_dir, "purchases.db")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS purchases (id INTEGER PRIMARY KEY AUTOINCREMENT, merchant TEXT NOT NULL DEFAULT '', "
            "order_ref TEXT NOT NULL DEFAULT '', amount REAL, currency TEXT NOT NULL DEFAULT '', date TEXT NOT NULL DEFAULT '', "
            "items_json TEXT NOT NULL DEFAULT '[]', stage TEXT NOT NULL DEFAULT 'paid', refs_json TEXT NOT NULL DEFAULT '{}', "
            "created_ts REAL NOT NULL, updated_ts REAL NOT NULL, notified_json TEXT NOT NULL DEFAULT '{}', "
            "order_key TEXT NOT NULL DEFAULT '', message_ids_json TEXT NOT NULL DEFAULT '[]', meta_json TEXT NOT NULL DEFAULT '{}')")
        self._db.execute("CREATE INDEX IF NOT EXISTS purchases_order ON purchases(order_key)")
        self._db.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT)")
        self._db.commit()

    # -- lifecycle ---------------------------------------------------------------------------
    def start(self) -> None:
        last = self._state_get("last_event_id")
        self._catch_up_to = self.hub.events.last_id
        self._since = int(last) if last is not None else self.hub.events.last_id   # a first run does not replay history
        self._off = self.hub.events.subscribe(self._on_event)
        self._thread = threading.Thread(target=self._loop, name="hoard-hub-purchases", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._off:
            try:
                self._off()
            except Exception:  # noqa: BLE001
                pass
        self._queue.put(None)
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        with self._lock:
            try:
                self._db.close()
            except Exception:  # noqa: BLE001
                pass

    def _state_get(self, key: str) -> Optional[str]:
        with self._lock:
            row = self._db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def _state_set(self, key: str, value: Any) -> None:
        with self._lock:
            self._db.execute("INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
            self._db.commit()

    def _on_event(self, event: dict[str, Any]) -> None:
        if event.get("type") in HANDLED:
            self._queue.put(event)

    def _loop(self) -> None:
        try:
            missed = [e for e in self.hub.events.query(since_id=self._since, limit=2000, newest_first=False)
                      if e["id"] <= self._catch_up_to and e["type"] in HANDLED]
        except Exception:  # noqa: BLE001
            missed = []
        missed_ids = {e["id"] for e in missed}
        for ev in missed:
            self._safe(ev)
        self._state_set("last_event_id", max(self._catch_up_to, int(self._state_get("last_event_id") or 0)))
        next_enrich = time.monotonic() + 30.0
        while not self._stop.is_set():
            try:
                ev = self._queue.get(timeout=1.0)
            except queue.Empty:
                if self.background_enrich and time.monotonic() >= next_enrich:
                    next_enrich = time.monotonic() + ENRICH_EVERY_S
                    self._safe({"type": "_enrich", "data": {}})
                continue
            try:
                if ev is None:
                    break
                if ev.get("id") is not None and ev.get("id") in missed_ids:
                    continue                      # already handled by the catch-up
                self._safe(ev)
                if ev.get("id"):
                    self._state_set("last_event_id", max(int(ev.get("id") or 0), int(self._state_get("last_event_id") or 0)))
            finally:
                self._queue.task_done()

    def _safe(self, ev: dict[str, Any]) -> None:
        try:
            if ev.get("type") == "_enrich":
                pid = (ev.get("data") or {}).get("purchase")
                if pid is None:
                    self.enrich_open()
                else:
                    self.enrich(int(pid), force=True)
                return
            self.ingest(ev)
        except Exception:  # noqa: BLE001 - one odd event must not stop the worker
            pass

    def wait_idle(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._queue.unfinished_tasks == 0:
                return True
            time.sleep(0.01)
        return False

    # -- reading an event ----------------------------------------------------------------------
    @staticmethod
    def signal(event: dict[str, Any]) -> Optional[dict[str, Any]]:
        """What a purchase-related event says, normalised; None for any other event."""
        etype = str(event.get("type") or "")
        d = event.get("data") if isinstance(event.get("data"), dict) else {}
        ts = float(event.get("ts") or time.time())
        base = {"merchant": str(d.get("merchant") or "").strip(), "order_ref": str(d.get("order_ref") or "").strip(),
                "message_id": str(d.get("message_id") or "").strip(), "amount": _to_amount(d.get("amount")),
                "currency": str(d.get("currency") or "").strip(), "date": (_to_date(d.get("date")) or None),
                "items": _items(d.get("items")), "ts": ts, "event_type": etype, "url": str(d.get("url") or ""),
                "purchase_id": None, "related": [], "meta": {}}

        def purchase_id(*values: Any) -> Optional[int]:
            for v in values:
                m = _PURCHASE_URI.match(str(v or ""))
                if m:
                    return int(m.group(1))
            return None

        if etype == "ledger.mail.recorded":
            tx = d.get("tx_id")
            if tx in (None, ""):
                return None
            return {**base, "role": "ledger", "stage": "paid", "key": "ledger", "uri": f"hoard://ledger/tx/{tx}"}
        if etype == "phileas.shipment.new" or (etype == "phileas.shipment.delivered") or (etype == "phileas.update" and str(d.get("status") or "").lower() == "delivered"):
            sid = d.get("shipment_id")
            if sid in (None, ""):
                return None
            delivered = etype != "phileas.shipment.new"
            meta = {k: d[k] for k in ("carrier", "tracking_number", "delivered_at") if d.get(k)}
            return {**base, "role": "delivered" if delivered else "shipment", "stage": "delivered" if delivered else "shipped",
                    "key": "phileas", "uri": f"hoard://phileas/shipment/{sid}", "meta": meta,
                    "date": base["date"] or _to_date(d.get("delivered_at"))}
        if etype == "kafka.document.archived":
            doc = d.get("doc_id")
            kind = str(d.get("kind") or "").strip().lower()
            if doc in (None, "") or kind not in INVOICE_KINDS | WARRANTY_KINDS:
                return None
            key = "warranty" if kind in WARRANTY_KINDS else ("invoice" if kind in ("invoice", "factura") else
                                                              "receipt" if kind in ("receipt", "recibo", "ticket") else "document")
            return {**base, "role": "document", "stage": "filed", "key": key, "uri": f"hoard://kafka/document/{doc}",
                    "meta": {"document_kind": kind}}
        if etype == "kafka.warranty.created":
            doc = d.get("doc_id")
            if doc in (None, ""):
                return None
            related = [f"hoard://phileas/shipment/{d['shipment_id']}"] if d.get("shipment_id") not in (None, "") else []
            return {**base, "role": "warranty", "stage": None, "key": "warranty", "uri": f"hoard://kafka/document/{doc}",
                    "related": related, "meta": {"warranty_until": str(d.get("until") or "")}}
        if etype == "homehoard.item.created":
            item = d.get("item_id")
            pid = purchase_id(d.get("source_ref"))
            if item in (None, "") or pid is None:
                return None
            return {**base, "role": "item", "stage": "stored", "key": "homehoard", "uri": f"hoard://homehoard/item/{item}",
                    "purchase_id": pid}
        if etype == "tantalus.watcher.bought":
            wid = d.get("watcher_id")
            pid = purchase_id(d.get("purchase_ref"))
            if wid in (None, "") or pid is None:
                return None
            return {**base, "role": "bought", "stage": None, "key": "tantalus", "uri": f"hoard://tantalus/watcher/{wid}",
                    "purchase_id": pid}
        return None

    # -- rows -----------------------------------------------------------------------------------
    _COLS = ("id, merchant, order_ref, amount, currency, date, items_json, stage, refs_json, created_ts, updated_ts, "
             "notified_json, order_key, message_ids_json, meta_json")

    @staticmethod
    def _row(r: tuple[Any, ...]) -> dict[str, Any]:
        def j(text: str, default: Any) -> Any:
            try:
                return json.loads(text)
            except (TypeError, ValueError):
                return default
        return {"id": int(r[0]), "merchant": r[1], "order_ref": r[2], "amount": r[3], "currency": r[4], "date": r[5],
                "items": j(r[6], []), "stage": r[7], "refs": j(r[8], {}), "created_ts": r[9], "updated_ts": r[10],
                "notified": j(r[11], {}), "order_key": r[12], "message_ids": j(r[13], []), "meta": j(r[14], {})}

    def _fetch(self, pid: int) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._db.execute(f"SELECT {self._COLS} FROM purchases WHERE id=?", (int(pid),)).fetchone()
        return self._row(row) if row else None

    def _save_row(self, p: dict[str, Any]) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE purchases SET merchant=?, order_ref=?, amount=?, currency=?, date=?, items_json=?, stage=?, refs_json=?, "
                "updated_ts=?, notified_json=?, order_key=?, message_ids_json=?, meta_json=? WHERE id=?",
                (p["merchant"], p["order_ref"], p["amount"], p["currency"], p["date"], json.dumps(p["items"], ensure_ascii=False),
                 p["stage"], json.dumps(p["refs"]), p["updated_ts"], json.dumps(p["notified"], ensure_ascii=False), p["order_key"],
                 json.dumps(p["message_ids"]), json.dumps(p["meta"], ensure_ascii=False), p["id"]))
            self._db.commit()

    # -- matching ---------------------------------------------------------------------------------
    def find(self, sig: dict[str, Any]) -> Optional[dict[str, Any]]:
        """The purchase an event belongs to, or None."""
        if sig.get("purchase_id"):
            return self._fetch(sig["purchase_id"])
        with self._lock:
            rows = self._db.execute(f"SELECT {self._COLS} FROM purchases WHERE stage != 'closed' ORDER BY id DESC LIMIT 800").fetchall()
        cands = [self._row(r) for r in rows]
        uris = {sig["uri"], *sig.get("related", [])}
        for p in cands:                                           # 1. a record already linked
            if uris & set(p["refs"].values()):
                return p
        ok = order_key(sig.get("order_ref"))
        if ok:                                                    # 2. same order
            for p in cands:
                if p["order_key"] == ok and not self._conflict(sig, p):
                    return p
        mid = sig.get("message_id")
        if mid:                                                   # 3. same mail
            for p in cands:
                if mid in p["message_ids"] and not self._conflict(sig, p):
                    return p
        best: Optional[tuple[int, dict[str, Any]]] = None         # 4. fuzzy
        for p in cands:
            if self._conflict(sig, p) or not self._fuzzy(sig, p):
                continue
            diff = abs(((_to_date(p["date"]) or date.fromtimestamp(p["created_ts"])) - self._sig_date(sig)).days)
            if best is None or diff < best[0]:
                best = (diff, p)
        return best[1] if best else None

    @staticmethod
    def _sig_date(sig: dict[str, Any]) -> date:
        return sig.get("date") or date.fromtimestamp(sig.get("ts") or time.time())

    @staticmethod
    def _conflict(sig: dict[str, Any], p: dict[str, Any]) -> bool:
        """Two different records of the same kind (another payment, another parcel) are two purchases."""
        have = p["refs"].get(sig["key"])
        if sig["key"] in ("ledger", "phileas", "homehoard", "tantalus") and have and have != sig["uri"]:
            return True
        ok = order_key(sig.get("order_ref"))
        return bool(ok and p["order_key"] and ok != p["order_key"])

    def _fuzzy(self, sig: dict[str, Any], p: dict[str, Any]) -> bool:
        if not merchant_similar(sig.get("merchant"), p["merchant"]):
            return False
        pdate = _to_date(p["date"]) or date.fromtimestamp(p["created_ts"])
        if abs((pdate - self._sig_date(sig)).days) > DATE_WINDOW_DAYS:
            return False
        a, b = sig.get("amount"), p["amount"]
        if a is not None and b is not None:
            return abs(a - b) <= AMOUNT_TOLERANCE * max(a, b) + 1e-9
        return True       # one side has no amount (a parcel, a bare document): merchant + dates are what we have

    # -- applying an event ----------------------------------------------------------------------
    def ingest(self, event: dict[str, Any]) -> Optional[dict[str, Any]]:
        """Apply one event (the worker calls this; tests may too). Returns the purchase it touched."""
        sig = self.signal(event)
        if sig is None:
            return None
        sig["_enrich"] = bool(event.get("_enrich"))
        with self._lock:
            p = self.find(sig)
            created = False
            if p is None:
                if sig["role"] in ("item", "bought"):
                    return None
                now = time.time()
                with self._lock:
                    cur = self._db.execute(
                        "INSERT INTO purchases (merchant, order_ref, amount, currency, date, items_json, stage, refs_json, created_ts, "
                        "updated_ts, notified_json, order_key, message_ids_json, meta_json) VALUES ('', '', NULL, '', '', '[]', ?, '{}', ?, ?, '{}', '', '[]', '{}')",
                        (sig["stage"] or "filed", now, now))
                    self._db.commit()
                p = self._fetch(cur.lastrowid)
                created = True
            old_refs = dict(p["refs"])
            old_stage = "" if created else p["stage"]      # a new purchase's first stage is a change too
            if sig.get("merchant") and not p["merchant"]:
                p["merchant"] = sig["merchant"]
            if sig.get("order_ref") and not p["order_ref"]:
                p["order_ref"] = sig["order_ref"]
                p["order_key"] = order_key(sig["order_ref"])
            if sig.get("amount") is not None and p["amount"] is None:
                p["amount"] = sig["amount"]
            if sig.get("currency") and not p["currency"]:
                p["currency"] = sig["currency"]
            if sig.get("date") and not p["date"] and sig["role"] != "delivered":
                p["date"] = sig["date"].isoformat()
            for it in sig.get("items") or []:
                if it not in p["items"]:
                    p["items"].append(it)
            if sig.get("message_id") and sig["message_id"] not in p["message_ids"]:
                p["message_ids"].append(sig["message_id"])
            p["meta"].update({k: v for k, v in (sig.get("meta") or {}).items() if v})
            if sig["role"] == "delivered":
                p["meta"]["delivered"] = True
            if sig["key"] not in p["refs"]:
                p["refs"][sig["key"]] = sig["uri"]
            if p["stage"] != "closed" and sig.get("stage") and RANK.get(sig["stage"], 0) > RANK.get(p["stage"], 0):
                p["stage"] = sig["stage"]
            p["updated_ts"] = time.time()
            self._save_row(p)
        added = [(k, u) for k, u in p["refs"].items() if old_refs.get(k) != u]
        if p["stage"] != old_stage:
            try:
                self.hub.events.emit("purchases.stage", {"id": p["id"], "stage": p["stage"], "previous": old_stage or None,
                                                         "merchant": p["merchant"], "title": self._title(p)}, source="hub")
            except Exception:  # noqa: BLE001
                pass
        self._link(p, old_refs, added)
        self._actions(p, sig, added)
        if not sig.get("_enrich") and (created or added):
            self._queue.put({"type": "_enrich", "id": None, "data": {"purchase": p["id"]}})
        return self._fetch(p["id"])

    # -- filling the gaps: ask the apps instead of waiting for an event that already went by ------------------
    def enrich(self, pid: int, *, force: bool = False) -> dict[str, Any]:
        """Look for the records a purchase still lacks: the Phileas shipment with the same order number and the Ledger
        movement with the same amount, merchant and date (``tx_find``). A record found is fed in as if its event had
        just arrived (so links, stage and actions follow the same path). At most once every ten minutes per purchase."""
        p = self._fetch(pid)
        if p is None or p["stage"] == "closed":
            return {"ok": False, "error": "unknown or closed purchase"}
        last = float(p["meta"].get("enriched_ts") or 0)
        if not force and time.time() - last < ENRICH_EVERY_S:
            return {"ok": True, "skipped": "recently"}
        found: list[str] = []
        if "phileas" not in p["refs"] and p["order_ref"]:
            res = self._ask("phileas", "shipments_list", {"text": p["order_ref"]})
            for sh in (res.get("shipments") if isinstance(res, dict) else None) or []:
                if not isinstance(sh, dict) or order_key(sh.get("order_ref")) != p["order_key"] or not sh.get("id"):
                    continue
                delivered = str(sh.get("status") or "").lower() == "delivered"
                data = {"shipment_id": sh["id"], "merchant": sh.get("merchant") or "", "order_ref": sh.get("order_ref") or "",
                        "carrier": sh.get("carrier") or "", "tracking_number": sh.get("tracking_number") or "",
                        "items": [sh["item"]] if sh.get("item") else []}
                if delivered and sh.get("delivered_ts"):
                    data["delivered_at"] = datetime.fromtimestamp(float(sh["delivered_ts"])).isoformat(timespec="seconds")
                self.ingest({"type": "phileas.shipment.new", "id": None, "ts": time.time(), "data": data, "_enrich": True})
                if delivered:
                    self.ingest({"type": "phileas.shipment.delivered", "id": None, "ts": time.time(), "data": data, "_enrich": True})
                found.append(f"hoard://phileas/shipment/{sh['id']}")
                break
        p = self._fetch(pid) or p
        if "ledger" not in p["refs"] and p["amount"] is not None and p["date"]:
            res = self._ask("ledger", "tx_find", {"amount": p["amount"], "date": p["date"], "merchant": p["merchant"],
                                                  "days": DATE_WINDOW_DAYS, **({"currency": p["currency"]} if p["currency"] else {})})
            matches = [m for m in ((res.get("matches") if isinstance(res, dict) else None) or [])
                       if isinstance(m, dict) and m.get("tx_id") not in (None, "") and float(m.get("score") or 0) >= 0.8]
            if len(matches) == 1:
                m = matches[0]
                self.ingest({"type": "ledger.mail.recorded", "id": None, "ts": time.time(), "_enrich": True,
                             "data": {"tx_id": m["tx_id"], "merchant": m.get("merchant") or p["merchant"], "amount": m.get("amount"),
                                      "currency": p["currency"], "date": m.get("date") or p["date"], "order_ref": p["order_ref"]}})
                found.append(f"hoard://ledger/tx/{m['tx_id']}")
        with self._lock:
            fresh = self._fetch(pid) or p
            fresh["meta"]["enriched_ts"] = time.time()
            self._save_row(fresh)
        return {"ok": True, "found": found}

    def _ask(self, app: str, tool: str, args: dict[str, Any]) -> Any:
        try:
            res = self.hub.call_app(app, tool, args, caller="hub", timeout=20.0)
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(res, dict) or not res.get("ok"):
            return None
        out = res.get("result")
        if isinstance(out, dict) and isinstance(out.get("result"), (dict, list)) and len(out) <= 3:
            out = out["result"]
        return out

    def enrich_open(self) -> int:
        """Run :meth:`enrich` over every open purchase that still lacks a shipment or a payment."""
        with self._lock:
            ids = [r[0] for r in self._db.execute("SELECT id FROM purchases WHERE stage != 'closed'").fetchall()]
        n = 0
        for pid in ids:
            p = self._fetch(pid)
            if p and ("phileas" not in p["refs"] or "ledger" not in p["refs"]):
                if self.enrich(pid).get("found"):
                    n += 1
        return n

    @staticmethod
    def _title(p: dict[str, Any]) -> str:
        return (p["items"][0] if p["items"] else "") or p["merchant"] or f"#{p['id']}"

    def _label(self, key: str, p: dict[str, Any]) -> str:
        if key == "ledger":
            amount = "" if p["amount"] is None else f" {fmt_amount(p['amount'])} {p['currency']}".rstrip()
            return f"{p['merchant'] or self._title(p)}{amount}"
        return self._title(p)

    def _link(self, p: dict[str, Any], old_refs: dict[str, str], added: list[tuple[str, str]]) -> None:
        refs = self.hub.facet("refs")
        if refs is None or not callable(getattr(refs, "link", None)) or not added:
            return

        def order_index(key: str) -> int:
            return REF_ORDER.index(key) if key in REF_ORDER else len(REF_ORDER)

        existing = sorted([k for k in old_refs], key=order_index)
        for key, uri in sorted(added, key=lambda kv: order_index(kv[0])):
            if existing:
                anchor = existing[0]
                a_key, b_key = (anchor, key) if order_index(anchor) <= order_index(key) else (key, anchor)
                try:
                    refs.link(p["refs"][a_key], p["refs"][b_key], "purchase", from_label=self._label(a_key, p),
                              to_label=self._label(b_key, p), by="hub")
                except Exception:  # noqa: BLE001
                    pass
            existing.append(key)

    # -- actions, once per purchase -------------------------------------------------------------------
    def _actions(self, p: dict[str, Any], sig: dict[str, Any], added: list[tuple[str, str]]) -> None:
        todo: list[str] = []
        if any(k == "ledger" for k, _ in added) and "tantalus" not in p["notified"]:
            todo.append("tantalus")
        if sig["role"] == "delivered" and "delivered" not in p["notified"] and "homehoard" not in p["refs"] and p["stage"] != "closed":
            todo.append("delivered")
        if not todo:
            return
        # claim first: a second event for the same purchase must not repeat an action while this one runs
        with self._lock:
            fresh = self._fetch(p["id"]) or p
            for name in todo:
                fresh["notified"].setdefault(name, {"ts": time.time(), "pending": True})
            self._save_row(fresh)
        results: dict[str, Any] = {}
        if "tantalus" in todo:
            results["tantalus"] = self._tantalus(p, sig)
        if "delivered" in todo:
            results["delivered"] = self._delivered(p)
        with self._lock:
            fresh = self._fetch(p["id"]) or p
            for name, res in results.items():
                fresh["notified"][name] = {"ts": time.time(), **res}
            self._save_row(fresh)

    def _notify(self, title: str, body: str, *, url: str = "", dedupe: str = "") -> bool:
        notify = self.hub.facet("notify")
        if notify is None or not callable(getattr(notify, "send", None)):
            return False
        try:
            notify.send(title, body, app="hub", priority="normal", url=url, group="purchase", dedupe_key=dedupe)
            return True
        except Exception:  # noqa: BLE001
            return False

    def _tantalus(self, p: dict[str, Any], sig: dict[str, Any]) -> dict[str, Any]:
        args = {"title": self._title(p), "merchant": p["merchant"]}
        if sig.get("url"):
            args["url"] = sig["url"]
        try:
            res = self.hub.call_app("tantalus", "watchers_match_purchase", args, caller="hub", timeout=15.0)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"{type(exc).__name__}: {exc}"}
        if not res.get("ok"):
            return {"error": str(res.get("error") or "no answer")[:160]}
        result = res.get("result")
        if isinstance(result, dict) and "matches" not in result and isinstance(result.get("result"), dict):
            result = result["result"]
        matches = result.get("matches") if isinstance(result, dict) else None
        marked: list[Any] = []
        es = _lang(self.hub) == "es"
        for m in sorted([m for m in matches or [] if isinstance(m, dict)], key=lambda m: -(m.get("score") or 0)):
            score = m.get("score")
            if not isinstance(score, (int, float)) or score < TANTALUS_MIN_SCORE or m.get("watcher_id") in (None, ""):
                continue
            ref = f"hoard://hub/purchase/{p['id']}"
            r2 = self.hub.call_app("tantalus", "watcher_mark_bought", {"watcher_id": m["watcher_id"], "purchase_ref": ref},
                                   caller="hub", timeout=15.0)
            if not r2.get("ok"):
                continue
            marked.append(m["watcher_id"])
            what = str(m.get("name") or m.get("watcher_name") or m.get("title") or self._title(p))
            self._notify(f"Dejo de vigilar {what}: ya lo has comprado" if es else f"Stopped watching {what}: you already bought it",
                         p["merchant"], dedupe=f"purchase:{p['id']}:watcher:{m['watcher_id']}")
            if len(marked) >= 3:
                break
        return {"matches": len(matches or []), "marked": marked}

    def _delivered(self, p: dict[str, Any]) -> dict[str, Any]:
        home = self.hub.get("homehoard")
        base = (home.url if home is not None else HOME_URL) or HOME_URL
        url = quick_add_url(base, p)
        es = _lang(self.hub) == "es"
        what = self._title(p)
        sent = self._notify(f"Ha llegado {what}. ¿Dónde lo guardas?" if es else f"{what} has arrived. Where do you keep it?",
                            p["merchant"], url=url, dedupe=f"purchase:{p['id']}:delivered")
        return {"sent": sent, "url": url}

    # -- reading / closing ---------------------------------------------------------------------------
    def view(self, p: dict[str, Any]) -> dict[str, Any]:
        refs = {}
        for key, uri in p["refs"].items():
            parts = uri.split("/")
            app = parts[2] if len(parts) > 2 else ""
            a = self.hub.get(app)
            refs[key] = {"uri": uri, "app": app, "kind": parts[3] if len(parts) > 3 else "", "id": "/".join(parts[4:]),
                         "app_url": a.url if a is not None else ""}
        r = p["refs"]
        milestones = {"paid": "ledger" in r, "shipped": "phileas" in r, "delivered": bool(p["meta"].get("delivered_at") or p["meta"].get("delivered")),
                      "filed": any(k in r for k in ("invoice", "receipt", "document", "warranty")), "stored": "homehoard" in r}
        return {"id": p["id"], "title": self._title(p), "merchant": p["merchant"], "order_ref": p["order_ref"], "amount": p["amount"],
                "currency": p["currency"], "date": p["date"], "items": p["items"], "stage": p["stage"], "stage_index": RANK.get(p["stage"], 0),
                "milestones": milestones,
                "refs": refs, "meta": p["meta"], "created_ts": p["created_ts"], "updated_ts": p["updated_ts"],
                "notified": {k: {kk: vv for kk, vv in v.items() if kk != "pending"} if isinstance(v, dict) else v
                             for k, v in p["notified"].items()},
                "uri": f"hoard://hub/purchase/{p['id']}"}

    def list(self, stage: str = "", q: str = "", limit: int = 100) -> dict[str, Any]:
        with self._lock:
            rows = self._db.execute(f"SELECT {self._COLS} FROM purchases ORDER BY updated_ts DESC, id DESC").fetchall()
        all_p = [self._row(r) for r in rows]
        counts = {s: 0 for s in STAGES}
        for p in all_p:
            counts[p["stage"]] = counts.get(p["stage"], 0) + 1
        words = [w for w in _fold(q).split() if w]
        out = []
        for p in all_p:
            if stage and p["stage"] != stage:
                continue
            if words:
                hay = _fold(" ".join([p["merchant"], p["order_ref"], " ".join(p["items"]), " ".join(p["refs"].values())]))
                if not all(w in hay for w in words):
                    continue
            out.append(self.view(p))
        limit = max(1, min(int(limit or 100), 500))
        return {"ok": True, "purchases": out[:limit], "total": len(out), "counts": counts}

    def get_purchase(self, pid: int) -> Optional[dict[str, Any]]:
        p = self._fetch(pid)
        return self.view(p) if p else None

    def close_purchase(self, pid: int) -> dict[str, Any]:
        with self._lock:
            p = self._fetch(pid)
            if p is None:
                return {"ok": False, "status": 404, "error": f"unknown purchase: {pid}"}
            previous = p["stage"]
            p["stage"] = "closed"
            p["updated_ts"] = time.time()
            self._save_row(p)
        if previous != "closed":
            try:
                self.hub.events.emit("purchases.stage", {"id": pid, "stage": "closed", "previous": previous,
                                                         "merchant": p["merchant"], "title": self._title(p)}, source="hub")
            except Exception:  # noqa: BLE001
                pass
        return {"ok": True, "purchase": self.view(p)}

    def reopen_purchase(self, pid: int) -> dict[str, Any]:
        """Back from ``closed``: the stage the records say it reached."""
        with self._lock:
            p = self._fetch(pid)
            if p is None:
                return {"ok": False, "status": 404, "error": f"unknown purchase: {pid}"}
            refs = p["refs"]
            if "homehoard" in refs:
                stage = "stored"
            elif any(k in refs for k in ("invoice", "receipt", "document", "warranty")):
                stage = "filed"
            elif p["meta"].get("delivered_at") or (p["notified"].get("delivered")):
                stage = "delivered"
            elif "phileas" in refs:
                stage = "shipped"
            else:
                stage = "paid"
            p["stage"] = stage
            p["updated_ts"] = time.time()
            self._save_row(p)
        return {"ok": True, "purchase": self.view(p)}

    # -- HTTP -------------------------------------------------------------------------------------------
    def get(self, req: Request) -> Optional[Any]:
        if req.path == "/api/purchases":
            return self.list(req.q("stage") or "", req.q("q") or "", req.q_int("limit", 100))
        m = re.fullmatch(r"/api/purchases/(\d+)", req.path)
        if m:
            v = self.get_purchase(int(m.group(1)))
            return {"ok": True, "purchase": v} if v else {"ok": False, "status": 404, "error": "unknown purchase"}
        return None

    def post(self, req: Request) -> Optional[Any]:
        m = re.fullmatch(r"/api/purchases/(\d+)/(close|reopen)", req.path)
        if not m:
            return None
        if not req.caller():
            return {"ok": False, "status": 401, "error": "a family bearer token is required"}
        pid = int(m.group(1))
        return self.close_purchase(pid) if m.group(2) == "close" else self.reopen_purchase(pid)

    # -- agent tools --------------------------------------------------------------------------------------
    @classmethod
    def tools(cls) -> list[dict[str, Any]]:
        return [
            {
                "name": "hub_purchases",
                "description": "List purchases and the stage each reached. Keywords: compras, pedidos, purchases, orders, qué he comprado.\n"
                               "Stages: paid, shipped, delivered, filed (invoice archived), stored (has a place at home), closed. "
                               "Filter by stage or by a word (merchant, item, order number).",
                "inputSchema": {"type": "object", "properties": {
                    "stage": {"type": "string", "enum": list(STAGES)},
                    "q": {"type": "string", "description": "A word of the merchant, item or order reference."},
                    "limit": {"type": "integer", "default": 50}}, "additionalProperties": False},
                "annotations": {"readOnlyHint": True},
            },
            {
                "name": "hub_purchase",
                "description": "One purchase in full, with its records in each app. Keywords: compra, pedido, purchase detail.\n"
                               "Returns stage, items, amount and refs to the Ledger payment, the Phileas parcel, the Kafka invoice and warranty, the HomeHoard item.",
                "inputSchema": {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"],
                                "additionalProperties": False},
                "annotations": {"readOnlyHint": True},
            },
        ]

    def handlers(self) -> dict[str, Any]:
        def purchases(a: dict[str, Any]) -> dict[str, Any]:
            return self.list(str(a.get("stage") or ""), str(a.get("q") or ""), int(a.get("limit") or 50))

        def purchase(a: dict[str, Any]) -> dict[str, Any]:
            try:
                v = self.get_purchase(int(a.get("id")))
            except (TypeError, ValueError):
                return {"ok": False, "error": "id must be a number"}
            return {"ok": True, "purchase": v} if v else {"ok": False, "error": f"unknown purchase: {a.get('id')}"}

        return {"hub_purchases": purchases, "hub_purchase": purchase}


__all__ = ["PurchasesFacet", "quick_add_url", "order_key", "merchant_similar", "STAGES"]
