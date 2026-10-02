"""Global search — the ``search`` facet: one question, every app that can answer it.

Each app of the family has its own search tool (``notes_search``, ``recall_search``, ``cards_search``...).
The hub does not know their names in advance: it reads every app's tool catalogue (``hub.app_tools``,
cached 10 minutes), picks the read-only ones that look like a search (:func:`pick_search_tools`), calls
them in parallel (6 s each) and normalises whatever they answer (:func:`normalise`) into one shape,
``{title, snippet, id, url, score?}``. Besides the apps, the hub's own stores are searched: mail and chat
messages (``mailgate``), the labels of linked records (``refs``) and the notification history (``notify``).

``GET /api/search?q=&apps=a,b&limit=8`` -> ``{ok, q, groups: [{app, name, tool, ms, results, error?}], took_ms}``;
groups with hits first (best score, then most hits), apps that failed or found nothing after them.
"""

from __future__ import annotations

import json
import re
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Any, Optional

from .facets import Facet, Request

CALL_TIMEOUT_S = 6.0
CATALOGUE_TTL_S = 600.0
CATALOGUE_FAIL_TTL_S = 20.0
MAX_TOOLS_PER_APP = 3
EXACT_NAMES = ("search", "recall_search", "library_search")
QUERY_PROPS = ("query", "q", "text", "term", "search")
LIST_KEYS = ("results", "items", "hits", "matches", "links", "cards", "documents", "messages", "data", "records", "entries",
             "rows", "notes", "files", "pages", "list", "found")
TITLE_KEYS = ("title", "name", "subject", "front", "label", "filename", "heading", "headline")
SNIPPET_KEYS = ("snippet", "summary", "text", "excerpt", "back", "description", "content", "body", "preview")
ID_KEYS = ("id", "uid", "key", "slug")
URL_KEYS = ("url", "link", "href")
SCORE_KEYS = ("score", "relevance", "similarity")
OWN_GROUPS = ("mail", "refs", "notify")


def _fold(text: Any) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", str(text or "").lower()) if not unicodedata.combining(c))


# ---- choosing the tools ---------------------------------------------------------------------

#: Tools that look like a search but reach the internet (a web search, stock footage, a shop): a family search stays home.
_OUTSIDE_WORDS = ("web", "internet", "online", "stock_search", "hf_", "huggingface", "secondhand", "market_search")
_OUTSIDE_DESC = ("internet", "the web", "en la web", "online", "hugging face", "wallapop", "yahoo", "coingecko", "en línea")


def _is_search_name(name: str) -> bool:
    return name.endswith("_search") or name.startswith("search_") or name in EXACT_NAMES


def _is_find_name(name: str) -> bool:
    """Read-only finders and lists that take a text: ``find_people``, ``shipments_list(text)``, ``media_list(query)``."""
    return name.startswith("find_") or name.endswith("_find") or name.endswith("_list") or name.endswith("_assets")


def _reaches_outside(tool: dict[str, Any]) -> bool:
    name = str(tool.get("name") or "").lower()
    first = str(tool.get("description") or "").split("\n", 1)[0].lower()
    ann = tool.get("annotations") if isinstance(tool.get("annotations"), dict) else {}
    return bool(ann.get("openWorldHint")) or any(w in name for w in _OUTSIDE_WORDS) or any(w in first for w in _OUTSIDE_DESC)


def pick_search_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The tools of a catalogue the hub may call for a global search, best first:
    ``[{name, prop, limit}]`` — the name looks like a search, it is not marked as changing anything, it has an input
    property for the text (``query|q|text|term|search``) and needs nothing else."""
    out: list[dict[str, Any]] = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        name = str(t.get("name") or "")
        if not name or _reaches_outside(t):
            continue
        ann = t.get("annotations") if isinstance(t.get("annotations"), dict) else None
        if _is_search_name(name):
            if ann is not None and (ann.get("readOnlyHint") is False or ann.get("destructiveHint") is True):
                continue
        elif _is_find_name(name):
            if not (ann and ann.get("readOnlyHint") is True):     # a finder only when it says it only reads
                continue
        else:
            continue
        schema = t.get("inputSchema") or t.get("input_schema") or {}
        props = schema.get("properties") if isinstance(schema, dict) and isinstance(schema.get("properties"), dict) else {}
        prop = next((p for p in QUERY_PROPS if p in props), None)
        if prop is None:
            continue
        required = [r for r in (schema.get("required") or []) if isinstance(r, str)] if isinstance(schema, dict) else []
        if any(r not in (prop, "limit") for r in required):
            continue
        out.append({"name": name, "prop": prop, "limit": "limit" in props})
    # a plain "search" first, then the shortest names (the most general ones)
    out.sort(key=lambda t: (0 if t["name"] in EXACT_NAMES else 1, len(t["name"]), t["name"]))
    return out[:MAX_TOOLS_PER_APP]


# ---- reading the answers ---------------------------------------------------------------------

def _unwrap(result: Any) -> Any:
    """A tool answer as plain data: JSON text is parsed, an MCP-style ``{content: [{text}]}`` unwrapped."""
    for _ in range(3):
        if isinstance(result, str):
            text = result.strip()
            if text[:1] in "[{":
                try:
                    result = json.loads(text)
                    continue
                except ValueError:
                    pass
            return result
        if isinstance(result, dict) and isinstance(result.get("content"), list) and result["content"] \
                and all(isinstance(c, dict) and "text" in c for c in result["content"]):
            joined = "".join(str(c.get("text") or "") for c in result["content"])
            result = joined
            continue
        if isinstance(result, dict) and set(result) <= {"ok", "tool", "result", "name"} and "result" in result:
            result = result["result"]
            continue
        break
    return result


def find_list(result: Any) -> Optional[list[Any]]:
    """The first list in a tool answer: a known key (``results``, ``items``, ``hits``...), else any list of dicts,
    looking one level into ``result``/``data`` objects."""
    result = _unwrap(result)
    if isinstance(result, list):
        return result
    if not isinstance(result, dict):
        return None
    for k in LIST_KEYS:
        v = result.get(k)
        if isinstance(v, list):
            return v
    for v in result.values():
        if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
            return v
    for k in ("result", "data", "response", "payload"):
        inner = result.get(k)
        if isinstance(inner, (dict, list)):
            found = find_list(inner)
            if found is not None:
                return found
    for v in result.values():
        if isinstance(v, list):
            return v
    return None


def _clean(text: Any, limit: int) -> str:
    s = re.sub(r"<[^>]+>", " ", str(text))
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def _first(item: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for k in keys:
        v = item.get(k)
        if v not in (None, "", [], {}):
            return v
    return None


def normalise_item(item: Any, base_url: str = "") -> Optional[dict[str, Any]]:
    """One search hit as ``{title, snippet, id, url, score?, ref?}``; None when there is nothing to show."""
    if isinstance(item, str):
        text = _clean(item, 240)
        return {"title": text[:90], "snippet": text if len(text) > 90 else "", "id": "", "url": ""} if text else None
    if not isinstance(item, dict):
        return None
    title = _first(item, TITLE_KEYS)
    snippet = _first(item, SNIPPET_KEYS)
    rid = _first(item, ID_KEYS)
    if rid is None:
        for k, v in item.items():
            if k.endswith("_id") and isinstance(v, (str, int)) and v != "":
                rid = v
                break
    url = _first(item, URL_KEYS)
    ref = item.get("uri") or item.get("ref")
    if url is None and isinstance(ref, str) and not ref.startswith("hoard://"):
        url = ref
    if isinstance(title, (dict, list)):
        title = None
    if isinstance(snippet, (dict, list)):
        snippet = None
    if title is None and snippet is not None:
        title = _clean(snippet, 90)
    if title is None and rid is not None:
        title = str(rid)
    if title is None:
        return None
    out: dict[str, Any] = {"title": _clean(title, 160), "snippet": _clean(snippet, 240) if snippet is not None else "",
                           "id": "" if rid is None else str(rid), "url": ""}
    if isinstance(url, str) and url.strip():
        u = url.strip()
        if base_url and (u.startswith("/") or u.startswith("#") or u.startswith("?")):
            u = base_url.rstrip("/") + (u if u[0] != "?" else "/" + u)
        out["url"] = u
    for k in SCORE_KEYS:
        v = item.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out["score"] = round(float(v), 4)
            break
    if isinstance(ref, str) and ref.startswith("hoard://"):
        out["ref"] = ref
    return out


def normalise(result: Any, base_url: str = "", limit: int = 8) -> list[dict[str, Any]]:
    """A tool's answer -> a list of hits (any shape in, ``{title, snippet, id, url, score?}`` out)."""
    items = find_list(result)
    if not items:
        return []
    out = []
    for it in items:
        hit = normalise_item(it, base_url)
        if hit is not None:
            out.append(hit)
        if len(out) >= limit:
            break
    return out


def _best_score(results: list[dict[str, Any]]) -> float:
    scores = [r["score"] for r in results if isinstance(r.get("score"), (int, float))]
    return max(scores) if scores else 0.0


# ---- the facet ---------------------------------------------------------------------------------

class SearchFacet(Facet):
    id = "search"
    ui_scripts = ("search.js",)

    def __init__(self, hub: Any):
        super().__init__(hub)
        self._lock = threading.Lock()
        self._catalogue: dict[str, tuple[float, list[dict[str, Any]], str]] = {}   # app id -> (ts, picked tools, error)
        self.call_timeout_s = CALL_TIMEOUT_S

    def _running_ids(self) -> Optional[set[str]]:
        """Apps whose server answers now (from the hub's snapshot, cached a few seconds); None when unknown."""
        now = time.monotonic()
        cached = getattr(self, "_running_cache", None)
        if cached and now - cached[0] < 10:
            return cached[1]
        try:
            snap = self.hub.snapshot()
            ids = {a["id"] for a in snap.get("apps", []) if a.get("state") in ("running", "foreign", "starting")}
        except Exception:  # noqa: BLE001
            ids = None
        self._running_cache = (now, ids)
        return ids

    def reset_cache(self) -> None:
        with self._lock:
            self._catalogue.clear()

    def _tools_of(self, app_id: str) -> tuple[list[dict[str, Any]], str]:
        now = time.monotonic()
        with self._lock:
            hit = self._catalogue.get(app_id)
        if hit and now - hit[0] < (CATALOGUE_TTL_S if not hit[2] else CATALOGUE_FAIL_TTL_S):
            return hit[1], hit[2]
        res = self.hub.app_tools(app_id)
        if res.get("ok") and isinstance(res.get("tools"), list):
            picked, err = pick_search_tools(res["tools"]), ""
        else:
            picked, err = [], str(res.get("error") or "not reachable")
        with self._lock:
            self._catalogue[app_id] = (now, picked, err)
        return picked, err

    # -- the hub's own stores ---------------------------------------------------------------
    def _own(self, q: str, limit: int, only: Optional[set[str]]) -> list[dict[str, Any]]:
        groups: list[dict[str, Any]] = []

        def add(gid: str, name: str, fn) -> None:
            if only is not None and gid not in only:
                return
            t0 = time.monotonic()
            try:
                results = fn()
                err = ""
            except Exception as exc:  # noqa: BLE001
                results, err = [], f"{type(exc).__name__}: {exc}"
            if results is None:
                return
            g = {"app": gid, "name": name, "tool": gid, "own": True, "ms": int((time.monotonic() - t0) * 1000),
                 "results": results[:limit]}
            if err:
                g["error"] = err
            groups.append(g)

        mail = self.hub.facet("mailgate")
        if mail is not None and callable(getattr(mail, "search", None)):
            def _mail() -> list[dict[str, Any]]:
                rows = mail.search(q, sphere=None, days=30, limit=limit) or []
                out = []
                for r in rows:
                    if not isinstance(r, dict):
                        continue
                    kind = r.get("kind") or "mail"
                    who = r.get("from_name") or r.get("from_addr") or r.get("from") or ""
                    out.append({"title": _clean(r.get("subject") or r.get("snippet") or who or "(sin asunto)", 160),
                                "snippet": _clean(" · ".join(x for x in (who, r.get("snippet") or r.get("text") or "") if x), 240),
                                "id": str(r.get("id") or r.get("mail_id") or ""), "url": "", "kind": kind,
                                **({"score": r["score"]} if isinstance(r.get("score"), (int, float)) else {})})
                return out
            add("mail", "Mail", _mail)
        refs = self.hub.facet("refs")
        if refs is not None and callable(getattr(refs, "search_labels", None)):
            add("refs", "Links", lambda: refs.search_labels(q, limit))
        notify = self.hub.facet("notify")
        if notify is not None:
            def _notify() -> Optional[list[dict[str, Any]]]:
                fn = getattr(notify, "search", None)
                rows: Any
                if callable(fn):
                    rows = fn(q, limit=limit)
                else:
                    hist = getattr(notify, "history", None)
                    if not callable(hist):
                        return None
                    rows = hist(limit=200)
                    rows = rows.get("items") if isinstance(rows, dict) else rows
                    words = [w for w in _fold(q).split() if w]
                    rows = [r for r in (rows or []) if isinstance(r, dict)
                            and all(w in _fold(f"{r.get('title', '')} {r.get('body', '')}") for w in words)]
                out = []
                for r in rows or []:
                    if isinstance(r, dict):
                        out.append({"title": _clean(r.get("title") or "", 160), "snippet": _clean(r.get("body") or "", 240),
                                    "id": str(r.get("id") or ""), "url": r.get("url") or ""})
                return out
            add("notify", "Notifications", _notify)
        return groups

    # -- the search -------------------------------------------------------------------------
    def search(self, q: str, apps: Optional[list[str]] = None, limit: int = 8) -> dict[str, Any]:
        t0 = time.monotonic()
        q = str(q or "").strip()
        if not q:
            return {"ok": False, "status": 400, "error": "q is required"}
        limit = max(1, min(int(limit or 8), 50))
        wanted = {str(a).strip() for a in (apps or []) if str(a).strip()} or None
        candidates = [a for a in self.hub.apps if a.agent_contract and (wanted is None or a.id in wanted)]
        running = self._running_ids()
        skipped_down = [a for a in candidates if running is not None and a.id not in running]
        candidates = [a for a in candidates if running is None or a.id in running]
        groups: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = [{"app": a.id, "reason": "not running"} for a in skipped_down]

        # 1. catalogues (parallel; cached)
        pool = ThreadPoolExecutor(max_workers=max(4, min(len(candidates) * 2 + 2, 32)), thread_name_prefix="hub-search")
        try:
            cat_futs = {a.id: pool.submit(self._tools_of, a.id) for a in candidates}
            wait(list(cat_futs.values()), timeout=self.call_timeout_s + 1.0)
            jobs = []
            for a in candidates:
                fut = cat_futs[a.id]
                if not fut.done():
                    skipped.append({"app": a.id, "reason": "catalogue timeout"})
                    continue
                picked, err = fut.result()
                if err:
                    skipped.append({"app": a.id, "reason": err})
                elif not picked:
                    skipped.append({"app": a.id, "reason": "no search tool"})
                for tool in picked:
                    args: dict[str, Any] = {tool["prop"]: q}
                    if tool["limit"]:
                        args["limit"] = limit
                    jobs.append((a, tool, args))

            # 2. the searches (parallel)
            def run(a: Any, tool: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
                t1 = time.monotonic()
                res = self.hub.call_app(a.id, tool["name"], args, caller="hub", timeout=self.call_timeout_s)
                g = {"app": a.id, "name": a.name, "tool": tool["name"], "ms": int((time.monotonic() - t1) * 1000), "results": []}
                if res.get("ok"):
                    g["results"] = normalise(res.get("result"), a.url, limit)
                else:
                    g["error"] = str(res.get("error") or f"HTTP {res.get('status')}")[:200]
                return g

            futs = {pool.submit(run, a, tool, args): (a, tool) for a, tool, args in jobs}
            wait(list(futs), timeout=self.call_timeout_s + 1.0)
            for fut, (a, tool) in futs.items():
                if fut.done():
                    try:
                        groups.append(fut.result())
                    except Exception as exc:  # noqa: BLE001
                        groups.append({"app": a.id, "name": a.name, "tool": tool["name"], "ms": 0, "results": [],
                                       "error": f"{type(exc).__name__}: {exc}"})
                else:
                    groups.append({"app": a.id, "name": a.name, "tool": tool["name"], "ms": int(self.call_timeout_s * 1000),
                                   "results": [], "error": "timeout"})
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

        # 3. the hub's own stores
        only = None
        if wanted is not None:
            only = {w for w in wanted if w in OWN_GROUPS}
            if not only:
                only = set()
        groups.extend(self._own(q, limit, only))

        groups.sort(key=lambda g: (0 if g["results"] else 1, 1 if g.get("error") else 0, -_best_score(g["results"]),
                                   -len(g["results"]), g["app"]))
        return {"ok": True, "q": q, "groups": groups, "hits": sum(len(g["results"]) for g in groups),
                "skipped": skipped, "took_ms": int((time.monotonic() - t0) * 1000)}

    # -- HTTP / tools ---------------------------------------------------------------------------
    def get(self, req: Request) -> Optional[Any]:
        if req.path != "/api/search":
            return None
        apps = [a for a in (req.q("apps") or "").split(",") if a.strip()]
        return self.search(req.q("q") or "", apps or None, req.q_int("limit", 8))

    @classmethod
    def tools(cls) -> list[dict[str, Any]]:
        return [{
            "name": "hub_search",
            "description": "Search every app and the hub's mail at once. Keywords: busca en todo, buscar, search everything, global.\n"
                           "Asks each running app's own search tool and groups the hits by app: title, snippet, id, url. "
                           "Use it to find where something is before opening one app.",
            "inputSchema": {"type": "object", "properties": {
                "q": {"type": "string", "description": "What to look for."},
                "apps": {"type": "array", "items": {"type": "string"}, "description": "Only these apps (ids); omit for all. "
                                                                                       "'mail', 'refs' and 'notify' are the hub's own."},
                "limit": {"type": "integer", "default": 8, "description": "Hits per app."}},
                "required": ["q"], "additionalProperties": False},
            "annotations": {"readOnlyHint": True},
        }]

    def handlers(self) -> dict[str, Any]:
        def hub_search(a: dict[str, Any]) -> dict[str, Any]:
            apps = a.get("apps")
            if isinstance(apps, str):
                apps = [x for x in apps.split(",") if x.strip()]
            res = self.search(str(a.get("q") or ""), apps or None, int(a.get("limit") or 8))
            res.pop("status", None)
            return res
        return {"hub_search": hub_search}


__all__ = ["SearchFacet", "pick_search_tools", "normalise", "normalise_item", "find_list"]
