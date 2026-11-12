"""Outbound contract: real MIME composition, explicit sending and authenticated gateway routes."""
from email import message_from_bytes, policy
from types import SimpleNamespace

import pytest

from hoard_link import mail_helper as mh
from .test_mailgate import gate, req


class Transport:
    def __init__(self, refused=None):
        self.sent = []
        self.drafts = []
        self.refused = refused or {}

    def send_message(self, msg, **kw):
        self.sent.append((msg, kw))
        return self.refused

    def list(self):
        return "OK", [b'(\\HasNoChildren \\Drafts) "/" "Borradores"']

    def append(self, *args):
        self.drafts.append(args)
        return "OK", [b"[APPENDUID 12 3]"]

    def quit(self):
        pass

    logout = quit


@pytest.fixture
def transport(monkeypatch):
    conn = Transport()
    monkeypatch.setattr(mh, "_connect", lambda cfg: conn)
    server = SimpleNamespace(_resolve_send_config=lambda account: ("Mine", {"from_address": "me@example.com"}),
                             _imap_connect=lambda account: conn)
    return conn, server


def payload(**kw):
    return {"to": ["me@example.com"], "subject": "Cobro de prueba", "body": "Texto con acentos: envío.", **kw}


def test_default_is_remote_draft_with_mime_and_bcc(transport, tmp_path):
    conn, server = transport
    file = tmp_path / "nota.txt"
    file.write_text("Adjunto de prueba", encoding="utf-8")
    answer = mh.compose(server, payload(attachments=[str(file)], bcc=["hidden@example.com"], html="<p>Texto</p>"))
    assert answer["ok"] and answer["state"] == "draft" and not answer["sent"]
    assert not conn.sent
    folder, flags, _, raw = conn.drafts[0]
    assert folder == '"Borradores"' and flags == "\\Draft"
    msg = message_from_bytes(raw, policy=policy.default)
    assert msg["Bcc"] == "hidden@example.com"
    [att] = list(msg.iter_attachments())
    assert att.get_filename() == "nota.txt" and att.get_payload(decode=True) == file.read_bytes()


def test_explicit_smtp_uses_envelope_without_bcc_header(transport):
    conn, server = transport
    answer = mh.compose(server, payload(mode="send", cc=["copy@example.com"], bcc=["hidden@example.com"]))
    assert answer["ok"] and answer["sent"] and answer["state"] == "sent" and answer["message_id"]
    assert not conn.drafts
    msg, envelope = conn.sent[0]
    assert msg["Bcc"] is None and msg["Cc"] == "copy@example.com"
    assert envelope["to_addrs"] == ["me@example.com", "copy@example.com", "hidden@example.com"]


@pytest.mark.parametrize("changes", [{"to": []}, {"to": ["bad-address"]}, {"to": ["me@example.com\nBcc: x@y.com"]},
                                     {"attachments": ["relative.txt"]}, {"mode": "later"}, {"subject": ""}])
def test_invalid_requests_never_connect(transport, changes):
    conn, server = transport
    assert not mh.compose(server, payload(**changes))["ok"]
    assert not conn.sent and not conn.drafts


def test_partial_delivery_reports_accepted_without_retry(transport):
    conn, server = transport
    conn.refused = {"copy@example.com": (550, b"refused")}
    result = mh.compose(server, payload(mode="send", cc=["copy@example.com"]))
    assert not result["ok"] and result["state"] == "partial"
    assert result["accepted"] == ["me@example.com"] and len(conn.sent) == 1


def test_lost_reply_is_unknown_and_never_retried(transport):
    conn, server = transport
    def lost(*a, **kw):
        conn.sent.append(True)
        raise TimeoutError("reply lost")
    conn.send_message = lost
    result = mh.compose(server, payload(mode="send"))
    assert not result["ok"] and result["state"] == "unknown" and len(conn.sent) == 1


def test_gateway_auth_account_scope_and_explicit_mode(gate):
    assert gate.post(req("POST", "/api/mail/compose", caller=None, body=payload()))["status"] == 401
    gate.runner.answers = [{"ok": True, "send_ready": True, "accounts": [
        {"account": "Work", "id": "2", "address": "work@corp.com"},
        {"account": "Mine", "id": "1", "address": "me@example.com"}]}]
    status = gate.get(req("GET", "/api/mail/accounts", caller="ledger"))
    assert status["available"] and [r["account"] for r in status["accounts"]] == ["Mine"]
    result = gate.compose_mail(payload(account="Work", mode="send"), "ledger")
    assert not result["ok"] and len(gate.runner.requests) == 1
    gate.runner.answers = [{"ok": True, "state": "draft", "sent": False}]
    result = gate.post(req("POST", "/api/mail/compose", caller="ledger", body=payload()))
    assert result["state"] == "draft"
    assert gate.runner.requests[-1]["action"] == "compose" and "mode" not in gate.runner.requests[-1]
    gate.runner.answers = [{"ok": True, "state": "sent", "sent": True, "message_id": "<test@example.com>"}]
    result = gate.handlers()["hub_mail_compose"](payload(mode="send"))
    assert result["sent"] and gate.runner.requests[-1]["mode"] == "send"


def test_catalogue_and_service(gate, hub):
    names = {t["name"]: t for t in gate.tools()}
    assert names["hub_mail_compose"]["annotations"]["idempotentHint"] is False
    gate.runner.answers = [{"ok": True, "send_ready": True, "accounts": [{"account": "Mine", "address": "me@example.com"}]}]
    result = hub.facet("services")._probe(("mail", "hub", ("hub_mail_accounts", "hub_mail_compose")))
    assert result["available"] and result["state"] == "ready"
