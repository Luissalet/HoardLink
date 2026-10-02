"""Global search: tool selection, result normalisation over many shapes, the parallel fan-out against fake
apps, the hub's own stores, ordering, timeouts and the HTTP/tool surface."""

from __future__ import annotations

import json

import pytest

from hoard_link.hub import tools
from hoard_link.hub.search import SearchFacet, find_list, normalise, normalise_item, pick_search_tools

from ._hub_fakes import FakeApp, RecordingNotify, http, make_hub, serve

RO = {"readOnlyHint": True}


def schema(**props):
    return {"type": "object", "properties": {k: {"type": v} for k, v in props.items()}}


# ---- choosing the tools -------------------------------------------------------------------------------

def test_pick_search_tools_rules():
    cat = [
        {"name": "notes_search", "inputSchema": schema(query="string", limit="integer"), "annotations": RO},
        {"name": "search_links", "inputSchema": schema(q="string")},                                   # no annotations: fine
        {"name": "search", "inputSchema": schema(text="string")},                                      # the plain one goes first
        {"name": "recall_search", "inputSchema": schema(term="string")},
        {"name": "docs_search", "inputSchema": schema(query="string"), "annotations": {"readOnlyHint": False}},   # changes things
        {"name": "files_search", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "folder": {"type": "string"}},
                                                  "required": ["query", "folder"]}},                    # needs more than a text
        {"name": "tags_search", "inputSchema": schema(name="string")},                                  # no text property
        {"name": "echo", "inputSchema": schema(query="string")},                                        # not a search by name
        {"name": "library_search", "inputSchema": {"type": "object", "properties": {"search": {"type": "string"}}, "required": ["search"]}},
        "junk",
    ]
    picked = pick_search_tools(cat)
    assert [t["name"] for t in picked] == ["search", "recall_search", "library_search"]      # max 3 per app, plain "search" first
    assert picked[0] == {"name": "search", "prop": "text", "limit": False}
    all_ = pick_search_tools(cat[:2] + cat[3:4])
    by = {t["name"]: t for t in all_}
    assert by["notes_search"] == {"name": "notes_search", "prop": "query", "limit": True}
    assert by["search_links"]["prop"] == "q" and by["search_links"]["limit"] is False
    assert pick_search_tools([]) == [] and pick_search_tools(None) == []


# ---- normalising answers -------------------------------------------------------------------------------

def test_normalise_shapes():
    base = "http://127.0.0.1:5000"
    r = normalise({"results": [{"title": "T", "snippet": "S", "id": 4, "url": "/n/4", "score": 0.9}]}, base)
    assert r == [{"title": "T", "snippet": "S", "id": "4", "url": base + "/n/4", "score": 0.9}]
    r = normalise({"links": [{"name": "Docs", "description": "<b>Read</b>  the docs", "href": "https://x.test/d", "link_id": 7}]}, base)
    assert r[0]["title"] == "Docs" and r[0]["snippet"] == "Read the docs" and r[0]["url"] == "https://x.test/d" and r[0]["id"] == "7"
    r = normalise({"cards": [{"front": "Capital of France?", "back": "Paris", "uid": "c1"}]})
    assert r[0] == {"title": "Capital of France?", "snippet": "Paris", "id": "c1", "url": ""}
    r = normalise([{"subject": "Invoice", "excerpt": "Total 20", "key": "k"}, {"label": "L", "text": "t"}, "plain string hit", 5, None])
    assert [x["title"] for x in r] == ["Invoice", "L", "plain string hit"]
    r = normalise({"ok": True, "total": 2, "data": [{"filename": "a.pdf", "summary": "x"}]})
    assert r[0]["title"] == "a.pdf"
    r = normalise({"result": {"matches": [{"title": "Deep"}]}})                      # one level down
    assert r[0]["title"] == "Deep"
    r = normalise({"whatever": 3, "stuff": [{"title": "Any list of dicts"}]})
    assert r[0]["title"] == "Any list of dicts"
    # JSON text and MCP-style content blocks
    r = normalise(json.dumps({"hits": [{"title": "From text"}]}))
    assert r[0]["title"] == "From text"
    r = normalise({"content": [{"type": "text", "text": json.dumps([{"title": "Blocks", "id": 1}])}]})
    assert r[0]["title"] == "Blocks"
    # no title: the snippet, then the id
    long_title = normalise_item({"text": "A long text with no title at all, more than ninety characters long so that it is cut " * 2})["title"]
    assert len(long_title) <= 90 and long_title.endswith("…")
    assert normalise_item({"id": 12})["title"] == "12"
    assert normalise_item({"description": "only text"})["title"] == "only text"
    assert normalise_item({"unrelated": 1}) is None and normalise_item(3) is None
    # refs and relative urls
    r = normalise_item({"title": "x", "uri": "hoard://ledger/tx/1"})
    assert r["ref"] == "hoard://ledger/tx/1" and r["url"] == ""
    assert normalise_item({"title": "x", "url": "#/n/1"}, base)["url"] == base + "#/n/1"
    # limit and non-lists
    assert len(normalise({"results": [{"title": str(i)} for i in range(20)]}, limit=5)) == 5
    assert normalise({"results": []}) == [] and normalise("just text") == [] and normalise(None) == [] and find_list({"a": 1}) is None


# ---- the fan-out ----------------------------------------------------------------------------------------

@pytest.fixture
def family(tmp_path):
    apps = {
        "notes": FakeApp("notes", [{"name": "notes_search", "inputSchema": schema(query="string", limit="integer"), "annotations": RO},
                                    {"name": "notes_add", "inputSchema": schema(text="string")}],
                         {"notes_search": lambda a: {"results": [{"title": "Shopping list", "snippet": "milk " + a["query"], "id": 1, "score": 0.4},
                                                                  {"title": "Gift ideas", "id": 2, "score": 0.9}][: a.get("limit", 9)]}}),
        "links": FakeApp("links", [{"name": "search_links", "inputSchema": schema(q="string")}],
                         {"search_links": lambda a: {"links": [{"name": "Docs for " + a["q"], "href": "/l/9", "description": "d"}]}}),
        "hypatia": FakeApp("hypatia", [{"name": "cards_search", "inputSchema": schema(text="string"), "annotations": RO}],
                           {"cards_search": lambda a: {"cards": [{"front": "Q " + a["text"], "back": "A", "uid": "c1"}] * 3}}),
        "funes": FakeApp("funes", [{"name": "recall_search", "inputSchema": schema(query="string")}],
                         {"recall_search": lambda a: {"content": [{"type": "text", "text": json.dumps([{"title": "Memory", "snippet": "x"}])}]}}),
        "broken": FakeApp("broken", [{"name": "search", "inputSchema": schema(query="string")}],
                          {"search": lambda a: (_ for _ in ()).throw(ValueError("index is rebuilding"))}),
        "empty": FakeApp("empty", [{"name": "search", "inputSchema": schema(query="string")}], {"search": lambda a: {"results": []}}),
        "nosearch": FakeApp("nosearch", [{"name": "echo", "inputSchema": schema(query="string")}], {}),
        "legacy": FakeApp("legacy", catalogue_status=404),
    }
    hub = make_hub(tmp_path, list(apps.values()), extra_apps=["down"])
    yield hub, apps
    hub.close()
    for a in apps.values():
        a.stop()


def groups_by_app(res):
    return {g["app"]: g for g in res["groups"]}


def test_search_fans_out_normalises_and_orders(family):
    hub, apps = family
    search = hub.facet("search")
    res = search.search("tea", limit=2)
    assert res["ok"] and res["q"] == "tea" and res["took_ms"] >= 0
    g = groups_by_app(res)
    # each app got its own argument name, and `limit` only where the schema has it
    assert apps["notes"].calls[-1]["arguments"] == {"query": "tea", "limit": 2}
    assert apps["links"].calls[-1]["arguments"] == {"q": "tea"}
    assert apps["hypatia"].calls[-1]["arguments"] == {"text": "tea"}
    assert [r["title"] for r in g["notes"]["results"]] == ["Shopping list", "Gift ideas"]
    assert g["notes"]["tool"] == "notes_search" and g["notes"]["name"] == "Notes's Hoard" and g["notes"]["ms"] >= 0
    assert g["links"]["results"][0] == {"title": "Docs for tea", "snippet": "d", "id": "", "url": hub.get("links").url + "/l/9"}
    assert len(g["hypatia"]["results"]) == 2                       # capped to the limit even when the app ignores it
    assert g["funes"]["results"][0]["title"] == "Memory"           # MCP-style content block
    assert g["broken"]["error"] == "index is rebuilding" and g["broken"]["results"] == []
    assert g["empty"]["results"] == [] and "error" not in g["empty"]
    assert "nosearch" not in g and "legacy" not in g and "down" not in g and "notes_add" not in json.dumps(res)
    reasons = {s["app"]: s["reason"] for s in res["skipped"]}
    assert reasons["nosearch"] == "no search tool" and "legacy" in reasons and "down" in reasons
    assert not any(c["name"] == "notes_add" for c in apps["notes"].calls)      # a non-search tool is never called
    # the best score first, apps with hits before empty ones, failures last
    order = [x["app"] for x in res["groups"] if not x.get("own")]
    assert order[0] == "notes"
    assert order.index("broken") > order.index("hypatia") and order.index("empty") > order.index("hypatia")
    assert res["hits"] == sum(len(x["results"]) for x in res["groups"])


def test_apps_filter_and_catalogue_cache(family):
    hub, apps = family
    search = hub.facet("search")
    res = search.search("x", apps=["links", "funes"])
    assert {g["app"] for g in res["groups"]} == {"links", "funes"}
    before = len(apps["links"].calls)
    search.search("y", apps=["links"])
    assert len(apps["links"].calls) == before + 1
    # the catalogue is cached: dropping the tool from the app does not matter until the cache expires
    apps["links"].tools = []
    assert groups_by_app(search.search("z", apps=["links"]))["links"]["results"]
    search.reset_cache()
    res = search.search("z", apps=["links"])
    assert res["groups"] == [] and res["skipped"][0]["reason"] == "no search tool"
    assert search.search("")["ok"] is False and search.search("   ")["status"] == 400


def test_a_slow_app_times_out_without_holding_the_others(tmp_path):
    slow = FakeApp("slow", [{"name": "search", "inputSchema": schema(query="string")}], {"search": lambda a: {"results": [{"title": "late"}]}},
                   delay=3.0)
    fast = FakeApp("fast", [{"name": "search", "inputSchema": schema(query="string")}], {"search": lambda a: {"results": [{"title": "now"}]}})
    hub = make_hub(tmp_path, [slow, fast])
    try:
        search = hub.facet("search")
        search.call_timeout_s = 0.6
        res = search.search("q")
        g = groups_by_app(res)
        assert g["fast"]["results"][0]["title"] == "now"
        assert g["slow"]["results"] == [] and g["slow"]["error"]
        assert res["took_ms"] < 2500
        assert [x["app"] for x in res["groups"] if not x.get("own")][0] == "fast"
    finally:
        hub.close()
        slow.stop()
        fast.stop()


# ---- the hub's own stores ----------------------------------------------------------------------------------

class FakeMail:
    id = "mailgate"

    def __init__(self):
        self.calls = []

    def search(self, q, sphere=None, days=30, limit=20):
        self.calls.append((q, sphere, days, limit))
        return [{"id": 5, "kind": "mail", "subject": "Your invoice", "from_name": "Shop", "snippet": "total 20 EUR", "score": 0.7},
                {"id": 6, "kind": "chat", "snippet": "invoice sent", "from_addr": "ann@x.test"}]


class FakeNotifyHistory(RecordingNotify):
    def history(self, limit=50, **kw):
        return {"items": [{"id": 1, "title": "Payment failed", "body": "Shop invoice", "url": "http://x/1"},
                          {"id": 2, "title": "Backup done", "body": "ok"}]}


def test_own_groups_mail_refs_and_notify(family):
    hub, _ = family
    hub._facets_by_id["mailgate"] = FakeMail()
    hub._facets_by_id["notify"] = FakeNotifyHistory()
    hub.facet("refs").link("hoard://ledger/tx/1", "hoard://kafka/document/2", "purchase", from_label="Invoice Shop 20 EUR")
    res = hub.facet("search").search("invoice", limit=5)
    g = groups_by_app(res)
    assert g["mail"]["own"] and [r["title"] for r in g["mail"]["results"]] == ["Your invoice", "invoice sent"]
    assert g["mail"]["results"][0]["snippet"].startswith("Shop")
    assert hub.facet("mailgate").calls[0] == ("invoice", None, 30, 5)
    assert g["refs"]["results"][0]["uri"] == "hoard://ledger/tx/1"
    assert [r["title"] for r in g["notify"]["results"]] == ["Payment failed"] and g["notify"]["results"][0]["url"] == "http://x/1"
    # asking for some apps only does not drag the hub's stores in, unless named
    assert not any(x.get("own") for x in hub.facet("search").search("invoice", apps=["notes"])["groups"])
    only = hub.facet("search").search("invoice", apps=["mail"])
    assert [x["app"] for x in only["groups"]] == ["mail"]
    # a facet that blows up becomes a group error, not a failed search
    class Boom:
        def search(self, *a, **k):
            raise RuntimeError("db locked")
    hub._facets_by_id["mailgate"] = Boom()
    res = hub.facet("search").search("invoice", apps=["mail"])
    assert res["ok"] and res["groups"][0]["error"].startswith("RuntimeError")
    # facets that are not there simply add nothing
    for fid in ("mailgate", "notify", "refs"):
        hub._facets_by_id.pop(fid, None)
    assert not any(x.get("own") for x in hub.facet("search").search("invoice")["groups"])


# ---- HTTP and tool ----------------------------------------------------------------------------------------------

def test_http_and_tool(family):
    hub, apps = family
    server = serve(hub)
    try:
        status, body = http(hub.config.url + "/api/search?q=gift&apps=notes,links&limit=1")
        assert status == 200 and body["ok"] and {g["app"] for g in body["groups"]} == {"notes", "links"}
        assert len(next(g for g in body["groups"] if g["app"] == "notes")["results"]) == 1
        status, body = http(hub.config.url + "/api/search")
        assert status == 400 and "q is required" in body["error"]
    finally:
        server.shutdown()
    tool = next(t for t in SearchFacet.tools() if t["name"] == "hub_search")
    assert tool["annotations"]["readOnlyHint"] and len(tool["description"].splitlines()[0]) <= 110
    res = tools.call(hub, "hub_search", {"q": "tea", "apps": "notes", "limit": 3})
    assert res["ok"] and res["groups"][0]["app"] == "notes" and "status" not in res
    assert tools.call(hub, "hub_search", {"q": ""})["ok"] is False


def test_finders_count_and_the_internet_does_not():
    from hoard_link.hub.search import pick_search_tools
    q = {"type": "object", "properties": {"query": {"type": "string"}}}
    t = {"type": "object", "properties": {"text": {"type": "string"}}}
    ro = {"readOnlyHint": True}
    tools = [
        {"name": "web_search", "inputSchema": q, "annotations": ro, "description": "Search the internet"},
        {"name": "studio_stock_search", "inputSchema": q, "annotations": ro},
        {"name": "find_people", "inputSchema": q, "annotations": ro},
        {"name": "shipments_list", "inputSchema": t, "annotations": ro},
        {"name": "media_list", "inputSchema": q},                       # does not say it only reads
        {"name": "mail_scan", "inputSchema": q, "annotations": {"readOnlyHint": False}},
        {"name": "doc_search", "inputSchema": q, "description": "Search the archived papers"},
    ]
    assert {p["name"] for p in pick_search_tools(tools)} == {"find_people", "shipments_list", "doc_search"}
