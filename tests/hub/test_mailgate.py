"""The mail gateway facet: one incremental pass for the family, spheres, interests, claims, retention and the HTTP surface.

The helper process is a fake runner (a callable with the shape of ``subprocess.run``); the real helper file is tested in
test_mail_helper.py. A fake spheres facet stands in for the real one."""

from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from hoard_link.hub import facets as _facets
from hoard_link.hub import tools as hub_tools
from hoard_link.hub.facets import Reply, Request
from hoard_link.hub.mailgate import (MailGate, MailStore, domain_of, fold, guess_category, match_interest, normalize_spec)
from hoard_link.hub.server import make_server

from .test_hub_and_server import _http


class FakeSpheres:
    """The part of the spheres facet the gateway uses."""
    id = "spheres"

    def __init__(self):
        self.accounts = {"work@corp.com": "work", "Work": "work"}
        self.apps = {"personal": ["*"], "work": ["people"]}
        self.vip = {"boss@corp.com", "vip@example.com"}
        self.keywords = ["urgente"]

    def sphere_of_account(self, selector, address=""):
        for key in (selector, address):
            if key in self.accounts:
                return self.accounts[key]
        return "personal"

    def sphere_of_chat_source(self, source_id):
        return {"slack-work": "work"}.get(source_id, "personal")

    def classify(self, sphere_id, *, sender, subject="", text="", mentions_me=False, direct=False):
        reasons = []
        if any(v in sender.lower() for v in self.vip):
            reasons.append("vip")
        if any(k in (subject + " " + text).lower() for k in self.keywords):
            reasons.append("keyword")
        if mentions_me:
            reasons.append("mention")
        if direct:
            reasons.append("direct")
        return {"priority": "attention" if reasons else "normal", "reasons": reasons}

    def app_allowed(self, sphere_id, app_id):
        allowed = self.apps.get(sphere_id, [])
        return "*" in allowed or app_id in allowed

    def list(self):
        return [{"id": "personal"}, {"id": "work"}]


class FakeRunner:
    """A ``subprocess.run`` stand-in: records each request and answers with what the test queued."""

    def __init__(self):
        self.requests: list[dict] = []
        self.cmds: list[list[str]] = []
        self.answers: list = []                  # dicts, or exceptions to raise

    def __call__(self, cmd, **kw):
        self.cmds.append(cmd)
        self.requests.append(json.loads(kw["input"]))
        answer = self.answers.pop(0) if self.answers else {"ok": True, "error": "", "accounts": [], "messages": []}
        if isinstance(answer, Exception):
            raise answer
        out = answer if isinstance(answer, str) else "noise line\n" + json.dumps(answer) + "\n"
        return SimpleNamespace(stdout=out, returncode=0)


def rec(uid, subject="Hola", frm="ana@example.com", account="Personal", text="", **kw):
    r = {"message_id": f"<{account}-{uid}@x>", "subject": subject, "from_name": "", "from_address": frm, "date_ts": time.time() - 3600 + uid,
         "ts": time.time() - 3600 + uid, "text": text, "links": [], "attachments": [], "to": ["me@example.com"], "cc": [],
         "in_reply_to": "", "references": [], "account": account, "account_id": "1", "account_address": "me@example.com",
         "folder": "INBOX", "uid": uid, "from_self": False}
    r.update(kw)
    return r


def answer(messages, **last):
    accounts = {}
    for m in messages:
        a = accounts.setdefault(m["account"], {"account": m["account"], "address": m["account_address"], "folders": {}})
        f = a["folders"].setdefault(m["folder"], {"last_uid": 0, "validity": 42, "new": 0, "remaining": 0})
        f["last_uid"], f["new"] = max(f["last_uid"], m["uid"]), f["new"] + 1
    return {"ok": True, "error": "", "accounts": list(accounts.values()), "messages": messages}


def req(method, path, *, caller="ui", query=None, body=None) -> Request:
    return Request(method=method, path=path, query={k: [str(v)] for k, v in (query or {}).items()}, body=body or {},
                   caller=lambda: caller, agent=lambda: caller == "hub")


@pytest.fixture
def gate(hub, tmp_path):
    faustus = tmp_path / "faustus"
    (faustus / "mcp_servers").mkdir(parents=True)
    (faustus / "mcp_servers" / "email_server.py").write_text("", encoding="utf-8")
    (faustus / "venv" / "bin").mkdir(parents=True)
    (faustus / "venv" / "bin" / "python").write_text("", encoding="utf-8")
    hub.config.faustus_dir = str(faustus)
    g = hub.facet("mailgate")
    assert isinstance(g, MailGate)
    g.background = False
    g.runner = FakeRunner()
    hub._facets_by_id["spheres"] = FakeSpheres()
    return g


def enable(gate):
    return gate.post(req("POST", "/api/mail/config", body={"enabled": True, "interval_min": 0}))


# ---- pure functions ----------------------------------------------------------------------------

def test_fold_and_domain():
    assert fold("Facturación ÁÉÍ") == "facturacion aei"
    assert domain_of("Ana <Ana@Sub.Example.com>") == "sub.example.com" and domain_of("nope") == ""


def test_normalize_spec_cleans_and_rejects_bad_regex():
    spec = normalize_spec({"subject_terms": "factura, recibo", "from_domains": ["@Amazon.es", "amazon.es", ""], "regex": "pedido \\d+", "has_attachment": 1})
    assert spec["subject_terms"] == ["factura", "recibo"] and spec["from_domains"] == ["@Amazon.es", "amazon.es"]
    assert spec["has_attachment"] is True and spec["text_terms"] == [] and spec["from_addresses"] == []
    with pytest.raises(ValueError):
        normalize_spec({"regex": "(unclosed"})
    with pytest.raises(ValueError):
        normalize_spec("nope")


@pytest.mark.parametrize("spec,msg,expected", [
    ({"subject_terms": ["factura"]}, {"subject": "Tu FACTURA de octubre"}, True),
    ({"subject_terms": ["facturación"]}, {"subject": "Tu facturacion"}, True),            # accents folded both ways
    ({"subject_terms": ["factura"]}, {"subject": "Hola", "text": "factura adjunta"}, False),
    ({"text_terms": ["pedido"]}, {"subject": "x", "text": "Tu PEDIDO ha salido"}, True),
    ({"from_domains": ["amazon.es"]}, {"from_addr": "envios@amazon.es"}, True),
    ({"from_domains": ["@amazon.es"]}, {"from_addr": "envios@pedidos.amazon.es"}, True),   # subdomain
    ({"from_domains": ["amazon.es"]}, {"from_addr": "x@notamazon.es"}, False),
    ({"from_addresses": ["Ana@Example.com"]}, {"from_addr": "ana@example.com"}, True),
    ({"regex": r"pedido\s+#\d{4}"}, {"subject": "Pedido #1234 enviado"}, True),
    ({"regex": r"pedido\s+#\d{4}"}, {"subject": "Pedido #12"}, False),
    ({"has_attachment": True}, {"attachments": [{"name": "a.pdf"}]}, True),
    ({"has_attachment": True}, {"attachments": []}, False),
    ({"has_attachment": False}, {"attachments": [{"name": "a.pdf"}]}, False),            # false is not a criterion
    ({}, {"subject": "anything"}, False),
    ({"subject_terms": ["zzz"], "from_domains": ["x.com"]}, {"subject": "hola", "from_addr": "a@x.com"}, True),   # ANY criterion
])
def test_match_interest(spec, msg, expected):
    assert match_interest(spec, msg) is expected


@pytest.mark.parametrize("msg,category", [
    ({"from_addr": "no-reply@accounts.google.com", "subject": "Alerta de seguridad"}, "security"),
    ({"from_addr": "x@y.com", "subject": "Tu código de verificación es 123456"}, "security"),
    ({"from_addr": "notifications@github.com", "subject": "[repo] Pull request #4"}, "dev"),
    ({"from_addr": "x@y.com", "subject": "Build failed on main"}, "dev"),
    ({"from_addr": "notification@facebookmail.com", "subject": "Ana te ha etiquetado"}, "social"),
    ({"from_addr": "messages-noreply@linkedin.com", "subject": "Tienes 3 mensajes"}, "social"),
    ({"from_addr": "ofertas@tienda.es", "subject": "Black Friday: 40% de descuento"}, "promo"),
    ({"from_addr": "news@tienda.es", "subject": "Novedades de octubre"}, "promo"),
    ({"from_addr": "pepe@example.com", "subject": "¿Cenamos el sábado?"}, "other"),
])
def test_guess_category(msg, category):
    assert guess_category(msg) == category


# ---- the store ------------------------------------------------------------------------------------

def test_store_insert_dedupes_and_searches_without_accents(tmp_path):
    st = MailStore(str(tmp_path / "m.db"))
    a = st.insert_message(message_id="<1@x>", subject="Factura de Teléfono", from_addr="ana@x.com", text="importe 12,50 EUR", priority="attention")
    assert a["created"] and a["id"] == 1
    again = st.insert_message(message_id="<1@x>", subject="otra")
    assert again == {"id": 1, "created": False}
    st.insert_message(kind="chat", message_id="slack:1", subject="#general", text="comemos juntos?", sphere="work")
    assert [r["id"] for r in st.query(q="telefono factura")] == [1]                 # every word, folded
    assert [r["id"] for r in st.query(q="comemos", kind="chat")] == [2]
    assert st.query(q="comemos", kind="mail") == []
    assert [r["id"] for r in st.query(spheres=["work"])] == [2] and st.query(spheres=[]) == []
    assert st.query(q="100%") == []                                                  # LIKE wildcards are escaped
    assert st.counts() == {"total": 2, "mail": 1, "chat": 1, "attention": 1, "unclaimed": 1}
    st.set_state("k", {"a": 1})
    assert st.get_state("k") == {"a": 1} and st.get_state("missing", 7) == 7


def test_store_prune_drops_text_and_unreferenced_files_but_keeps_headers(tmp_path):
    now = [time.time()]
    st = MailStore(str(tmp_path / "m.db"), clock=lambda: now[0])
    att = tmp_path / "att"
    att.mkdir()
    old_sha, new_sha = "a" * 64, "b" * 64
    (att / f"{old_sha}.pdf").write_bytes(b"old")
    (att / f"{new_sha}.pdf").write_bytes(b"new")
    st.insert_message(message_id="<old>", subject="Vieja", text="cuerpo viejo", links=[{"url": "http://x"}],
                      attachments=[{"name": "a.pdf", "sha": old_sha, "path": str(att / f"{old_sha}.pdf")}])
    now[0] += 40 * 86400
    st.insert_message(message_id="<new>", subject="Nueva", text="cuerpo nuevo",
                      attachments=[{"name": "b.pdf", "sha": new_sha, "path": str(att / f"{new_sha}.pdf")}])
    res = st.prune(30, str(att))
    assert res == {"pruned": 1, "files_removed": 1}
    old, new = st.get(1), st.get(2)
    assert old["subject"] == "Vieja" and old["text"] == "" and old["links"] == [] and old["attachments"][0]["pruned"] is True
    assert "path" not in old["attachments"][0] and new["text"] == "cuerpo nuevo"
    assert not (att / f"{old_sha}.pdf").exists() and (att / f"{new_sha}.pdf").exists()
    assert old["snippet"] == "" and st.query(q="viejo") == []                         # the pruned text is no longer searchable
    assert [r["id"] for r in st.query(q="vieja")] == [1]


# ---- the pass ----------------------------------------------------------------------------------------

def test_disabled_gateway_never_runs_the_helper(gate):
    assert gate.config()["enabled"] is False
    res = gate.run_pass()
    assert res["ok"] is False and res["skipped"] == "disabled" and gate.runner.requests == []
    out = gate.post(req("POST", "/api/mail/fetch"))
    assert out["ok"] is False and out["status"] == 409
    assert gate.status()["ready"] is False and gate.status()["enabled"] is False


def test_status_without_faustus(hub):
    g = hub.facet("mailgate")
    hub.config.faustus_dir = None
    g.background = False
    st = g.status()
    assert st["ok"] and st["configured"] is False and st["counts"]["total"] == 0
    r = g.call_helper({"action": "status"})
    assert r["ok"] is False and "Faustus folder" in r["error"]


def test_pass_stores_classifies_spheres_and_emits(gate, hub):
    enable(gate)
    msgs = [rec(1, "Hola", "ana@example.com"),
            rec(2, "Cena", "vip@example.com"),
            rec(3, "Informe urgente", "boss@corp.com", account="Work", account_address="work@corp.com"),
            rec(4, "Nota", "me@example.com", from_self=True)]
    gate.runner.answers.append(answer(msgs))
    res = gate.run_pass()
    assert res["ok"] and res["new"] == 4 and res["seen"] == 4
    rows = {r["subject"]: r for r in gate.store.query(limit=10)}
    assert rows["Hola"]["sphere"] == "personal" and rows["Hola"]["priority"] == "normal"
    assert rows["Cena"]["priority"] == "attention" and rows["Cena"]["reasons"] == ["vip"]
    assert rows["Informe urgente"]["sphere"] == "work" and rows["Informe urgente"]["reasons"] == ["vip", "keyword"]
    assert rows["Nota"]["priority"] == "low" and "own mail" in rows["Nota"]["reasons"]
    events = hub.events.query(type="mail.received", limit=10)
    assert len(events) == 4
    ev = next(e for e in events if e["data"]["subject"] == "Informe urgente")
    assert ev["data"] == {"mail_id": rows["Informe urgente"]["id"], "sphere": "work", "source": "Work", "from_domain": "corp.com",
                          "subject": "Informe urgente", "priority": "attention", "interests": []}
    assert "text" not in ev["data"]
    # the request: first read uses since_days, the attachments folder is the hub's
    first = gate.runner.requests[0]
    assert first["action"] == "fetch" and first["since"] == {} and first["since_days"] == 14 and first["max"] == 300
    assert first["attachments_dir"].endswith("attachments")
    assert gate.runner.cmds[0][1].endswith("mail_helper.py") and gate.runner.cmds[0][0].endswith("python")


def test_second_pass_is_incremental_and_never_stores_twice(gate, hub):
    enable(gate)
    gate.runner.answers.append(answer([rec(1), rec(2), rec(5, account="Work", account_address="work@corp.com")]))
    gate.run_pass()
    gate.runner.answers.append(answer([rec(2), rec(6)]))               # 2 again (a helper that repeats), 6 new
    res = gate.run_pass()
    assert res["new"] == 1 and res["seen"] == 2
    second = gate.runner.requests[1]
    assert second["since"] == {"Personal": {"INBOX": 2}, "Work": {"INBOX": 5}}
    assert second["validity"] == {"Personal": {"INBOX": 42}, "Work": {"INBOX": 42}}
    assert len(hub.events.query(type="mail.received", limit=50)) == 4
    wm = gate.store.get_state("watermarks")
    assert wm["Personal"]["INBOX"]["uid"] == 6
    # a watermark never goes backwards
    gate.runner.answers.append({"ok": True, "error": "", "accounts": [{"account": "Personal", "folders": {"INBOX": {"last_uid": 1, "validity": 42}}}], "messages": []})
    gate.run_pass()
    assert gate.store.get_state("watermarks")["Personal"]["INBOX"]["uid"] == 6
    assert gate.store.get_state("last_pass")["ok"] and gate.status()["ready"] is True


def test_helper_failures_are_reported_not_raised(gate):
    enable(gate)
    for bad in (subprocess.TimeoutExpired("x", 1), OSError("boom"), "not json at all\n", {"ok": False, "error": "login failed"}):
        gate.runner.answers.append(bad)
        res = gate.run_pass()
        assert res["ok"] is False and res["error"]
    assert gate.status()["ready"] is False and gate.status()["last_pass"]["ok"] is False


def test_a_pass_already_running_is_refused(gate):
    enable(gate)
    gate._pass_lock.acquire()
    try:
        assert gate.run_pass()["error"] == "a pass is already running"
    finally:
        gate._pass_lock.release()


# ---- interests, spheres and what an app may see -----------------------------------------------------------

def seed(gate):
    enable(gate)
    gate.runner.answers.append(answer([
        rec(1, "Tu factura de octubre", "billing@tienda.es", text="importe"),
        rec(2, "Factura del cliente", "boss@corp.com", account="Work", account_address="work@corp.com"),
        rec(3, "Hola Pepe", "pepe@example.com"),
    ]))
    gate.run_pass()


def ids_of(res):
    return [m["subject"] for m in res["messages"]]


def test_an_app_only_sees_its_interest_in_its_spheres(gate):
    seed(gate)
    r = gate.post(req("POST", "/api/mail/interests", caller="ledger", body={"spec": {"subject_terms": ["factura"]}}))
    assert r["ok"] and r["app"] == "ledger" and r["reindexed"] >= 1               # registered AFTER the messages arrived
    got = gate.get(req("GET", "/api/mail/messages", caller="ledger", query={"since_id": 0}))
    assert ids_of(got) == ["Tu factura de octubre"]                             # work mail never reaches ledger
    assert got["last_id"] == got["messages"][0]["id"]
    # interest=0 widens to the app's spheres, not beyond
    wide = gate.get(req("GET", "/api/mail/messages", caller="ledger", query={"interest": 0, "kind": "mail"}))
    assert sorted(ids_of(wide)) == ["Hola Pepe", "Tu factura de octubre"]
    assert gate.get(req("GET", "/api/mail/messages", caller="ledger", query={"sphere": "work"}))["status"] == 403
    work_id = gate.store.query(spheres=["work"])[0]["id"]
    assert gate.get(req("GET", f"/api/mail/messages/{work_id}", caller="ledger"))["status"] == 404
    assert gate.get(req("GET", f"/api/mail/messages/{work_id}", caller="people"))["ok"] is True       # people IS allowed in work
    assert gate.get(req("GET", f"/api/mail/messages/{work_id}", caller="ui"))["ok"] is True
    # the page sees everything, in both spheres
    assert len(gate.get(req("GET", "/api/mail/messages", query={"kind": "mail"}))["messages"]) == 3
    assert [m["subject"] for m in gate.get(req("GET", "/api/mail/messages", query={"sphere": "work"}))["messages"]] == ["Factura del cliente"]


def test_new_mail_is_matched_at_ingest_and_listed_in_the_event(gate, hub):
    enable(gate)
    gate.post(req("POST", "/api/mail/interests", caller="phileas", body={"spec": {"from_domains": ["correos.es"], "regex": "env[ií]o \\d+"}}))
    gate.post(req("POST", "/api/mail/interests", caller="people", body={"spec": {"subject_terms": ["factura"]}}))
    gate.runner.answers.append(answer([rec(1, "Tu envío 123", "avisos@correos.es"), rec(2, "factura trabajo", "x@corp.com", account="Work", account_address="work@corp.com")]))
    gate.run_pass()
    evs = {e["data"]["subject"]: e["data"] for e in hub.events.query(type="mail.received", limit=10)}
    assert evs["Tu envío 123"]["interests"] == ["phileas"]
    assert evs["factura trabajo"]["interests"] == ["people"]                      # people is allowed in work, phileas is not
    mine = gate.get(req("GET", "/api/mail/interests", caller="phileas"))["interests"]
    assert [i["app"] for i in mine] == ["phileas"]
    assert {i["app"] for i in gate.get(req("GET", "/api/mail/interests"))["interests"]} == {"phileas", "people"}


def test_interest_validation_and_removal(gate):
    assert gate.post(req("POST", "/api/mail/interests", caller="ledger", body={"spec": {"regex": "("}}))["status"] == 400
    assert gate.post(req("POST", "/api/mail/interests", caller="ui", body={"spec": {}}))["status"] == 400       # the page must name the app
    assert gate.post(req("POST", "/api/mail/interests", caller="ui", body={"app": "kafka", "spec": {"has_attachment": True}}))["ok"]
    assert gate.post(req("POST", "/api/mail/interests/remove", caller="kafka"))["removed"] is True
    assert gate.get(req("GET", "/api/mail/interests"))["interests"] == []


def test_full_view_carries_text_links_and_attachment_urls(gate):
    enable(gate)
    sha = "c" * 64
    gate.runner.answers.append(answer([rec(1, "Con adjunto", text="mira esto", links=[{"url": "https://x.y", "label": "x"}],
                                           attachments=[{"name": "f.pdf", "mime": "application/pdf", "size": 3, "sha": sha, "path": "/tmp/f.pdf"}])]))
    gate.run_pass()
    plain = gate.get(req("GET", "/api/mail/messages"))["messages"][0]
    assert "text" not in plain and plain["n_attachments"] == 1 and plain["snippet"] == "mira esto"
    full = gate.get(req("GET", "/api/mail/messages", query={"full": 1}))["messages"][0]
    assert full["text"] == "mira esto" and full["links"][0]["url"] == "https://x.y"
    assert full["attachments"][0]["url"] == f"/api/mail/attachments/{sha}" and full["attachments"][0]["path"] == "/tmp/f.pdf"


def test_attachment_bytes_follow_the_sphere_rules(gate):
    enable(gate)
    sha_p, sha_w = "d" * 64, "e" * 64
    adir = Path(gate.attachments_dir)
    adir.mkdir(parents=True)
    (adir / f"{sha_p}.pdf").write_bytes(b"%PDF personal")
    (adir / f"{sha_w}.pdf").write_bytes(b"%PDF work")
    gate.runner.answers.append(answer([
        rec(1, "p", attachments=[{"name": "p.pdf", "sha": sha_p, "path": str(adir / f"{sha_p}.pdf"), "mime": "application/pdf", "size": 13}]),
        rec(2, "w", account="Work", account_address="work@corp.com",
            attachments=[{"name": "w.pdf", "sha": sha_w, "path": str(adir / f"{sha_w}.pdf"), "mime": "application/pdf", "size": 9}])]))
    gate.run_pass()
    ok = gate.get(req("GET", f"/api/mail/attachments/{sha_p}", caller="ledger"))
    assert isinstance(ok, Reply) and ok.body == b"%PDF personal" and ok.content_type == "application/pdf"
    assert ok.headers["X-Content-Type-Options"] == "nosniff"
    assert gate.get(req("GET", f"/api/mail/attachments/{sha_w}", caller="ledger"))["status"] == 404
    assert gate.get(req("GET", f"/api/mail/attachments/{sha_w}", caller="ui")).body == b"%PDF work"
    assert gate.get(req("GET", f"/api/mail/attachments/{'f' * 64}", caller="ui"))["status"] == 404


def test_claims_dismiss_attention_and_unclaimed(gate):
    enable(gate)
    gate.runner.answers.append(answer([
        rec(1, "Ana", "vip@example.com"), rec(2, "Cena", "vip@example.com"), rec(3, "Oferta", "ofertas@tienda.es", text="descuento"),
        rec(4, "GitHub", "notifications@github.com"), rec(5, "Trabajo urgente", "x@corp.com", account="Work", account_address="work@corp.com")]))
    gate.run_pass()
    assert sorted(m["subject"] for m in gate.attention("personal")) == ["Ana", "Cena"]
    assert [m["subject"] for m in gate.attention("work")] == ["Trabajo urgente"]
    assert len(gate.attention(None)) == 3
    ana = next(m for m in gate.attention("personal") if m["subject"] == "Ana")
    res = gate.post(req("POST", "/api/mail/claim", caller="kafka", body={"ids": [ana["id"], 9999], "kind": "doc", "ref": "hoard://kafka/document/3"}))
    assert res == {"ok": True, "claimed": 1, "skipped": 1}                                 # 9999 does not exist
    assert [m["subject"] for m in gate.attention("personal")] == ["Cena"]                     # claimed: no longer "needs you"
    un = gate.unclaimed("personal")
    assert sorted(m["subject"] for m in un) == ["Cena", "GitHub", "Oferta"]
    assert {m["subject"]: m["category"] for m in un}["Oferta"] == "promo" and {m["subject"]: m["category"] for m in un}["GitHub"] == "dev"
    msg = gate.get(req("GET", f"/api/mail/messages/{ana['id']}"))["message"]
    assert msg["claims"][0]["app"] == "kafka" and msg["claims"][0]["ref"] == "hoard://kafka/document/3"
    # an app cannot claim for another one, nor dismiss; it cannot claim mail of a sphere it is not allowed in
    assert gate.post(req("POST", "/api/mail/dismiss", caller="kafka", body={"ids": [ana["id"]]}))["status"] == 403
    work = gate.store.query(spheres=["work"])[0]["id"]
    assert gate.post(req("POST", "/api/mail/claim", caller="ledger", body={"ids": [work], "kind": "x", "ref": "r"})) == {"ok": True, "claimed": 0, "skipped": 1}
    cena = un[0] if un[0]["subject"] == "Cena" else next(m for m in un if m["subject"] == "Cena")
    assert gate.post(req("POST", "/api/mail/dismiss", body={"ids": [cena["id"]]})) == {"ok": True, "dismissed": 1}
    assert gate.attention("personal") == [] and "Cena" not in [m["subject"] for m in gate.unclaimed("personal")]
    assert gate.post(req("POST", "/api/mail/dismiss", body={"ids": [cena["id"]], "undo": True}))["dismissed"] == 1
    http_un = gate.get(req("GET", "/api/mail/unclaimed", query={"sphere": "personal"}))["messages"]
    assert {m["subject"] for m in http_un} == {"Cena", "GitHub", "Oferta"} and all("category" in m for m in http_un)
    assert [m["subject"] for m in gate.get(req("GET", "/api/mail/attention", query={"sphere": "personal"}))["messages"]] == ["Cena"]


def test_search_and_filters(gate):
    enable(gate)
    gate.runner.answers.append(answer([rec(1, "Reunión de presupuestos", "ana@example.com", text="traigo los números"),
                                       rec(2, "Cena", "luis@example.com"), rec(3, "Presupuesto trabajo", "x@corp.com", account="Work", account_address="work@corp.com")]))
    gate.run_pass()
    assert [m["subject"] for m in gate.search("reunion presupuestos")] == ["Reunión de presupuestos"]
    assert [m["subject"] for m in gate.search("presupuesto", sphere="work")] == ["Presupuesto trabajo"]
    assert gate.search("zzz") == []
    got = gate.get(req("GET", "/api/mail/messages", query={"q": "ana", "kind": "mail"}))
    assert [m["subject"] for m in got["messages"]] == ["Reunión de presupuestos"]
    one = gate.get(req("GET", "/api/mail/messages", query={"source": "Work"}))
    assert [m["subject"] for m in one["messages"]] == ["Presupuesto trabajo"]
    cursor = gate.get(req("GET", "/api/mail/messages", query={"since_id": 1, "limit": 1}))
    assert [m["id"] for m in cursor["messages"]] == [2] and cursor["last_id"] == 2        # since_id: ascending, resumable
    assert gate.get(req("GET", "/api/mail/messages", query={"since_id": 3}))["last_id"] == 3


def test_http_requires_a_family_caller_and_config_is_private(gate):
    assert gate.get(req("GET", "/api/mail/status", caller=None))["status"] == 401
    assert gate.post(req("POST", "/api/mail/claim", caller=None))["status"] == 401
    assert gate.post(req("POST", "/api/mail/config", caller="ledger", body={"enabled": True}))["status"] == 403
    assert gate.get(req("GET", "/api/mail/config", caller="ledger"))["status"] == 403
    bad = gate.post(req("POST", "/api/mail/config", body={"interval_min": "soon"}))
    assert bad["status"] == 400 and gate.config()["interval_min"] == 10
    cfg = gate.post(req("POST", "/api/mail/config", body={"enabled": True, "interval_min": 5, "retention_days": 30, "owner": "luis"}))["config"]
    assert cfg["enabled"] and cfg["interval_min"] == 5 and cfg["retention_days"] == 30 and cfg["owner"] == "luis"
    assert gate.get(req("GET", "/api/mail/nope")) is None and gate.get(req("GET", "/api/other")) is None
    assert json.loads(Path(gate.config_path).read_text())["owner"] == "luis"


def test_owner_is_passed_to_the_helper(gate):
    gate.post(req("POST", "/api/mail/config", body={"enabled": True, "owner": "luis", "interval_min": 0}))
    gate.run_pass()
    assert gate.runner.requests[0]["owner"] == "luis"


def test_retention_runs_with_the_pass(gate):
    enable(gate)
    gate.post(req("POST", "/api/mail/config", body={"retention_days": 1}))
    old = gate.store.insert_message(message_id="<old>", subject="vieja", text="cuerpo", fetched_ts=time.time() - 3 * 86400)
    gate.runner.answers.append(answer([rec(1)]))
    res = gate.run_pass()
    assert res["pruned"]["pruned"] == 1 and gate.store.get(old["id"])["text"] == ""


def test_send_mail_goes_through_the_helper_even_when_disabled(gate):
    gate.runner.answers.append({"ok": True, "error": "", "account": "Personal", "from": "me***@example.com", "to": ["me***@example.com"]})
    out = gate.send_mail("Aviso", "cuerpo", html="<b>cuerpo</b>")
    assert out["ok"] is True
    sent = gate.runner.requests[0]
    assert sent["action"] == "send" and sent["subject"] == "Aviso" and sent["text"] == "cuerpo" and sent["html"] == "<b>cuerpo</b>"
    assert "to" not in sent and sent["from_name"] == "Hoard Hub"                       # default recipient: the account's own address
    gate.runner.answers.append({"ok": True, "error": ""})
    gate.send_mail("x", "y", to=["a@b.co"])
    assert gate.runner.requests[1]["to"] == ["a@b.co"]
    assert gate.config()["enabled"] is False


def test_ingest_for_other_facets_and_store_is_shared(gate, hub):
    res = gate.ingest(rec(7, "Outlook mail", "boss@corp.com"), sphere="work", source="ms365")
    assert res["created"] and res["sphere"] == "work" and res["priority"] == "attention"
    assert gate.store.get(res["id"])["source"] == "ms365"
    chat = gate.store.insert_message(kind="chat", source="slack-1", message_id="slack:1:1", subject="#general", text="hola equipo")
    assert chat["created"] and [m["kind"] for m in gate.search("hola")] == ["chat"]
    assert gate.search("hola", kind="mail") == []


# ---- thread lifecycle -----------------------------------------------------------------------------------------

def test_background_thread_starts_only_when_enabled_and_stops_on_close(hub, gate):
    gate.background = True
    gate.first_delay_s = 3600.0                           # never fires inside the test
    gate.start()
    assert gate._thread is None                            # disabled: no thread
    gate.post(req("POST", "/api/mail/config", body={"enabled": True, "interval_min": 10}))
    t = gate._thread
    assert t is not None and t.is_alive() and gate.status()["next_pass_ts"] is not None
    gate.post(req("POST", "/api/mail/config", body={"interval_min": 0}))
    t.join(timeout=2)
    assert not t.is_alive()                                # interval 0: stopped, manual passes only
    gate.post(req("POST", "/api/mail/config", body={"interval_min": 5}))
    t2 = gate._thread
    assert t2 is not t and t2.is_alive()
    gate.close()
    assert not t2.is_alive()
    assert gate.runner.requests == []


def test_the_loop_runs_a_pass_on_its_clock(gate):
    enable(gate)
    gate.post(req("POST", "/api/mail/config", body={"interval_min": 1}))
    gate.background = True
    gate.first_delay_s = 0.05
    gate.runner.answers.append(answer([rec(1)]))
    gate._ensure_thread()
    deadline = time.time() + 5
    while time.time() < deadline and gate.store.counts()["total"] == 0:
        time.sleep(0.05)
    gate.close()
    assert gate.runner.requests and gate.runner.requests[0]["action"] == "fetch"


# ---- tools and the real server ---------------------------------------------------------------------------------

def test_tools_catalogue_shape():
    tools = {t["name"]: t for t in MailGate.tools()}
    assert set(tools) == {"hub_mail_status", "hub_mail_search", "hub_mail_get", "hub_mail_attention", "hub_mail_unclaimed", "hub_mail_fetch"}
    for t in tools.values():
        first = t["description"].split("\n")[0]
        assert len(first) <= 110, (t["name"], len(first))
        assert t["inputSchema"]["type"] == "object"
    for name in ("hub_mail_status", "hub_mail_search", "hub_mail_get", "hub_mail_attention", "hub_mail_unclaimed"):
        assert tools[name]["annotations"]["readOnlyHint"] is True
    assert "annotations" not in tools["hub_mail_fetch"]
    assert "hub_mail_search" in {t["name"] for t in hub_tools.all_tools()}


def test_tool_handlers(gate, hub):
    enable(gate)
    gate.runner.answers.append(answer([rec(1, "Cena con Ana", "vip@example.com", text="x" * 9000), rec(2, "Trabajo", "z@corp.com", account="Work", account_address="work@corp.com")]))
    gate.run_pass()
    found = hub_tools.call(hub, "hub_mail_search", {"q": "cena"})
    assert found["count"] == 1 and found["messages"][0]["subject"] == "Cena con Ana"
    assert hub_tools.call(hub, "hub_mail_search", {"q": "trabajo", "sphere": "personal"})["count"] == 0
    mid = found["messages"][0]["id"]
    got = hub_tools.call(hub, "hub_mail_get", {"id": mid})
    assert got["ok"] and len(got["message"]["text"]) == 8000 and got["message"]["truncated"] is True
    assert hub_tools.call(hub, "hub_mail_get", {"id": 999})["ok"] is False
    assert hub_tools.call(hub, "hub_mail_attention", {"sphere": "personal"})["count"] == 1
    assert hub_tools.call(hub, "hub_mail_unclaimed", {})["count"] == 2
    assert hub_tools.call(hub, "hub_mail_status", {})["counts"]["total"] == 2
    gate.runner.answers.append(answer([]))
    assert hub_tools.call(hub, "hub_mail_fetch", {})["ok"] is True


@pytest.fixture
def served(hub, gate):
    server = make_server(hub, port=hub.config.port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield hub.config.url
    server.shutdown()


def test_routes_through_the_real_server(served, gate, hub):
    ui = {"Sec-Fetch-Site": "same-origin"}
    assert _http(served + "/api/mail/status")[0] == 401
    st, body = _http(served + "/api/mail/status", headers=ui)
    assert st == 200 and body["enabled"] is False
    st, body = _http(served + "/api/mail/fetch", {}, headers=ui)
    assert st == 409
    st, body = _http(served + "/api/mail/config", {"enabled": True, "interval_min": 0}, headers=ui)
    assert st == 200 and body["config"]["enabled"] is True
    gate.runner.answers.append(answer([rec(1, "Hola", "vip@example.com")]))
    st, body = _http(served + "/api/mail/fetch", {}, headers=ui)
    assert st == 200 and body["new"] == 1
    st, body = _http(served + "/api/mail/messages?limit=5&sphere=personal", headers=ui)
    assert st == 200 and body["messages"][0]["subject"] == "Hola"
    tok = {"Authorization": "Bearer " + hub.token}
    st, body = _http(served + "/api/mail/attention?sphere=personal", headers=tok)
    assert st == 200 and len(body["messages"]) == 1
    st, body = _http(served + "/api/facets", headers=ui)
    assert "mail.js" in [s for f in body["facets"] for s in f["ui_scripts"]]
    st, body = _http(served + "/api/mail/messages/1", headers=ui)
    assert st == 200 and body["message"]["category"] == "other"
    assert _http(served + "/api/mail/messages/99", headers=ui)[0] == 404
