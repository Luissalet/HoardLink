"""References between apps — the ``refs`` facet.

Apps already put ``hoard://<app>/<kind>/<id>`` in their records (``hoard_link.artifacts``): a Writer
item remembers the chapter it came from, a Ledger payment remembers the mail. Each app only knows its
own side. The hub keeps the graph — one row per link — so either end can find the other: "what is
connected to this payment?" is one question to one place.

``<data>/refs.db`` (SQLite): ``edges(id, from_uri, to_uri, rel, from_label, to_label, note, by_app, ts,
UNIQUE(from_uri, to_uri, rel))``. A link is symmetric when read (``around`` follows it both ways) and
directed when stored (``from`` -> ``to``, with the ``rel`` that names it: ``related``, ``purchase``,
``source``...).

Python API (other facets call it): :meth:`RefsFacet.link`, :meth:`~RefsFacet.unlink`,
:meth:`~RefsFacet.around`, :meth:`~RefsFacet.recent`, :meth:`~RefsFacet.search_labels`.
HTTP: ``POST /api/refs`` (a family token: the caller app must own the ``from`` or the ``to`` URI; the hub
and its page may link anything), ``GET /api/refs?uri=&depth=1|2`` (the neighbourhood) or ``?q=`` (a label
fragment), ``GET /api/refs/recent``, ``POST /api/refs/remove``. Each new edge emits ``refs.linked``.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
import unicodedata
from typing import Any, Optional

from ..artifacts import parse_ref
from .facets import Facet, Request

MAX_DEPTH = 3
MAX_EDGES = 500
_REL_OK = re.compile(r"[^a-z0-9_.\-]")


def _fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", str(text or "").lower()) if not unicodedata.combining(c))


def normalize_uri(uri: Any) -> str:
    """The canonical ``hoard://app/kind/id`` of ``uri`` (raises ValueError when it is not one)."""
    text = str(uri or "").strip()
    if not text or any(c.isspace() for c in text):
        raise ValueError("a Hoard reference looks like hoard://<app>/<kind>/<id>")
    ref = parse_ref(text)
    return ref.uri


def normalize_rel(rel: Any) -> str:
    out = _REL_OK.sub("", str(rel or "related").strip().lower().replace(" ", "_"))[:40]
    return out or "related"


class RefsFacet(Facet):
    id = "refs"
    ui_scripts = ("refs.js",)

    def __init__(self, hub: Any):
        super().__init__(hub)
        self._lock = threading.RLock()
        path = os.path.join(hub.config.data_dir, "refs.db")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS edges (id INTEGER PRIMARY KEY AUTOINCREMENT, from_uri TEXT NOT NULL, to_uri TEXT NOT NULL, "
            "rel TEXT NOT NULL, from_label TEXT NOT NULL DEFAULT '', to_label TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '', "
            "by_app TEXT NOT NULL DEFAULT 'hub', ts REAL NOT NULL, UNIQUE(from_uri, to_uri, rel))")
        self._db.execute("CREATE INDEX IF NOT EXISTS edges_from ON edges(from_uri)")
        self._db.execute("CREATE INDEX IF NOT EXISTS edges_to ON edges(to_uri)")
        self._db.commit()

    def close(self) -> None:
        with self._lock:
            try:
                self._db.close()
            except Exception:  # noqa: BLE001
                pass

    # -- rows ---------------------------------------------------------------------------
    _COLS = "id, from_uri, to_uri, rel, from_label, to_label, note, by_app, ts"

    @staticmethod
    def _edge(r: tuple[Any, ...]) -> dict[str, Any]:
        return {"id": int(r[0]), "from": r[1], "to": r[2], "rel": r[3], "from_label": r[4], "to_label": r[5],
                "note": r[6], "by": r[7], "ts": float(r[8])}

    def _app_url(self, app_id: str) -> str:
        try:
            app = self.hub.get(app_id)
        except Exception:  # noqa: BLE001
            app = None
        return app.url if app is not None else ""

    def _node(self, uri: str, label: str = "", depth: int = 0) -> dict[str, Any]:
        try:
            ref = parse_ref(uri)
            app, kind, rid = ref.app, ref.kind, ref.id
        except ValueError:
            app, kind, rid = "", "", uri
        return {"uri": uri, "app": app, "kind": kind, "id": rid, "label": label, "app_url": self._app_url(app), "depth": depth}

    # -- API ------------------------------------------------------------------------------
    def link(self, from_uri: str, to_uri: str, rel: str = "related", *, from_label: str = "", to_label: str = "",
             note: str = "", by: str = "hub") -> dict[str, Any]:
        """Record ``from_uri --rel--> to_uri``. Idempotent: the same triple only refreshes empty labels/notes
        (an existing label is never blanked) and emits nothing."""
        try:
            a, b = normalize_uri(from_uri), normalize_uri(to_uri)
        except ValueError as exc:
            return {"ok": False, "status": 400, "error": str(exc)}
        if a == b:
            return {"ok": False, "status": 400, "error": "a record cannot link to itself"}
        rel = normalize_rel(rel)
        fl, tl, nt = str(from_label or "")[:200], str(to_label or "")[:200], str(note or "")[:500]
        by = str(by or "hub")[:60]
        created = False
        with self._lock:
            row = self._db.execute(f"SELECT {self._COLS} FROM edges WHERE from_uri=? AND to_uri=? AND rel=?", (a, b, rel)).fetchone()
            if row is None:
                cur = self._db.execute(
                    "INSERT INTO edges (from_uri, to_uri, rel, from_label, to_label, note, by_app, ts) VALUES (?,?,?,?,?,?,?,?)",
                    (a, b, rel, fl, tl, nt, by, time.time()))
                self._db.commit()
                created = True
                row = self._db.execute(f"SELECT {self._COLS} FROM edges WHERE id=?", (cur.lastrowid,)).fetchone()
            else:
                new_fl, new_tl, new_nt = fl or row[4], tl or row[5], nt or row[6]
                if (new_fl, new_tl, new_nt) != (row[4], row[5], row[6]):
                    self._db.execute("UPDATE edges SET from_label=?, to_label=?, note=? WHERE id=?", (new_fl, new_tl, new_nt, row[0]))
                    self._db.commit()
                    row = self._db.execute(f"SELECT {self._COLS} FROM edges WHERE id=?", (row[0],)).fetchone()
        edge = self._edge(row)
        if created:
            try:
                self.hub.events.emit("refs.linked", {"from": a, "to": b, "rel": rel}, source="hub")
            except Exception:  # noqa: BLE001
                pass
        return {"ok": True, "created": created, "edge": edge}

    def unlink(self, from_uri: str = "", to_uri: str = "", rel: Optional[str] = None, *, edge_id: Optional[int] = None) -> dict[str, Any]:
        """Remove one edge (by id, or by ``from``/``to`` and optionally ``rel``)."""
        with self._lock:
            if edge_id is not None:
                cur = self._db.execute("DELETE FROM edges WHERE id=?", (int(edge_id),))
            else:
                try:
                    a, b = normalize_uri(from_uri), normalize_uri(to_uri)
                except ValueError as exc:
                    return {"ok": False, "status": 400, "error": str(exc)}
                if rel:
                    cur = self._db.execute("DELETE FROM edges WHERE from_uri=? AND to_uri=? AND rel=?", (a, b, normalize_rel(rel)))
                else:
                    cur = self._db.execute("DELETE FROM edges WHERE from_uri=? AND to_uri=?", (a, b))
            self._db.commit()
            n = cur.rowcount
        if not n:
            return {"ok": False, "status": 404, "error": "no such link"}
        return {"ok": True, "removed": n}

    def get_edge(self, edge_id: int) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._db.execute(f"SELECT {self._COLS} FROM edges WHERE id=?", (int(edge_id),)).fetchone()
        return self._edge(row) if row else None

    def around(self, uri: str, depth: int = 1) -> dict[str, Any]:
        """``{nodes, edges}`` within ``depth`` hops of ``uri`` (links followed both ways; depth 1..3)."""
        try:
            root = normalize_uri(uri)
        except ValueError as exc:
            return {"ok": False, "status": 400, "error": str(exc)}
        depth = max(1, min(int(depth or 1), MAX_DEPTH))
        labels: dict[str, str] = {}
        seen_depth: dict[str, int] = {root: 0}
        edges: dict[int, dict[str, Any]] = {}
        frontier = [root]
        with self._lock:
            for hop in range(1, depth + 1):
                nxt: list[str] = []
                for node in frontier:
                    rows = self._db.execute(f"SELECT {self._COLS} FROM edges WHERE from_uri=? OR to_uri=? ORDER BY id", (node, node)).fetchall()
                    for r in rows:
                        e = self._edge(r)
                        if e["id"] in edges:
                            continue
                        if len(edges) >= MAX_EDGES:
                            break
                        edges[e["id"]] = e
                        if e["from_label"]:
                            labels.setdefault(e["from"], e["from_label"])
                        if e["to_label"]:
                            labels.setdefault(e["to"], e["to_label"])
                        for end in (e["from"], e["to"]):
                            if end not in seen_depth:
                                seen_depth[end] = hop
                                nxt.append(end)
                frontier = nxt
                if not frontier:
                    break
        nodes = [self._node(u, labels.get(u, ""), d) for u, d in sorted(seen_depth.items(), key=lambda kv: (kv[1], kv[0]))]
        return {"ok": True, "uri": root, "depth": depth, "nodes": nodes, "edges": list(edges.values()),
                "truncated": len(edges) >= MAX_EDGES}

    def recent(self, limit: int = 30) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit or 30), 500))
        with self._lock:
            rows = self._db.execute(f"SELECT {self._COLS} FROM edges ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._edge(r) for r in rows]

    def count(self) -> int:
        with self._lock:
            return int(self._db.execute("SELECT COUNT(*) FROM edges").fetchone()[0])

    def search_labels(self, q: str, limit: int = 10) -> list[dict[str, Any]]:
        """Records whose label (or URI) contains every word of ``q`` — for the global search. Each hit:
        ``{title, snippet, id, url, uri, app, kind, score}``."""
        words = [w for w in _fold(q).split() if w]
        if not words:
            return []
        limit = max(1, min(int(limit or 10), 100))
        nodes: dict[str, dict[str, Any]] = {}
        with self._lock:
            rows = self._db.execute(f"SELECT {self._COLS} FROM edges ORDER BY id DESC LIMIT 5000").fetchall()
        for r in rows:
            e = self._edge(r)
            for uri, label in ((e["from"], e["from_label"]), (e["to"], e["to_label"])):
                node = nodes.setdefault(uri, {"uri": uri, "label": "", "links": 0})
                node["links"] += 1
                if label and not node["label"]:
                    node["label"] = label
        out: list[dict[str, Any]] = []
        for uri, node in nodes.items():
            hay = _fold(node["label"] + " " + uri)
            if not all(w in hay for w in words):
                continue
            label_fold = _fold(node["label"])
            score = (1.0 if all(w in label_fold for w in words) else 0.6) + min(node["links"], 10) / 100.0
            try:
                ref = parse_ref(uri)
                app, kind, rid = ref.app, ref.kind, ref.id
            except ValueError:
                app = kind = ""
                rid = uri
            out.append({"title": node["label"] or f"{kind} {rid}".strip(), "snippet": uri, "id": rid, "url": "", "uri": uri,
                        "app": app, "kind": kind, "score": round(score, 3), "links": node["links"]})
        out.sort(key=lambda h: (-h["score"], h["uri"]))
        return out[:limit]

    # -- HTTP ---------------------------------------------------------------------------
    @staticmethod
    def _owns(caller: Optional[str], *uris: str) -> bool:
        if caller in ("hub", "ui"):
            return True
        if not caller:
            return False
        for u in uris:
            try:
                if parse_ref(u).app == caller:
                    return True
            except ValueError:
                continue
        return False

    def get(self, req: Request) -> Optional[Any]:
        if req.path == "/api/refs/recent":
            return {"ok": True, "edges": self.recent(req.q_int("limit", 30))}
        if req.path == "/api/refs":
            uri = req.q("uri")
            if uri:
                return self.around(uri, req.q_int("depth", 1))
            q = (req.q("q") or "").strip()
            if q.startswith("hoard://"):
                return self.around(q, req.q_int("depth", 1))
            if q:
                return {"ok": True, "q": q, "matches": self.search_labels(q, req.q_int("limit", 20))}
            return {"ok": True, "edges": self.recent(req.q_int("limit", 30)), "count": self.count()}
        return None

    def post(self, req: Request) -> Optional[Any]:
        if req.path not in ("/api/refs", "/api/refs/remove"):
            return None
        caller = req.caller()
        if not caller:
            return {"ok": False, "status": 401, "error": "a family bearer token is required"}
        b = req.body or {}
        if req.path == "/api/refs":
            frm, to = b.get("from") or b.get("from_uri"), b.get("to") or b.get("to_uri")
            try:
                a, c = normalize_uri(frm), normalize_uri(to)
            except ValueError as exc:
                return {"ok": False, "status": 400, "error": str(exc)}
            if not self._owns(caller, a, c):
                return {"ok": False, "status": 403, "error": f"{caller} may only link records of its own app"}
            by = str(b.get("by") or caller) if caller in ("hub", "ui") else caller
            return self.link(a, c, b.get("rel") or "related", from_label=b.get("from_label") or "", to_label=b.get("to_label") or "",
                             note=b.get("note") or "", by=by)
        # remove
        if b.get("id") is not None:
            edge = self.get_edge(int(b["id"])) if str(b["id"]).lstrip("-").isdigit() else None
            if edge is None:
                return {"ok": False, "status": 404, "error": "no such link"}
            if not self._owns(caller, edge["from"], edge["to"]):
                return {"ok": False, "status": 403, "error": f"{caller} may only remove links of its own app"}
            return self.unlink(edge_id=edge["id"])
        frm, to = b.get("from") or b.get("from_uri"), b.get("to") or b.get("to_uri")
        try:
            a, c = normalize_uri(frm), normalize_uri(to)
        except ValueError as exc:
            return {"ok": False, "status": 400, "error": str(exc)}
        if not self._owns(caller, a, c):
            return {"ok": False, "status": 403, "error": f"{caller} may only remove links of its own app"}
        return self.unlink(a, c, b.get("rel"))

    # -- agent tools --------------------------------------------------------------------
    @classmethod
    def tools(cls) -> list[dict[str, Any]]:
        return [
            {
                "name": "hub_refs",
                "description": "Show what a record is linked to across apps. Keywords: references, links, enlaces, relacionado, hoard://.\n"
                               "Give a uri (hoard://<app>/<kind>/<id>) for its neighbourhood, or q (a label fragment) to find records; "
                               "with neither it lists the latest links.",
                "inputSchema": {"type": "object", "properties": {
                    "uri": {"type": "string", "description": "hoard://<app>/<kind>/<id>"},
                    "q": {"type": "string", "description": "A word of a record's label, e.g. 'invoice amazon'."},
                    "depth": {"type": "integer", "minimum": 1, "maximum": MAX_DEPTH, "default": 1},
                    "limit": {"type": "integer", "default": 20}}, "additionalProperties": False},
                "annotations": {"readOnlyHint": True},
            },
            {
                "name": "hub_ref_link",
                "description": "Link two records of two apps so each finds the other. Keywords: enlazar, vincular, relacionar, link.\n"
                               "Both are hoard://<app>/<kind>/<id>; rel names the link (related, purchase, source...). Idempotent.",
                "inputSchema": {"type": "object", "properties": {
                    "from": {"type": "string", "description": "hoard://<app>/<kind>/<id>"},
                    "to": {"type": "string", "description": "hoard://<app>/<kind>/<id>"},
                    "rel": {"type": "string", "default": "related"},
                    "from_label": {"type": "string"}, "to_label": {"type": "string"}, "note": {"type": "string"}},
                    "required": ["from", "to"], "additionalProperties": False},
            },
        ]

    def handlers(self) -> dict[str, Any]:
        def refs(a: dict[str, Any]) -> dict[str, Any]:
            if a.get("uri"):
                return self.around(str(a["uri"]), int(a.get("depth") or 1))
            q = str(a.get("q") or "").strip()
            if q.startswith("hoard://"):
                return self.around(q, int(a.get("depth") or 1))
            if q:
                return {"ok": True, "q": q, "matches": self.search_labels(q, int(a.get("limit") or 20))}
            return {"ok": True, "edges": self.recent(int(a.get("limit") or 20))}

        def ref_link(a: dict[str, Any]) -> dict[str, Any]:
            res = self.link(a.get("from") or "", a.get("to") or "", a.get("rel") or "related", from_label=a.get("from_label") or "",
                            to_label=a.get("to_label") or "", note=a.get("note") or "", by="hub")
            res.pop("status", None)
            return res

        return {"hub_refs": refs, "hub_ref_link": ref_link}


__all__ = ["RefsFacet", "normalize_uri", "normalize_rel"]
