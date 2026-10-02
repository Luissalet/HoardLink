"""Chat sources: Slack Web API, Microsoft Graph (Teams chats, Outlook inbox, device-code login) and JSON-lines files, against local
fake servers on 127.0.0.1; the messages land in the mail gateway's table."""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

import pytest

from hoard_link.hub import tools as hub_tools
from hoard_link.hub.chats import (ChatSources, MASK, http_request, iso_utc, mask, parse_ts, slack_plain, slug)
from hoard_link.hub.facets import Request
from hoard_link.hub.server import make_server

from .test_hub_and_server import _http
from .test_mailgate import FakeSpheres, req


class FakeAPI:
    """A local HTTP server; ``handler(method, path, query, form, headers) -> (status, json body, extra headers)``."""

    def __init__(self, handler: Callable[..., tuple]):
        self.calls: list[dict[str, Any]] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # noqa: D102
                pass

            def _do(self, method):
                url = urllib.parse.urlsplit(self.path)
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n).decode() if n else ""
                form = {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}
                query = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
                call = {"method": method, "path": urllib.parse.unquote(url.path), "query": query, "form": form, "auth": self.headers.get("Authorization", "")}
                outer.calls.append(call)
                result = handler(call)
                status, body = result[0], result[1]
                extra = result[2] if len(result) > 2 else {}
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in extra.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                self._do("GET")

            def do_POST(self):  # noqa: N802
                self._do("POST")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()

    def named(self, name: str) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["path"].endswith("/" + name)]


NOW = time.time()


def sts(offset: float) -> str:
    return f"{NOW - 3600 + offset:.6f}"


# ---- a fake Slack ----------------------------------------------------------------------------------------

class SlackWorld:
    token = "xoxb-test-token-1234"

    def __init__(self):
        self.users = {"U2": "Ana", "U3": "Bob"}
        self.channels = [{"id": "C1", "name": "general", "is_member": True}, {"id": "C2", "name": "random", "is_member": False},
                         {"id": "C3", "name": "ops", "is_member": True}, {"id": "D1", "is_im": True, "user": "U2"}]
        self.history = {
            "C1": [{"ts": sts(1), "user": "U2", "text": "buenos días equipo"},
                   {"ts": sts(2), "user": "U3", "text": "<@UME> mira <http://x.com|el informe> &amp; <#C3|ops>"},
                   {"ts": sts(3), "user": "UME", "text": "mi propio mensaje"},
                   {"ts": sts(4), "user": "U2", "subtype": "channel_join", "text": "has joined"}],
            "C3": [{"ts": sts(5), "user": "U3", "text": "deploy hecho"}],
            "D1": [{"ts": sts(6), "user": "U2", "text": "¿tienes un minuto?"}],
        }
        self.limited = False
        self.pages = False

    def __call__(self, call):
        if call["auth"] != f"Bearer {self.token}":
            return 200, {"ok": False, "error": "invalid_auth"}
        if self.limited:
            return 429, {"ok": False, "error": "ratelimited"}, {"Retry-After": "120"}
        method, form = call["path"].rsplit("/", 1)[1], call["form"]
        if method == "auth.test":
            return 200, {"ok": True, "user_id": "UME", "user": "luis", "team": "Corp"}
        if method == "conversations.list":
            types = form.get("types", "")
            chans = [c for c in self.channels if not (c.get("is_im") and "im" not in types.split(","))]
            if self.pages:
                if not form.get("cursor"):
                    return 200, {"ok": True, "channels": chans[:2], "response_metadata": {"next_cursor": "p2"}}
                return 200, {"ok": True, "channels": chans[2:], "response_metadata": {"next_cursor": ""}}
            return 200, {"ok": True, "channels": chans}
        if method == "conversations.history":
            oldest = float(form.get("oldest") or 0)
            msgs = [m for m in self.history.get(form["channel"], []) if float(m["ts"]) > oldest]
            return 200, {"ok": True, "messages": sorted(msgs, key=lambda m: -float(m["ts"])), "has_more": False}
        if method == "users.info":
            name = self.users.get(form["user"])
            return 200, {"ok": True, "user": {"id": form["user"], "name": name.lower(), "profile": {"display_name": name}}} if name else (200, {"ok": False, "error": "user_not_found"})
        return 200, {"ok": False, "error": "unknown_method"}


# ---- a fake Microsoft Graph -----------------------------------------------------------------------------------

def giso(offset: float) -> str:
    return iso_utc(NOW - 3600 + offset)[:-1] + ".1234567Z"             # Graph sends 7 fractional digits


class GraphWorld:
    def __init__(self):
        self.tokens = {"tok1"}
        self.polls = 0
        self.refresh_ok = True
        self.limited = False
        self.chat_msgs = {
            "19:abc@thread": [{"id": "m2", "createdDateTime": giso(20), "messageType": "message", "from": {"user": {"id": "U-ANA", "displayName": "Ana"}},
                               "body": {"contentType": "html", "content": "<p>¿Comemos <b>hoy</b>?</p>"}, "mentions": []},
                              {"id": "m1", "createdDateTime": giso(10), "messageType": "message", "from": {"user": {"id": "ME", "displayName": "Luis"}},
                               "body": {"contentType": "text", "content": "mío"}, "mentions": []}],
            "19:grp@thread": [{"id": "g2", "createdDateTime": giso(40), "messageType": "message", "from": {"user": {"id": "U-BOB", "displayName": "Bob"}},
                               "body": {"contentType": "text", "content": "@Luis revisa"}, "mentions": [{"mentioned": {"user": {"id": "ME"}}}]},
                              {"id": "g1", "createdDateTime": giso(30), "messageType": "systemEventMessage", "from": None,
                               "body": {"contentType": "text", "content": "Bob joined"}, "mentions": []}],
        }
        self.mail = [{"id": "o2", "internetMessageId": "<o2@corp>", "subject": "Tu factura", "receivedDateTime": giso(60), "hasAttachments": True,
                      "from": {"emailAddress": {"name": "Pagos", "address": "pagos@proveedor.es"}}, "toRecipients": [{"emailAddress": {"address": "luis@corp.com"}}],
                      "ccRecipients": [], "conversationId": "conv1", "body": {"contentType": "html", "content": "<p>Importe 10 EUR <a href='https://pay.es/1'>pagar</a></p>"}},
                     {"id": "o1", "internetMessageId": "<o1@corp>", "subject": "Reunión", "receivedDateTime": giso(50), "hasAttachments": False,
                      "from": {"emailAddress": {"name": "Jefe", "address": "boss@corp.com"}}, "toRecipients": [], "ccRecipients": [],
                      "conversationId": "conv2", "body": {"contentType": "text", "content": "a las 10"}}]

    def __call__(self, call):
        path, auth, query = call["path"], call["auth"], call["query"]
        if path.endswith("/devicecode"):
            return 200, {"device_code": "DEV123", "user_code": "ABCD-EFGH", "verification_uri": "https://microsoft.com/devicelogin", "expires_in": 900,
                         "interval": 5, "message": "go there"}
        if path.endswith("/token"):
            form = call["form"]
            if form.get("grant_type") == "refresh_token":
                if not self.refresh_ok:
                    return 400, {"error": "invalid_grant"}
                self.tokens.add("tok-refreshed")
                return 200, {"access_token": "tok-refreshed", "refresh_token": "r2", "expires_in": 3600}
            self.polls += 1
            if self.polls < 3:
                return 400, {"error": "authorization_pending"}
            self.tokens.add("tok-login")
            return 200, {"access_token": "tok-login", "refresh_token": "r-login", "expires_in": 3600}
        if auth.replace("Bearer ", "") not in self.tokens:
            return 401, {"error": {"code": "InvalidAuthenticationToken"}}
        if self.limited:
            return 429, {"error": {"code": "TooManyRequests"}}, {"Retry-After": "90"}
        if path.endswith("/me"):
            return 200, {"id": "ME", "displayName": "Luis", "userPrincipalName": "luis@corp.com", "mail": "luis@corp.com"}
        if path.endswith("/me/chats"):
            return 200, {"value": [{"id": "19:abc@thread", "chatType": "oneOnOne", "topic": None, "lastMessagePreview": {"createdDateTime": giso(20)}},
                                   {"id": "19:grp@thread", "chatType": "group", "topic": "Proyecto X", "lastMessagePreview": {"createdDateTime": giso(40)}}]}
        if path.endswith("/messages") and "/chats/" in path:
            cid = path.split("/chats/")[1].rsplit("/messages", 1)[0]
            return 200, {"value": self.chat_msgs.get(cid, [])}
        if path.endswith("/me/mailFolders/inbox/messages"):
            assert "receivedDateTime gt" in query.get("$filter", "") and query.get("$orderby") == "receivedDateTime desc"
            return 200, {"value": self.mail}
        if "/attachments" in path:
            return 200, {"value": [{"name": "factura.pdf", "contentType": "application/pdf", "size": 1234}]}
        return 404, {"error": {"code": "NotFound"}}


@pytest.fixture
def chats(hub):
    g = hub.facet("chats")
    assert isinstance(g, ChatSources)
    g.background = False
    hub._facets_by_id["spheres"] = FakeSpheres()
    return g


@pytest.fixture
def slack(chats):
    world = SlackWorld()
    api = FakeAPI(world)
    world.api = api
    yield world
    api.close()


@pytest.fixture
def graph(chats):
    world = GraphWorld()
    api = FakeAPI(world)
    world.api = api
    yield world
    api.close()


def post(chats, path, body=None, caller="ui"):
    return chats.post(req("POST", path, caller=caller, body=body or {}))


def add_slack(chats, slack, **kw):
    cfg = {"id": "slack-work", "kind": "slack", "name": "Work Slack", "token": slack.token, "api_base": slack.api.url + "/api", "enabled": True}
    cfg.update(kw)
    res = post(chats, "/api/chats/sources", cfg)
    assert res["ok"], res
    return res["source"]


def rows(chats, **kw):
    return chats.store.query(kind="chat", limit=100, **kw)


# ---- pure helpers -------------------------------------------------------------------------------------------

def test_helpers():
    assert mask("xoxb-1234567890-abcd") == MASK + "abcd" and mask("short") == MASK and mask("") == ""
    assert slug("Work Slack!") == "work-slack" and slug("¡¡") == "source"
    assert parse_ts("1700000000.000200") == pytest.approx(1700000000.0002)
    assert parse_ts("2026-10-02T10:00:00.1234567Z") == pytest.approx(parse_ts("2026-10-02T10:00:00.123456+00:00"))
    assert parse_ts(1700000000000) == 1700000000.0 and parse_ts("") == 0.0 and parse_ts("garbage") == 0.0
    assert slack_plain("<@U3> mira <http://x.com|el informe> &amp; <#C3|ops> <!here> <https://y.z>", {"U3": "Bob"}) == \
        "@Bob mira el informe (http://x.com) & #ops @here https://y.z"


def test_http_request_reports_network_errors_without_raising_other_things():
    from hoard_link.hub.chats import ChatError
    with pytest.raises(ChatError):
        http_request("http://127.0.0.1:1/x", timeout=1)


# ---- source management ----------------------------------------------------------------------------------------

def test_sources_are_validated_masked_and_secrets_survive_edits(chats):
    res = post(chats, "/api/chats/sources", {"kind": "slack", "name": "Work Slack", "token": "xoxb-secret-0042", "channels": "general, #ops"})
    assert res["ok"] and res["source"]["id"] == "work-slack" and res["source"]["token"] == MASK + "0042"
    assert res["source"]["channels"] == ["general", "#ops"] and res["source"]["interval_min"] == 5 and res["source"]["enabled"] is True
    stored = json.loads(Path(chats.path).read_text())[0]
    assert stored["token"] == "xoxb-secret-0042"                                     # on disk it is the real one
    listed = chats.get(req("GET", "/api/chats/sources"))["sources"]
    assert listed[0]["token"] == MASK + "0042" and "xoxb-secret" not in json.dumps(listed)
    # the page sends the masked value back unchanged: the stored secret stays
    res = post(chats, "/api/chats/sources", {"id": "work-slack", "token": MASK + "0042", "interval_min": 9})
    assert res["ok"] and json.loads(Path(chats.path).read_text())[0]["token"] == "xoxb-secret-0042" and res["source"]["interval_min"] == 9
    assert post(chats, "/api/chats/sources", {"id": "work-slack", "token": "xoxb-new-9999"})["source"]["token"] == MASK + "9999"
    # a second source with the same name gets its own id
    assert post(chats, "/api/chats/sources", {"kind": "slack", "name": "Work Slack", "token": "x"})["source"]["id"] == "work-slack-2"
    # validation
    assert post(chats, "/api/chats/sources", {"kind": "irc", "name": "x"})["status"] == 400
    assert post(chats, "/api/chats/sources", {"kind": "jsonl", "name": "x"})["status"] == 400
    assert post(chats, "/api/chats/sources", {"kind": "slack", "name": "x", "api_base": "ftp://nope"})["status"] == 400
    assert post(chats, "/api/chats/sources", {"kind": "slack", "id": "Bad Id!", "name": "x"})["status"] == 400
    assert post(chats, "/api/chats/sources", {"id": "work-slack", "kind": "graph"})["status"] == 400            # kind cannot change
    # removal
    assert post(chats, "/api/chats/sources/remove", {"id": "work-slack-2"})["ok"]
    assert post(chats, "/api/chats/sources/remove", {"id": "nope"})["status"] == 404
    assert [s["id"] for s in chats.sources()] == ["work-slack"]


def test_only_the_page_and_the_hub_manage_sources(chats):
    assert chats.get(req("GET", "/api/chats/sources", caller="ledger"))["status"] == 403
    assert chats.get(req("GET", "/api/chats/sources", caller=None))["status"] == 401
    assert post(chats, "/api/chats/sources", {"kind": "jsonl", "name": "x", "path": "/tmp/x"}, caller="ledger")["status"] == 403
    assert post(chats, "/api/chats/fetch", {}, caller="ledger")["status"] == 403
    assert post(chats, "/api/chats/sources", {"kind": "jsonl", "name": "x", "path": "/tmp/x"}, caller="hub")["ok"]
    assert chats.get(req("GET", "/api/other")) is None and chats.post(req("POST", "/api/other")) is None


# ---- Slack -----------------------------------------------------------------------------------------------------------

def test_slack_fetch_stores_classifies_and_emits(chats, slack, hub):
    add_slack(chats, slack)
    res = post(chats, "/api/chats/fetch", {"id": "slack-work"})
    assert res["ok"] and res["new"] == 3                                  # general x2 (own message and join skipped) + ops; DMs off; C2 not a member
    assert {r["text"] for r in rows(chats)} == {"buenos días equipo", "@luis mira el informe (http://x.com) & #ops", "deploy hecho"}
    mention = next(r for r in rows(chats) if "informe" in r["text"])
    assert mention["priority"] == "attention" and mention["reasons"] == ["mention"]
    assert mention["subject"] == "#general" and mention["folder"] == "C1" and mention["from_name"] == "Bob" and mention["sphere"] == "work"
    plain = next(r for r in rows(chats) if r["text"] == "buenos días equipo")
    assert plain["priority"] == "normal" and plain["from_name"] == "Ana"
    assert not any(r["subject"].startswith("DM") for r in rows(chats))
    evs = hub.events.query(type="chat.received", limit=10)
    assert len(evs) == 3
    ev = next(e["data"] for e in evs if e["data"]["channel"] == "#general" and e["data"]["priority"] == "attention")
    assert ev["source"] == "slack-work" and ev["sphere"] == "work" and ev["from"] == "Bob" and ev["text"].startswith("@luis") and len(ev["text"]) <= 120
    # auth.test filled in `me`, persisted in the source
    assert json.loads(Path(chats.path).read_text())[0]["me"] == "UME"


def test_slack_second_fetch_uses_the_watermark_and_never_duplicates(chats, slack):
    add_slack(chats, slack)
    post(chats, "/api/chats/fetch", {"id": "slack-work"})
    slack.history["C1"].insert(0, {"ts": sts(100), "user": "U2", "text": "mensaje nuevo"})
    n_calls = len(slack.api.calls)
    res = post(chats, "/api/chats/fetch", {"id": "slack-work"})
    assert res["ok"] and res["new"] == 1
    later = slack.api.calls[n_calls:]
    c1 = [c for c in later if c["path"].endswith("conversations.history") and c["form"]["channel"] == "C1"][0]
    assert float(c1["form"]["oldest"]) == pytest.approx(float(sts(4)))                       # the newest ts seen, skipped ones included
    assert len(rows(chats)) == 4
    assert post(chats, "/api/chats/fetch", {"id": "slack-work"})["new"] == 0


def test_slack_dms_pagination_and_channel_filter(chats, slack):
    slack.pages = True
    add_slack(chats, slack, include_dms=True)
    res = post(chats, "/api/chats/fetch", {"id": "slack-work"})
    assert res["ok"] and res["channels"] == 3
    dm = next(r for r in rows(chats) if r["folder"] == "D1")
    assert dm["subject"] == "DM · Ana" and dm["priority"] == "attention" and dm["reasons"] == ["direct"]        # a DM is "direct"
    assert len(slack.api.named("conversations.list")) == 2                                  # followed next_cursor
    # an explicit channel list: by name or by id
    add_slack(chats, slack, id="slack-ops", name="ops only", channels=["ops"], include_dms=False)
    post(chats, "/api/chats/fetch", {"id": "slack-ops"})
    ops_calls = [c for c in slack.api.calls if c["path"].endswith("conversations.history") and c["form"]["channel"] == "C3"]
    assert ops_calls


def test_slack_errors_and_rate_limit(chats, slack):
    add_slack(chats, slack, token="xoxb-wrong-0000")
    res = post(chats, "/api/chats/fetch", {"id": "slack-work"})
    assert res["ok"] is False and res["error"] == "invalid_auth" and res["status"] == 502
    st = chats.sources()[0]["state"]
    assert st["last_ok"] is False and st["last_error"] == "invalid_auth"
    add_slack(chats, slack)                                                                   # fix the token (same id: update)
    slack.limited = True
    res = post(chats, "/api/chats/fetch", {"id": "slack-work"})
    assert res["ok"] is False and "rate limited" in res["error"]
    assert chats.sources()[0]["state"]["retry_after_ts"] > time.time() + 100
    slack.limited = False
    skipped = post(chats, "/api/chats/fetch", {"id": "slack-work"})
    assert skipped["skipped"] == "rate-limited"                                              # backs off on its own...
    assert post(chats, "/api/chats/fetch", {"id": "slack-work", "force": True})["new"] == 3  # ...unless forced


def test_test_endpoint(chats, slack):
    add_slack(chats, slack, me="")
    ok = post(chats, "/api/chats/sources/slack-work/test")
    assert ok["ok"] and ok["detail"] == "luis @ Corp"
    add_slack(chats, slack, token="xoxb-wrong-0000")
    bad = post(chats, "/api/chats/sources/slack-work/test")
    assert bad["ok"] is False and bad["status"] == 502 and bad["error"] == "invalid_auth"
    assert post(chats, "/api/chats/sources/nope/test")["status"] == 404


def test_disabled_sources_are_not_fetched(chats, slack):
    add_slack(chats, slack, enabled=False)
    assert post(chats, "/api/chats/fetch", {"id": "slack-work"})["skipped"] == "disabled"
    assert slack.api.calls == []
    assert post(chats, "/api/chats/fetch", {})["results"] == []
    assert post(chats, "/api/chats/fetch", {"id": "slack-work", "force": True})["ok"] and slack.api.calls


# ---- Microsoft Graph ---------------------------------------------------------------------------------------------------

def add_graph(chats, graph, **kw):
    cfg = {"id": "ms365", "kind": "graph", "name": "Microsoft 365", "access_token": "tok1", "api_base": graph.api.url + "/v1.0",
           "login_base": graph.api.url, "teams_chats": True, "outlook": False, "enabled": True}
    cfg.update(kw)
    res = post(chats, "/api/chats/sources", cfg)
    assert res["ok"], res
    return res["source"]


def test_graph_teams_chats(chats, graph, hub):
    src = add_graph(chats, graph)
    assert src["access_token"] == MASK
    res = post(chats, "/api/chats/fetch", {"id": "ms365"})
    assert res["ok"] and res["new"] == 2                       # the one-to-one question and the group mention (own message, system event skipped)
    by = {r["text"]: r for r in rows(chats)}
    assert set(by) == {"¿Comemos hoy?", "@Luis revisa"}
    one = by["¿Comemos hoy?"]
    assert one["subject"] == "Ana" and one["priority"] == "attention" and one["reasons"] == ["direct"] and one["source"] == "ms365"
    grp = by["@Luis revisa"]
    assert grp["subject"] == "Proyecto X" and grp["reasons"] == ["mention"] and grp["from_name"] == "Bob"
    assert len(hub.events.query(type="chat.received", limit=10)) == 2
    # a second fetch: the chats' previews are not newer than the watermark, so the messages are not even asked for
    n = len(graph.api.calls)
    assert post(chats, "/api/chats/fetch", {"id": "ms365"})["new"] == 0
    assert not [c for c in graph.api.calls[n:] if "/messages" in c["path"]]
    assert chats.sources()[0]["state"]["me"] == "Luis"


def test_graph_outlook_inbox_is_mail_with_interests_and_events(chats, graph, hub):
    gate = hub.facet("mailgate")
    gate.background = False
    assert chats.store is gate.store                           # one table for both
    gate.post(req("POST", "/api/mail/interests", caller="ledger", body={"spec": {"subject_terms": ["factura"]}}))
    add_graph(chats, graph, teams_chats=False, outlook=True)
    res = post(chats, "/api/chats/fetch", {"id": "ms365"})
    assert res["ok"] and res["new"] == 2
    mail = {m["subject"]: m for m in gate.store.query(kind="mail", limit=10)}
    inv = mail["Tu factura"]
    assert inv["source"] == "ms365" and inv["message_id"] == "<o2@corp>" and inv["from_addr"] == "pagos@proveedor.es"
    assert inv["interests"] == ["ledger"] and "Importe 10 EUR" in inv["text"] and inv["links"][0]["url"] == "https://pay.es/1"
    assert inv["attachments"][0]["name"] == "factura.pdf" and inv["attachments"][0]["path"] == ""
    assert mail["Reunión"]["priority"] == "attention" and mail["Reunión"]["reasons"] == ["vip"]      # the boss is a VIP in the fake spheres
    evs = hub.events.query(type="mail.received", limit=10)
    assert {e["data"]["subject"] for e in evs} == {"Tu factura", "Reunión"}
    assert next(e for e in evs if e["data"]["subject"] == "Tu factura")["data"]["interests"] == ["ledger"]
    assert hub.events.query(type="chat.received", limit=5) == []
    # the same mailbox read again (or also through IMAP) never duplicates
    assert post(chats, "/api/chats/fetch", {"id": "ms365"})["new"] == 0
    assert gate.ingest({"message_id": "<o2@corp>", "subject": "Tu factura", "from_address": "x@y.z", "account": "Work"})["created"] is False


def test_graph_token_refresh_and_login_needed(chats, graph):
    # a rejected token is refreshed once and the call retried
    add_graph(chats, graph, access_token="stale", refresh_token="r1", client_id="cid", tenant="corp")
    res = post(chats, "/api/chats/fetch", {"id": "ms365"})
    assert res["ok"] and res["new"] == 2
    stored = json.loads(Path(chats.path).read_text())[0]
    assert stored["access_token"] == "tok-refreshed" and stored["refresh_token"] == "r2" and stored["expires_at"] > time.time() + 3000
    assert any(c["form"].get("grant_type") == "refresh_token" and c["path"].startswith("/corp/") for c in graph.api.calls)
    # nothing to refresh with: a clear error, flagged for the login button
    graph.tokens = set()
    add_graph(chats, graph, id="ms-no-refresh", access_token="stale")
    bad = post(chats, "/api/chats/fetch", {"id": "ms-no-refresh"})
    assert bad["ok"] is False and "login needed" in bad["error"]
    # a token that expires soon is refreshed before the call, not after a 401
    graph.tokens = {"tok1"}
    graph.refresh_ok = False
    add_graph(chats, graph, id="ms-expiring", access_token="tok1", refresh_token="r1", client_id="cid", expires_at=time.time() + 30)
    assert post(chats, "/api/chats/fetch", {"id": "ms-expiring"})["ok"] is True                # refresh failed, the token still works
    graph.limited = True
    add_graph(chats, graph, id="ms-limited")
    lim = post(chats, "/api/chats/fetch", {"id": "ms-limited"})
    assert lim["ok"] is False and "rate limited" in lim["error"]


def test_graph_device_code_login(chats, graph):
    chats.login_poll_s = 0.05
    add_graph(chats, graph, access_token="", client_id="cid-123", tenant="corp")
    res = post(chats, "/api/chats/sources/ms365/login")
    assert res["ok"] and res["user_code"] == "ABCD-EFGH" and res["verification_uri"] == "https://microsoft.com/devicelogin" and res["expires_in"] > 800
    pending = chats.sources()[0]["state"]["login"]
    assert pending["user_code"] == "ABCD-EFGH"
    deadline = time.time() + 10
    while time.time() < deadline and chats.sources()[0]["state"]["login"]["state"] == "pending":
        time.sleep(0.05)
    login = chats.sources()[0]["state"]["login"]
    assert login["state"] == "ok"
    stored = json.loads(Path(chats.path).read_text())[0]
    assert stored["access_token"] == "tok-login" and stored["refresh_token"] == "r-login"
    assert chats.sources()[0]["access_token"] == MASK + "ogin" and chats.sources()[0]["refresh_token"].startswith(MASK)
    dev = [c for c in graph.api.calls if c["path"] == "/corp/oauth2/v2.0/devicecode"][0]
    assert dev["form"]["client_id"] == "cid-123" and "Chat.Read" in dev["form"]["scope"] and "offline_access" in dev["form"]["scope"]
    tok = [c for c in graph.api.calls if c["path"] == "/corp/oauth2/v2.0/token"][-1]
    assert tok["form"]["grant_type"] == "urn:ietf:params:oauth:grant-type:device_code" and tok["form"]["device_code"] == "DEV123"
    assert post(chats, "/api/chats/fetch", {"id": "ms365"})["ok"] is True                    # the new token works


def test_login_errors(chats, graph):
    assert post(chats, "/api/chats/sources/ms365/login")["status"] == 404
    add_graph(chats, graph)
    assert post(chats, "/api/chats/sources/ms365/login")["status"] == 400                    # no client_id
    add_slack_src = post(chats, "/api/chats/sources", {"kind": "slack", "name": "s", "token": "x"})["source"]["id"]
    assert post(chats, f"/api/chats/sources/{add_slack_src}/login")["status"] == 400


def test_login_declined_and_slow_down(chats, graph):
    chats.login_poll_s = 0.03
    seen = []

    def handler(call):
        if call["path"].endswith("/devicecode"):
            return 200, {"device_code": "D", "user_code": "U", "verification_uri": "https://x", "expires_in": 60, "interval": 1}
        seen.append(1)
        return 400, {"error": "slow_down" if len(seen) == 1 else "authorization_declined"}

    api = FakeAPI(handler)
    try:
        add_graph(chats, graph, login_base=api.url, client_id="c")
        assert post(chats, "/api/chats/sources/ms365/login")["ok"]
        deadline = time.time() + 10
        while time.time() < deadline and chats.sources()[0]["state"]["login"]["state"] == "pending":
            time.sleep(0.05)
        assert chats.sources()[0]["state"]["login"]["state"] == "declined" and len(seen) == 2
    finally:
        api.close()


# ---- JSON lines ------------------------------------------------------------------------------------------------------------

def test_jsonl_incremental_partial_lines_and_rewrites(chats, tmp_path):
    f = tmp_path / "chat.jsonl"
    lines = [{"ts": NOW - 100, "channel": "dev", "from": "ana", "text": "hola"},
             {"ts": NOW - 90, "channel": "dev", "from": "bob", "text": "@luis ¿puedes?", "mentions_me": True},
             {"ts": NOW - 80, "channel": "dm-ana", "from": "ana", "text": "privado", "direct": True}]
    f.write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n" + json.dumps({"ts": "2026-10-02T10:00:00Z", "channel": "dev", "from": "eve", "text": "x"})[:20], encoding="utf-8")
    post(chats, "/api/chats/sources", {"id": "feed", "kind": "jsonl", "name": "Feed", "path": str(f), "sphere": "work"})
    res = post(chats, "/api/chats/fetch", {"id": "feed"})
    assert res["ok"] and res["new"] == 3 and res["seen"] == 3                         # the partial last line is left for next time
    got = {r["text"]: r for r in rows(chats)}
    assert got["hola"]["priority"] == "normal" and got["hola"]["sphere"] == "work" and got["hola"]["subject"] == "dev"
    assert got["@luis ¿puedes?"]["reasons"] == ["mention"] and got["privado"]["reasons"] == ["direct"]
    assert post(chats, "/api/chats/fetch", {"id": "feed"})["new"] == 0
    with open(f, "a", encoding="utf-8") as fh:                                         # finish the partial line and add one
        fh.write(json.dumps({"ts": "2026-10-02T10:00:00Z", "channel": "dev", "from": "eve", "text": "x"})[20:] + "\n")
        fh.write(json.dumps({"ts": NOW, "channel": "dev", "from": "ana", "text": "fin"}) + "\n")
    res = post(chats, "/api/chats/fetch", {"id": "feed"})
    assert res["new"] == 2
    f.write_text(json.dumps({"ts": NOW + 5, "channel": "dev", "from": "ana", "text": "tras rotar"}) + "\n", encoding="utf-8")   # rewritten, shorter
    assert post(chats, "/api/chats/fetch", {"id": "feed"})["new"] == 1
    f.unlink()
    gone = post(chats, "/api/chats/fetch", {"id": "feed"})
    assert gone["ok"] is False and "file not readable" in gone["error"]
    assert post(chats, "/api/chats/sources/feed/test")["status"] == 502


# ---- reading back: HTTP, python API, tools -------------------------------------------------------------------------------------

def seeded(chats, tmp_path):
    f = tmp_path / "chat.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in [
        {"ts": NOW - 50, "channel": "general", "from": "ana", "text": "comemos pizza hoy", "direct": True},
        {"ts": NOW - 40, "channel": "general", "from": "bob", "text": "vale"}]) + "\n", encoding="utf-8")
    g = tmp_path / "work.jsonl"
    g.write_text(json.dumps({"ts": NOW - 30, "channel": "ops", "from": "eve", "text": "incidente en producción", "mentions_me": True}) + "\n", encoding="utf-8")
    post(chats, "/api/chats/sources", {"id": "feed", "kind": "jsonl", "name": "Feed", "path": str(f)})
    post(chats, "/api/chats/sources", {"id": "slack-work", "kind": "jsonl", "name": "Work", "path": str(g)})     # id mapped to work by the spheres stub
    post(chats, "/api/chats/fetch", {})


def test_messages_api_python_api_and_sphere_rules(chats, tmp_path):
    seeded(chats, tmp_path)
    assert len(chats.recent()) == 3 and chats.recent("work")[0]["text"] == "incidente en producción"
    assert [m["text"] for m in chats.attention("personal")] == ["comemos pizza hoy"]
    assert [m["text"] for m in chats.attention(None)] == ["incidente en producción", "comemos pizza hoy"]
    assert [m["text"] for m in chats.search("PIZZA")] == ["comemos pizza hoy"] and chats.search("pizza", sphere="work") == []
    ui = chats.get(req("GET", "/api/chats/messages", query={"source": "feed"}))
    assert [m["text"] for m in ui["messages"]] == ["vale", "comemos pizza hoy"] and ui["last_id"] == max(m["id"] for m in ui["messages"])
    first = ui["messages"][1]
    assert set(first) >= {"id", "source", "sphere", "channel", "channel_label", "from_name", "text", "date_ts", "priority", "reasons"}
    cursor = chats.get(req("GET", "/api/chats/messages", query={"since_id": first["id"], "limit": 5}))
    assert [m["text"] for m in cursor["messages"]][0] == "vale"                       # since_id: ascending
    assert [m["text"] for m in chats.get(req("GET", "/api/chats/messages", query={"q": "incidente", "sphere": "work"}))["messages"]] == ["incidente en producción"]
    assert [m["text"] for m in chats.get(req("GET", "/api/chats/messages", query={"channel": "ops"}))["messages"]] == ["incidente en producción"]
    # an app only gets the chats of the spheres it is allowed in
    ledger = chats.get(req("GET", "/api/chats/messages", caller="ledger"))
    assert {m["sphere"] for m in ledger["messages"]} == {"personal"}
    assert chats.get(req("GET", "/api/chats/messages", caller="ledger", query={"sphere": "work"}))["status"] == 403
    assert {m["sphere"] for m in chats.get(req("GET", "/api/chats/messages", caller="people"))["messages"]} == {"personal", "work"}
    assert chats.get(req("GET", "/api/chats/messages", caller=None))["status"] == 401
    # chat messages are visible in the mail table's own search, as kind chat
    gate = chats.hub.facet("mailgate")
    assert [m["kind"] for m in gate.search("pizza")] == ["chat"] and gate.search("pizza", kind="mail") == []


def test_chat_tools(chats, tmp_path, hub):
    seeded(chats, tmp_path)
    tools = {t["name"]: t for t in ChatSources.tools()}
    assert set(tools) == {"hub_chat_recent", "hub_chat_search"}
    for t in tools.values():
        assert len(t["description"].split("\n")[0]) <= 110 and t["annotations"]["readOnlyHint"] is True
    assert {"hub_chat_recent", "hub_chat_search"} <= {t["name"] for t in hub_tools.all_tools()}
    r = hub_tools.call(hub, "hub_chat_recent", {"limit": 2})
    assert r["count"] == 2
    assert hub_tools.call(hub, "hub_chat_recent", {"sphere": "work"})["messages"][0]["channel_label"] == "ops"
    assert hub_tools.call(hub, "hub_chat_recent", {"source": "feed"})["count"] == 2
    assert hub_tools.call(hub, "hub_chat_search", {"q": "pizza"})["count"] == 1
    assert hub_tools.call(hub, "hub_chat_search", {"q": "pizza", "sphere": "work"})["count"] == 0


# ---- thread and server ---------------------------------------------------------------------------------------------------------

def test_fetch_thread_lifecycle(chats, tmp_path):
    chats.background = True
    chats.first_delay_s = 3600.0
    chats.start()
    assert chats._thread is None                                         # no sources: no thread
    f = tmp_path / "c.jsonl"
    f.write_text("", encoding="utf-8")
    post(chats, "/api/chats/sources", {"id": "feed", "kind": "jsonl", "name": "f", "path": str(f), "enabled": False})
    assert chats._thread is None                                         # nothing enabled
    post(chats, "/api/chats/sources", {"id": "feed", "enabled": True})
    t = chats._thread
    assert t is not None and t.is_alive()
    chats.close()
    assert not t.is_alive()


def test_the_loop_fetches_due_sources(chats, tmp_path):
    f = tmp_path / "c.jsonl"
    f.write_text(json.dumps({"ts": NOW, "channel": "a", "from": "b", "text": "desde el bucle"}) + "\n", encoding="utf-8")
    post(chats, "/api/chats/sources", {"id": "feed", "kind": "jsonl", "name": "f", "path": str(f), "interval_min": 1})
    chats.background = True
    chats.first_delay_s = 0.05
    chats.tick_s = 0.05
    chats._ensure_thread()
    deadline = time.time() + 5
    while time.time() < deadline and not rows(chats):
        time.sleep(0.05)
    chats.close()
    assert [r["text"] for r in rows(chats)] == ["desde el bucle"]


def test_fetch_all_only_due(chats, tmp_path):
    f = tmp_path / "c.jsonl"
    f.write_text("", encoding="utf-8")
    post(chats, "/api/chats/sources", {"id": "feed", "kind": "jsonl", "name": "f", "path": str(f), "interval_min": 5})
    assert len(chats.fetch_all(only_due=True)["results"]) == 1
    assert chats.fetch_all(only_due=True)["results"] == []               # fetched a moment ago, not due
    assert len(chats.fetch_all()["results"]) == 1


@pytest.fixture
def served(hub, chats):
    server = make_server(hub, port=hub.config.port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield hub.config.url
    server.shutdown()


def test_routes_through_the_real_server(served, chats, tmp_path, hub):
    ui = {"Sec-Fetch-Site": "same-origin"}
    assert _http(served + "/api/chats/sources")[0] == 401
    f = tmp_path / "c.jsonl"
    f.write_text(json.dumps({"ts": NOW, "channel": "a", "from": "b", "text": "hola desde http"}) + "\n", encoding="utf-8")
    st, body = _http(served + "/api/chats/sources", {"id": "feed", "kind": "jsonl", "name": "Feed", "path": str(f)}, headers=ui)
    assert st == 200 and body["source"]["id"] == "feed"
    st, body = _http(served + "/api/chats/sources", headers=ui)
    assert st == 200 and body["sources"][0]["state"]["last_fetch_ts"] is None
    st, body = _http(served + "/api/chats/fetch", {"id": "feed"}, headers=ui)
    assert st == 200 and body["new"] == 1
    st, body = _http(served + "/api/chats/messages?q=http", headers=ui)
    assert st == 200 and body["messages"][0]["text"] == "hola desde http"
    st, body = _http(served + "/api/chats/sources/feed/test", {}, headers=ui)
    assert st == 200 and body["ok"]
    assert _http(served + "/api/chats/sources/nope/test", {}, headers=ui)[0] == 404
    st, body = _http(served + "/api/chats/sources", {"kind": "bad"}, headers=ui)
    assert st == 400
    st, body = _http(served + "/api/facets", headers=ui)
    assert "chats.js" in [s for f_ in body["facets"] for s in f_["ui_scripts"]]
    st, body = _http(served + "/api/chats/sources/remove", {"id": "feed"}, headers={"Authorization": "Bearer " + hub.token})
    assert st == 200 and body["removed"]
