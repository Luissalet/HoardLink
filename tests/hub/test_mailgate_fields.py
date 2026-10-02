"""The 0.8 extras of the mail gateway: stored ``html`` / ``images`` / ``headers`` (returned only on request), the additive schema
migration, and the interest keys ``exclude`` / ``all_of`` / ``category``. The default answers must be the ones the gateway always gave."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from hoard_link import fam_mail
from hoard_link.hub.mailgate import MailStore, guess_category, match_interest, normalize_spec, parse_fields

from tests.test_fam_mail import world            # noqa: F401 - the hub + real HTTP + fake helper fixture
from .test_mailgate import FakeRunner, answer, gate, rec, req    # noqa: F401

MARKUP = ('<html><body><script type="application/ld+json">{"@context":"http://schema.org","@type":"FlightReservation",'
          '"reservationNumber":"ABC123"}</script><p>Your booking</p></body></html>')
PLAIN_HTML = "<html><body><p>Hola</p></body></html>"


# ---- the schema migration ---------------------------------------------------------------------------------------

OLD_SCHEMA = """
CREATE TABLE messages(
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL DEFAULT 'mail', source TEXT NOT NULL DEFAULT '', sphere TEXT NOT NULL DEFAULT 'personal',
  folder TEXT NOT NULL DEFAULT '', uid INTEGER NOT NULL DEFAULT 0, message_id TEXT NOT NULL UNIQUE, thread TEXT NOT NULL DEFAULT '',
  date_ts REAL NOT NULL DEFAULT 0, from_addr TEXT NOT NULL DEFAULT '', from_name TEXT NOT NULL DEFAULT '', to_json TEXT NOT NULL DEFAULT '[]',
  subject TEXT NOT NULL DEFAULT '', snippet TEXT NOT NULL DEFAULT '', text TEXT NOT NULL DEFAULT '', links_json TEXT NOT NULL DEFAULT '[]',
  attachments_json TEXT NOT NULL DEFAULT '[]', fetched_ts REAL NOT NULL DEFAULT 0, priority TEXT NOT NULL DEFAULT 'normal',
  reasons_json TEXT NOT NULL DEFAULT '[]', interests_json TEXT NOT NULL DEFAULT '[]', dismissed INTEGER NOT NULL DEFAULT 0,
  search_text TEXT NOT NULL DEFAULT '');
"""


def test_an_old_database_gets_the_new_columns_and_keeps_its_rows(tmp_path):
    path = tmp_path / "mail.db"
    db = sqlite3.connect(path)
    db.executescript(OLD_SCHEMA)
    db.execute("INSERT INTO messages(message_id, subject, text, from_addr, date_ts, fetched_ts) VALUES('<old@x>', 'Vieja', 'texto', 'a@b.co', 1, 1)")
    db.commit()
    db.close()
    for _ in range(2):                                                    # opening twice is harmless
        store = MailStore(str(path))
        cols = {r[1] for r in store._db.execute("PRAGMA table_info(messages)").fetchall()}
        assert {"html", "images_json", "headers_json"} <= cols
        row = store.query()[0]
        assert row["subject"] == "Vieja" and row["text"] == "texto" and row["images"] == [] and row["headers"] == {}
        assert "html" not in row and store.get(row["id"], with_html=True)["html"] == ""
        store.close()


def test_a_new_database_stores_and_returns_the_extras(tmp_path):
    store = MailStore(str(tmp_path / "mail.db"))
    out = store.insert_message(message_id="<n@x>", subject="Reserva", html=MARKUP, images=[{"alt": "logo", "src": "https://x/l.png"}, "junk", {}],
                               headers={"list_unsubscribe": "<mailto:u@x>", "one_click": True, "gmail_category": "promotions", "secret": "dropped"})
    assert out["created"]
    plain = store.get(out["id"])
    assert "html" not in plain and plain["images"] == [{"alt": "logo", "src": "https://x/l.png"}]
    assert plain["headers"] == {"list_unsubscribe": "<mailto:u@x>", "gmail_category": "promotions", "one_click": True}
    assert store.get(out["id"], with_html=True)["html"] == MARKUP
    big = store.insert_message(message_id="<big@x>", html="x" * 300_000, images=[{"alt": f"a{i}", "src": ""} for i in range(80)])
    row = store.get(big["id"], with_html=True)
    assert len(row["html"]) == 240_000 and len(row["images"]) == 40
    store.close()


def test_retention_drops_html_and_images_but_keeps_headers(tmp_path):
    clock = [time.time()]
    store = MailStore(str(tmp_path / "mail.db"), clock=lambda: clock[0])
    mid = store.insert_message(message_id="<r@x>", subject="S", text="body", html=MARKUP, images=[{"alt": "pic", "src": ""}],
                               headers={"list_unsubscribe": "<mailto:u@x>"})["id"]
    clock[0] += 90 * 86400
    assert store.prune(60)["pruned"] == 1
    row = store.get(mid, with_html=True)
    assert row["html"] == "" and row["images"] == [] and row["text"] == "" and row["headers"] == {"list_unsubscribe": "<mailto:u@x>"}
    store.close()


# ---- ingest and the HTTP surface ---------------------------------------------------------------------------------

def test_ingest_keeps_html_only_when_it_carries_markup(gate):            # noqa: F811
    a = gate.ingest(rec(1, "Reserva", html=MARKUP, images=[{"alt": "logo", "src": "https://x/l.png"}],
                        headers={"list_unsubscribe": "<mailto:u@x>", "gmail_category": "updates", "message_id": "<m1>"}))
    b = gate.ingest(rec(2, "Hola", html=PLAIN_HTML))
    assert gate.store.get(a["id"], with_html=True)["html"] == MARKUP
    assert gate.store.get(b["id"], with_html=True)["html"] == ""


def test_the_default_http_answer_is_unchanged_and_fields_add_what_was_asked(gate):   # noqa: F811
    first = gate.ingest(rec(1, "Reserva", html=MARKUP, images=[{"alt": "logo", "src": ""}], headers={"gmail_category": "updates"}))["id"]
    base = gate.get(req("GET", "/api/mail/messages", query={"full": 1}))["messages"][0]
    assert not ({"html", "images", "headers"} & set(base))
    only_html = gate.get(req("GET", "/api/mail/messages", query={"full": 1, "fields": "html"}))["messages"][0]
    assert only_html["html"] == MARKUP and "images" not in only_html and "headers" not in only_html
    everything = gate.get(req("GET", "/api/mail/messages", query={"fields": "all"}))["messages"][0]
    assert everything["html"] == MARKUP and everything["images"] == [{"alt": "logo", "src": ""}] and everything["headers"] == {"gmail_category": "updates"}
    junk = gate.get(req("GET", "/api/mail/messages", query={"fields": "text,bogus"}))["messages"][0]
    assert not ({"html", "images", "headers"} & set(junk))
    one = gate.get(req("GET", f"/api/mail/messages/{first}", query={"fields": "headers,images"}))["message"]
    assert one["headers"] == {"gmail_category": "updates"} and one["images"] and "html" not in one
    assert gate.get_message(first, fields="html")["html"] == MARKUP and "html" not in gate.get_message(first)
    assert parse_fields("html, images") == ["html", "images"] and parse_fields("all") == ["html", "images", "headers"] and parse_fields(None) == []


def test_a_pass_stores_what_the_helper_sent(gate):                         # noqa: F811
    gate.post(req("POST", "/api/mail/config", body={"enabled": True, "interval_min": 0}))
    gate.runner.answers.append(answer([rec(1, "Reserva", html=MARKUP, images=[{"alt": "logo", "src": "https://x/l.png"}],
                                           headers={"list_unsubscribe": "<mailto:u@x>", "one_click": True})]))
    assert gate.run_pass()["new"] == 1
    row = gate.store.query(with_html=True)[0]
    assert row["html"] == MARKUP and row["headers"]["one_click"] is True and row["images"][0]["alt"] == "logo"


def test_an_app_asks_for_the_extras_through_fam_mail(world):             # noqa: F811
    gate = world["gate"]
    gate.post(req("POST", "/api/mail/config", body={"enabled": True, "interval_min": 10}))
    gate.runner.answers.append(answer([rec(1, "Tu factura", "billing@tienda.es", html=MARKUP, images=[{"alt": "logo grande", "src": ""}],
                                           headers={"list_unsubscribe": "<mailto:u@x>", "gmail_category": "promotions"})]))
    assert gate.run_pass()["ok"]
    fam_mail.forget_availability()
    assert fam_mail.register_interest({"subject_terms": ["factura"]})["ok"]
    plain = fam_mail.messages()["messages"][0]
    assert not ({"html", "images", "headers"} & set(plain))
    rich = fam_mail.messages(fields=["html", "images", "headers"])["messages"][0]
    assert rich["html"] == MARKUP and rich["images"][0]["alt"] == "logo grande" and rich["headers"]["gmail_category"] == "promotions"
    assert fam_mail.messages(fields="headers")["messages"][0]["headers"]["list_unsubscribe"] == "<mailto:u@x>"
    assert "html" not in fam_mail.messages(fields="headers")["messages"][0]


# ---- interest: exclude, all_of, category -------------------------------------------------------------------------

def test_the_old_spec_normalizes_to_the_old_shape():
    spec = normalize_spec({"subject_terms": "factura", "has_attachment": True})
    assert set(spec) == {"subject_terms", "from_domains", "from_addresses", "text_terms", "regex", "has_attachment"}
    assert not ({"exclude", "all_of", "category"} & set(spec))


def test_normalize_spec_the_new_keys():
    spec = normalize_spec({"subject_terms": ["pedido"], "exclude": {"subject_terms": ["oferta", ""], "category": "promotions"},
                           "all_of": [{"from_domains": ["amazon.es"]}, {"text_terms": ["envio"], "exclude": {"regex": "spam"}}, {}],
                           "category": ["promo", "Security", "promo"]})
    assert spec["category"] == ["promo", "security"]
    assert spec["exclude"]["subject_terms"] == ["oferta"] and spec["exclude"]["category"] == ["promo"]
    assert len(spec["all_of"]) == 2 and spec["all_of"][1]["exclude"]["regex"] == "spam"          # the empty sub-spec is dropped
    assert "exclude" not in normalize_spec({"subject_terms": ["x"], "exclude": {"subject_terms": []}})
    for bad in ({"category": "weather"}, {"all_of": "x"}, {"exclude": "x"}, {"all_of": [{"all_of": [{"subject_terms": ["x"]}]}]},
                {"exclude": {"regex": "(unclosed"}}, {"all_of": [{"regex": "(unclosed"}]}):
        with pytest.raises(ValueError):
            normalize_spec(bad)


def msg(subject="", frm="x@example.com", name="", text="", attachments=None):
    return {"subject": subject, "from_addr": frm, "from_name": name, "text": text, "attachments": attachments or []}


@pytest.mark.parametrize("spec,message,expected", [
    # exclude drops what the plain criteria matched
    ({"subject_terms": ["pedido"], "exclude": {"subject_terms": ["oferta"]}}, msg("Tu pedido 12"), True),
    ({"subject_terms": ["pedido"], "exclude": {"subject_terms": ["oferta"]}}, msg("Tu pedido en oferta"), False),
    ({"subject_terms": ["pedido"], "exclude": {"from_domains": ["spam.test"]}}, msg("Tu pedido", "a@mail.spam.test"), False),
    ({"has_attachment": True, "exclude": {"from_addresses": ["me@x.com"]}}, msg("s", "me@x.com", attachments=[{"name": "a.pdf"}]), False),
    ({"has_attachment": True, "exclude": {"from_addresses": ["me@x.com"]}}, msg("s", "you@x.com", attachments=[{"name": "a.pdf"}]), True),
    ({"subject_terms": ["x"], "exclude": {"regex": r"\bFWD\b"}}, msg("x FWD"), False),
    # all_of: every sub-spec must match, each with the any-of rule
    ({"all_of": [{"from_domains": ["amazon.es"]}, {"subject_terms": ["pedido", "envio"]}]}, msg("Tu envio", "a@amazon.es"), True),
    ({"all_of": [{"from_domains": ["amazon.es"]}, {"subject_terms": ["pedido", "envio"]}]}, msg("Tu envio", "a@other.es"), False),
    ({"all_of": [{"from_domains": ["amazon.es"]}, {"subject_terms": ["pedido", "envio"]}]}, msg("Hola", "a@amazon.es"), False),
    # plain criteria AND all_of
    ({"subject_terms": ["pedido"], "all_of": [{"from_domains": ["amazon.es"]}]}, msg("Tu pedido", "a@amazon.es"), True),
    ({"subject_terms": ["pedido"], "all_of": [{"from_domains": ["amazon.es"]}]}, msg("Hola", "a@amazon.es"), False),
    ({"subject_terms": ["pedido"], "all_of": [{"from_domains": ["amazon.es"]}]}, msg("Tu pedido", "a@other.es"), False),
    # category: a filter on top of the plain criteria, or the criterion by itself
    ({"subject_terms": ["descuento"], "category": ["promo"]}, msg("Descuento del 50% hoy", "news@tienda.es"), True),
    ({"subject_terms": ["descuento"], "category": ["social"]}, msg("Descuento del 50% hoy", "news@tienda.es"), False),
    ({"category": ["security"]}, msg("Tu código de verificación"), True),
    ({"category": ["security"]}, msg("Hola, ¿comemos?"), False),
    ({"category": ["dev", "social"]}, msg("Nueva pull request", "noreply@github.com"), True),
    ({"subject_terms": ["x"], "exclude": {"category": ["promo"]}}, msg("x oferta", "news@tienda.es"), False),
    # an empty spec still matches nothing
    ({}, msg("anything"), False),
    ({"exclude": {"subject_terms": ["x"]}}, msg("anything"), False),
])
def test_match_interest_extras(spec, message, expected):
    assert match_interest(normalize_spec(spec), message) is expected


def test_category_uses_the_same_guess_as_the_unowned_tray():
    m = msg("Descuento del 50% hoy", "news@tienda.es")
    assert guess_category(m) == "promo" and guess_category({"subject": "x", "from_address": "noreply@github.com"}) == "dev"


def test_the_gateway_applies_the_new_interest_keys_per_app(world):         # noqa: F811
    gate = world["gate"]
    gate.post(req("POST", "/api/mail/config", body={"enabled": True, "interval_min": 10}))
    gate.runner.answers.append(answer([
        rec(1, "Tu pedido 1", "envios@amazon.es"), rec(2, "Tu pedido reembolsado", "envios@amazon.es"), rec(3, "Tu pedido 3", "x@other.es"),
        rec(4, "Descuento del 50% hoy", "news@tienda.es")]))
    assert gate.run_pass()["new"] == 4
    fam_mail.forget_availability()
    reg = fam_mail.register_interest({"subject_terms": ["pedido"], "exclude": {"subject_terms": ["reembols"]},
                                      "all_of": [{"from_domains": ["amazon.es"]}]})
    assert reg["ok"] and reg["spec"]["exclude"]["subject_terms"] == ["reembols"] and reg["spec"]["all_of"][0]["from_domains"] == ["amazon.es"]
    assert [m["subject"] for m in fam_mail.messages()["messages"]] == ["Tu pedido 1"]
    assert fam_mail.register_interest({"category": "promotions"})["ok"]
    assert [m["subject"] for m in fam_mail.messages()["messages"]] == ["Descuento del 50% hoy"]
    bad = fam_mail.register_interest({"category": "weather"})
    assert bad["ok"] is False and "category" in bad["error"]
    # the old spec still works
    assert fam_mail.register_interest({"subject_terms": ["pedido"]})["ok"] and len(fam_mail.messages()["messages"]) == 3
